"""Per-thread conflict-intensity scoring using TypeSafe.

We care about topical *conversations*, not strictly "disputes" — most threads
are calm chats about a shared subject, and conflict intensity is just one
attribute of that conversation, not what defines it. So this classifier does
two batched passes over TypeSafe:

1. Score how heated/conflictual each thread is (calm -> hostile).
2. Only for threads that actually show disagreement (heat >= mild_disagreement)
   ask who, if anyone, made the more convincing case.

Note on why this isn't a binary "was it resolved?" question: that phrasing
(does the disagreement end without being resolved/agreed upon, e.g. it trails
off) is a leading question. Almost every chat argument "trails off" instead of
ending in an explicit "you're right, I concede" — that's just how casual chat
disagreements end, resolved or not. Asking that question came back "yes,
unresolved" on ~99% of disputed threads (386/389 in one real run), which is
not a useful signal, just an artifact of the phrasing. Asking who was more
convincing is a question that actually has a distribution of answers instead
of a near-constant one, and gives at least *some* real information about the
dispute's outcome.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from tqdm import tqdm

from common.segmenter import ParsedMessage

from .client import CachedTypeSafeClient, Choice, Score

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
MAX_MESSAGES_IN_STATE = 40
MAX_CHARS_PER_MESSAGE = 400

# Heat scores (0=calm .. 3=hostile) at/above this level are treated as
# showing real disagreement, worth asking a follow-up "who was more
# convincing?" question about. Below this, threads are on-topic conversation
# without any actual conflict, so the question doesn't apply and is left as
# None.
UNRESOLVED_HEAT_THRESHOLD = 1.0

# Choice option keys standing in for "no participant came out ahead", used
# alongside the thread's real participant names as choices.
NO_ONE_MORE_CONVINCING = "__no_one__"
UNCLEAR = "__unclear__"

# Cap on how many named participants get their own Choice option. Busy
# threads can have a dozen+ distinct senders (drive-by commenters, etc.);
# beyond this we only offer the most active ones as named choices so the
# question stays answerable instead of an unwieldy pile of one-off names.
MAX_NAMED_PARTICIPANTS = 8


@dataclass
class ThreadInsight:
    heat_score: float
    heat_label: str
    # None if heat was below UNRESOLVED_HEAT_THRESHOLD (no real disagreement
    # to judge). Otherwise the name of the participant judged more
    # convincing, or None if the model picked "no one"/"unclear".
    more_convincing_participant: Optional[str]
    more_convincing_confidence: Optional[float]


def _thread_state(thread: List[ParsedMessage]) -> List[Dict[str, str]]:
    trimmed = thread[:MAX_MESSAGES_IN_STATE]
    return [
        {
            "author": m.sender_name,
            "text": (m.text.strip() or f"[{m.media_type or 'media'}]")[:MAX_CHARS_PER_MESSAGE],
        }
        for m in trimmed
    ]


class ThreadInsightClassifier:
    """Rates each thread's conflict intensity and resolution status via TypeSafe."""

    def __init__(self, client: CachedTypeSafeClient):
        self.client = client

    def analyze(self, threads: List[List[ParsedMessage]]) -> List[ThreadInsight]:
        heat_scores, heat_labels_per_thread = self._score_heat(threads)
        convincing_by_idx = self._score_more_convincing(threads, heat_scores)

        insights: List[ThreadInsight] = []
        for idx in range(len(threads)):
            participant, confidence = convincing_by_idx.get(idx, (None, None))
            insights.append(
                ThreadInsight(
                    heat_score=heat_scores[idx],
                    heat_label=heat_labels_per_thread[idx],
                    more_convincing_participant=participant,
                    more_convincing_confidence=confidence,
                )
            )
        return insights

    def _score_heat(
        self, threads: List[List[ParsedMessage]]
    ) -> "tuple[List[float], List[str]]":
        heat_labels = ["calm", "mild_disagreement", "clear_dispute", "hostile"]
        scores: List[float] = [0.0] * len(threads)
        labels: List[str] = ["calm"] * len(threads)

        batch_starts = list(range(0, len(threads), BATCH_SIZE))
        for batch_start in tqdm(batch_starts, desc="Scoring thread heat", unit="batch"):
            batch = threads[batch_start : batch_start + BATCH_SIZE]
            state = {}
            questions = {}
            for local_idx, thread in enumerate(batch):
                key = f"thread_{local_idx}"
                state[key] = {"messages": _thread_state(thread)}
                questions[f"{key}_heat"] = Score(
                    instructions=(
                        f"How heated/conflictual is the discussion in `{key}.messages`? "
                        f"Most everyday conversation is calm; only score higher when "
                        f"there is real pushback or disagreement between participants."
                    ),
                    criteria=[
                        "Calm, friendly or purely informational exchange (no real disagreement)",
                        "Mild disagreement, still polite and constructive",
                        "Clear argument/dispute with pushback between participants",
                        "Hostile, aggressive, or heavily escalated conflict",
                    ],
                )

            logger.debug("Rating %d threads for conflict heat via TypeSafe...", len(batch))
            response = self.client.system_one(state=state, questions=questions)

            for local_idx in range(len(batch)):
                key = f"thread_{local_idx}"
                score_val = response["answers"][f"{key}_heat"]["score"]
                label_idx = max(0, min(len(heat_labels) - 1, round(score_val)))
                global_idx = batch_start + local_idx
                scores[global_idx] = score_val
                labels[global_idx] = heat_labels[label_idx]

        return scores, labels

    def _score_more_convincing(
        self, threads: List[List[ParsedMessage]], heat_scores: List[float]
    ) -> "Dict[int, Tuple[Optional[str], float]]":
        """Asks 'who made the more convincing case?' only for threads with real
        disagreement.

        Calm, purely topical conversations have no dispute to judge, so we
        skip the question there entirely. See the module docstring for why
        this replaced a binary "was it resolved?" question.
        """
        candidate_indices = [
            i for i, score in enumerate(heat_scores) if score >= UNRESOLVED_HEAT_THRESHOLD
        ]
        results: Dict[int, Tuple[Optional[str], float]] = {}

        batch_starts = list(range(0, len(candidate_indices), BATCH_SIZE))
        for batch_start in tqdm(
            batch_starts, desc="Scoring dispute outcome", unit="batch"
        ):
            batch_indices = candidate_indices[batch_start : batch_start + BATCH_SIZE]
            state = {}
            questions = {}
            # Choice criteria are keyed by option name, and participant names
            # can contain characters/duplicates that don't make safe keys, so
            # each thread gets its own local name->key mapping.
            name_by_key_per_thread: Dict[int, Dict[str, str]] = {}
            for local_idx, thread_idx in enumerate(batch_indices):
                key = f"thread_{local_idx}"
                thread = threads[thread_idx]
                state[key] = {"messages": _thread_state(thread)}

                message_counts: Dict[str, int] = {}
                for m in thread:
                    message_counts[m.sender_name] = message_counts.get(m.sender_name, 0) + 1
                participants = sorted(
                    message_counts, key=lambda n: message_counts[n], reverse=True
                )[:MAX_NAMED_PARTICIPANTS]
                name_by_key: Dict[str, str] = {}
                criteria: Dict[str, str] = {
                    NO_ONE_MORE_CONVINCING: "No participant came out ahead; it was a wash.",
                    UNCLEAR: "Can't tell from the messages who (if anyone) was more convincing.",
                }
                for p_idx, name in enumerate(participants):
                    option_key = f"p{p_idx}"
                    name_by_key[option_key] = name
                    criteria[option_key] = f"{name} made the more convincing case."
                name_by_key_per_thread[thread_idx] = name_by_key

                questions[f"{key}_convincing"] = Choice(
                    instructions=(
                        f"`{key}.messages` contains a disagreement between participants. "
                        f"Judging only by the arguments made (not who posted last or most), "
                        f"who made the more convincing case?"
                    ),
                    criteria=criteria,
                )

            logger.debug(
                "Rating %d disputed threads for outcome via TypeSafe...", len(batch_indices)
            )
            response = self.client.system_one(state=state, questions=questions)

            for local_idx, thread_idx in enumerate(batch_indices):
                key = f"thread_{local_idx}"
                answer = response["answers"][f"{key}_convincing"]
                choice_key = answer["choice"]
                confidence = answer["confidence"]
                name_by_key = name_by_key_per_thread[thread_idx]
                participant_name = name_by_key.get(choice_key)  # None for no_one/unclear
                results[thread_idx] = (participant_name, confidence)

        return results

"""Smart thread linking using TypeSafe Noul/Choice/Score judgments.

Plain heuristics (reply_to chains + time-gap sessions) get you "raw sessions":
blocks of messages close together in time. But busy groups often have several
independent conversations interleaved in the same session, and people reply
"in spirit" without pressing the Reply button. This module asks Jev (via
TypeSafe) whether one message actually continues another's topic/argument,
batching many such pairwise questions into as few API calls as possible
(TypeSafe's "speculative fan-out": many questions, one state, one call).

The within-session pass above only ever compares *adjacent* messages inside a
single raw time-gap session, so a topic that resumes after the raw session
boundary (e.g. the conversation pauses for an hour, then picks back up) ends
up split into two separate threads with no chance to be recombined. A second
"bridging" pass fixes this: after threads are formed, chronologically
adjacent threads within a wider gap are checked (again via TypeSafe) for
whether the later one actually continues the earlier one's topic, and merged
if so. This means a short, seemingly throwaway thread isn't just dropped by
--min-thread-messages if it's really part of a bigger conversation that
resumes later — instead it gets fused into it.
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from tqdm import tqdm

from common.segmenter import DialogueClusterer, ParsedMessage

from .client import CachedTypeSafeClient, Noul

logger = logging.getLogger(__name__)

# How many pairwise Noul questions to bundle into a single TypeSafe API call.
BATCH_SIZE = 20

# How many batches to have in flight at once. TypeSafe's published limit is
# 1,200 requests/minute; the client's internal rate limiter (see client.py)
# already paces individual calls, so this just controls how many worker
# threads are available to keep the rate limiter saturated. Higher than
# necessary just means more idle waiting threads, not more throughput.
DEFAULT_CONCURRENCY = 10

# Consecutive messages from the same sender within this many minutes are
# treated as a trivial self-continuation and linked without asking the model.
SAME_SENDER_AUTO_LINK_MINUTES = 5

# Beyond this gap we don't even bother asking the model; it's very unlikely
# to be a continuation and would just burn tokens.
MAX_CANDIDATE_GAP_MINUTES = 30

# Noul probability at/above which we accept "curr continues prev".
LINK_CONFIDENCE_THRESHOLD = 0.6

# Cross-thread bridging uses a stricter bar than within-session linking:
# merging two already-formed threads is a bigger, harder-to-undo decision
# (it can pull in many messages at once) than linking two adjacent messages,
# so we require higher confidence before doing it.
BRIDGE_CONFIDENCE_THRESHOLD = 0.75

# For the cross-thread bridging pass: how far apart (by wall-clock time
# between the earlier thread's last message and the later thread's first
# message) two threads can be and still be worth asking about. Wider than
# MAX_CANDIDATE_GAP_MINUTES (a resumed topic across a lull is exactly the
# case the within-session pass misses), but deliberately not a full day: in
# a busy group, "same participants, within a day" is true for dozens of
# unrelated topics, which caused false-positive merges chaining together
# entire days of unrelated conversation transitively (A merges with B,
# B merges with C, so A+C end up merged even though they're unrelated).
MAX_BRIDGE_GAP_MINUTES = 90

# How many other same-participant threads that follow a given thread to
# consider as bridging candidates. Kept at 1 (only the immediately-next
# candidate thread) rather than several, specifically to avoid the
# transitive-chaining problem above: each thread can bridge to at most one
# later neighbor, so a merge chain can't silently absorb many unrelated
# threads in a row.
MAX_BRIDGE_CANDIDATES_PER_THREAD = 1


@dataclass
class LinkDecision:
    prev_id: int
    curr_id: int
    probability: float
    linked: bool


def _minutes_between(a: ParsedMessage, b: ParsedMessage) -> Optional[float]:
    if not a.date_dt or not b.date_dt:
        return None
    return abs((b.date_dt - a.date_dt).total_seconds()) / 60.0


def _msg_state(m: ParsedMessage) -> Dict[str, str]:
    return {
        "author": m.sender_name,
        "text": m.text.strip() or f"[{m.media_type or 'media'} attachment, no text]",
    }


class SmartThreadLinker:
    """Refines raw time/reply sessions into topic-coherent threads via TypeSafe."""

    def __init__(self, client: CachedTypeSafeClient, concurrency: int = DEFAULT_CONCURRENCY):
        self.client = client
        self.concurrency = max(1, concurrency)

    def _gather_candidate_pairs(
        self, session: List[ParsedMessage], by_id: Dict[int, ParsedMessage]
    ) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
        """Returns (auto_linked_pairs, pairs_needing_model_judgment)."""
        auto_linked: List[Tuple[int, int]] = []
        candidates: List[Tuple[int, int]] = []

        explicit_reply_sources = {
            m.message_id
            for m in session
            if m.reply_to_msg_id and m.reply_to_msg_id in by_id
        }

        for i in range(1, len(session)):
            curr = session[i]
            prev = session[i - 1]

            if curr.message_id in explicit_reply_sources:
                # Already linked by an explicit reply_to elsewhere.
                continue

            gap = _minutes_between(prev, curr)
            if gap is None:
                continue

            if curr.sender_name == prev.sender_name and gap <= SAME_SENDER_AUTO_LINK_MINUTES:
                auto_linked.append((prev.message_id, curr.message_id))
                continue

            if gap <= MAX_CANDIDATE_GAP_MINUTES:
                candidates.append((prev.message_id, curr.message_id))

        return auto_linked, candidates

    def _ask_one_batch(
        self,
        batch: List[Tuple[int, int]],
        by_id: Dict[int, ParsedMessage],
    ) -> List[LinkDecision]:
        state: Dict[str, Dict] = {}
        questions = {}
        for idx, (prev_id, curr_id) in enumerate(batch):
            key = f"pair_{idx}"
            prev_m = by_id[prev_id]
            curr_m = by_id[curr_id]
            state[key] = {
                "message_a": _msg_state(prev_m),
                "message_b": _msg_state(curr_m),
            }
            questions[key] = Noul(
                instructions=(
                    f"In `{key}`, is `message_b` a reply, rebuttal, follow-up, "
                    f"or direct continuation of the same topic/argument as "
                    f"`message_a`, rather than an unrelated new topic someone "
                    f"else started?"
                ),
                criteria={
                    "true": "message_b directly engages with message_a's point, question, or argument",
                    "false": "message_b starts an unrelated topic or reacts to something else entirely",
                },
            )

        logger.debug("Asking TypeSafe about %d candidate links...", len(batch))
        response = self.client.system_one(state=state, questions=questions)

        decisions: List[LinkDecision] = []
        for idx, (prev_id, curr_id) in enumerate(batch):
            key = f"pair_{idx}"
            noul_value = response["answers"][key]["noul"]
            decisions.append(
                LinkDecision(
                    prev_id=prev_id,
                    curr_id=curr_id,
                    probability=noul_value,
                    linked=noul_value >= LINK_CONFIDENCE_THRESHOLD,
                )
            )
        return decisions

    def _ask_all_pairs(
        self,
        pairs: List[Tuple[int, int]],
        by_id: Dict[int, ParsedMessage],
    ) -> List[LinkDecision]:
        """Sends every candidate pair (across all sessions) as batches, run
        concurrently across self.concurrency worker threads. The client's
        internal rate limiter keeps the combined request rate under the
        configured cap, so raising concurrency here just keeps that limiter
        saturated instead of leaving it idle between batches.
        """
        batches = [pairs[i : i + BATCH_SIZE] for i in range(0, len(pairs), BATCH_SIZE)]
        if not batches:
            return []

        decisions: List[LinkDecision] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(self._ask_one_batch, batch, by_id) for batch in batches]
            for future in tqdm(as_completed(futures), total=len(futures), desc="Linking candidate pairs", unit="batch"):
                decisions.extend(future.result())
        return decisions

    def _bridge_candidate_pairs(
        self, threads: List[List[ParsedMessage]]
    ) -> List[Tuple[int, int]]:
        """Finds (thread_idx_a, thread_idx_b) pairs worth asking about, where
        b starts shortly (within MAX_BRIDGE_GAP_MINUTES) after a ends and the
        two threads share at least one participant. Only considers a bounded
        number of candidates per thread so this stays cheap even with many
        threads.
        """
        pairs: List[Tuple[int, int]] = []
        n = len(threads)
        for i in range(n):
            end_i = threads[i][-1]
            if not end_i.date_dt:
                continue
            participants_i = {m.sender_name for m in threads[i]}
            found = 0
            for j in range(i + 1, n):
                if found >= MAX_BRIDGE_CANDIDATES_PER_THREAD:
                    break
                start_j = threads[j][0]
                if not start_j.date_dt:
                    continue
                # Directional gap (not abs): threads are sorted by start
                # time, so start_j is non-decreasing as j grows, which means
                # this gap is also non-decreasing. Once it exceeds the
                # threshold we can safely stop scanning further j for this i.
                directional_gap = (start_j.date_dt - end_i.date_dt).total_seconds() / 60.0
                if directional_gap > MAX_BRIDGE_GAP_MINUTES:
                    break
                if directional_gap < 0:
                    # Overlapping in time with thread i (interleaved
                    # conversations); not the "resumed after a lull" case
                    # this pass targets, skip.
                    continue
                if participants_i & {m.sender_name for m in threads[j]}:
                    pairs.append((i, j))
                    found += 1
        return pairs

    def _thread_boundary_state(
        self, thread: List[ParsedMessage], take_from_end: bool, n: int = 3
    ) -> List[Dict[str, str]]:
        slice_ = thread[-n:] if take_from_end else thread[:n]
        return [_msg_state(m) for m in slice_]

    def _ask_bridge_batch(
        self, batch: List[Tuple[int, int]], threads: List[List[ParsedMessage]]
    ) -> Dict[Tuple[int, int], bool]:
        state: Dict[str, Dict] = {}
        questions = {}
        for idx, (i, j) in enumerate(batch):
            key = f"bridge_{idx}"
            state[key] = {
                "earlier_thread_tail": self._thread_boundary_state(threads[i], take_from_end=True),
                "later_thread_head": self._thread_boundary_state(threads[j], take_from_end=False),
            }
            questions[key] = Noul(
                instructions=(
                    f"In `{key}`, does `later_thread_head` continue the same "
                    f"topic/argument as `earlier_thread_tail` (e.g. someone "
                    f"picking the conversation back up after a pause), rather "
                    f"than being an unrelated new topic?"
                ),
                criteria={
                    "true": "later_thread_head clearly resumes the same subject or argument",
                    "false": "later_thread_head is about something unrelated",
                },
            )

        logger.debug("Asking TypeSafe about %d cross-thread bridges...", len(batch))
        response = self.client.system_one(state=state, questions=questions)

        results: Dict[Tuple[int, int], bool] = {}
        for idx, (i, j) in enumerate(batch):
            key = f"bridge_{idx}"
            noul_value = response["answers"][key]["noul"]
            results[(i, j)] = noul_value >= BRIDGE_CONFIDENCE_THRESHOLD
        return results

    def bridge_adjacent_threads(
        self, threads: List[List[ParsedMessage]]
    ) -> List[List[ParsedMessage]]:
        """Merges chronologically-nearby threads that TypeSafe judges to be a
        continuation of the same topic, so a topic resumed after a lull (or a
        short thread that's really part of a bigger conversation) doesn't
        stay artificially split.
        """
        if len(threads) < 2:
            return threads

        candidate_pairs = self._bridge_candidate_pairs(threads)
        if not candidate_pairs:
            return threads

        batches = [
            candidate_pairs[i : i + BATCH_SIZE] for i in range(0, len(candidate_pairs), BATCH_SIZE)
        ]
        merge_decisions: Dict[Tuple[int, int], bool] = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(self._ask_bridge_batch, batch, threads) for batch in batches]
            for future in tqdm(
                as_completed(futures), total=len(futures), desc="Bridging threads", unit="batch"
            ):
                merge_decisions.update(future.result())

        parent = list(range(len(threads)))

        def find(x: int) -> int:
            while parent[x] != x:
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for (i, j), should_merge in merge_decisions.items():
            if should_merge:
                union(i, j)

        groups: Dict[int, List[ParsedMessage]] = defaultdict(list)
        for idx, thread in enumerate(threads):
            groups[find(idx)].extend(thread)

        merged = list(groups.values())
        for group in merged:
            group.sort(key=lambda m: (m.date_dt or datetime.min, m.message_id))
        merged.sort(key=lambda th: th[0].date_dt or datetime.min)
        return merged

    def refine_sessions_into_threads(
        self,
        messages: List[ParsedMessage],
        raw_session_gap_minutes: int = 45,
    ) -> List[List[ParsedMessage]]:
        """Splits raw time-gap sessions into topic-coherent sub-threads using Jev.

        Two phases: first build per-session union-find structures and collect
        every candidate pair across ALL sessions (pure local work, no network),
        then fire all of them at TypeSafe in one concurrently-batched pass, so
        --concurrency workers stay busy across the whole run instead of being
        idle between small per-session batches.
        """
        by_id = {m.message_id: m for m in messages}
        raw_clusterer = DialogueClusterer(messages, session_gap_minutes=raw_session_gap_minutes)
        raw_sessions = raw_clusterer.extract_threads(min_messages=1, min_participants=1)

        session_states = []  # (session, union_parent, find, union) per eligible session
        all_candidates: List[Tuple[int, int]] = []
        small_sessions: List[List[ParsedMessage]] = []

        for session in raw_sessions:
            session = sorted(session, key=lambda m: (m.date_dt or datetime.min, m.message_id))

            if len(session) < 3:
                small_sessions.append(session)
                continue

            union_parent: Dict[int, int] = {m.message_id: m.message_id for m in session}

            def find(x: int, _parent=union_parent) -> int:
                while _parent[x] != x:
                    x = _parent[x]
                return x

            def union(a: int, b: int, _parent=union_parent, _find=find):
                ra, rb = _find(a), _find(b)
                if ra != rb:
                    _parent[rb] = ra

            # 1. Explicit reply_to edges are ground truth.
            for m in session:
                if m.reply_to_msg_id and m.reply_to_msg_id in union_parent:
                    union(m.reply_to_msg_id, m.message_id)

            # 2. Auto-linked same-sender bursts need no model call.
            auto_linked, candidates = self._gather_candidate_pairs(session, by_id)
            for prev_id, curr_id in auto_linked:
                union(prev_id, curr_id)

            session_states.append((session, union_parent, find, union))
            all_candidates.extend(candidates)

        # 3. One global, concurrently-batched pass over every candidate pair
        # gathered across all sessions.
        decisions = self._ask_all_pairs(all_candidates, by_id)
        decisions_by_pair = {(d.prev_id, d.curr_id): d for d in decisions}

        refined_threads: List[List[ParsedMessage]] = list(small_sessions)

        for session, union_parent, find, union in session_states:
            auto_linked, candidates = self._gather_candidate_pairs(session, by_id)
            for prev_id, curr_id in candidates:
                d = decisions_by_pair.get((prev_id, curr_id))
                if d is None:
                    continue
                if d.linked:
                    union(prev_id, curr_id)
                logger.debug(
                    "Pair (%s -> %s): p=%.2f linked=%s",
                    d.prev_id, d.curr_id, d.probability, d.linked,
                )

            groups: Dict[int, List[ParsedMessage]] = defaultdict(list)
            for m in session:
                groups[find(m.message_id)].append(m)

            for group in groups.values():
                group.sort(key=lambda x: (x.date_dt or datetime.min, x.message_id))
                refined_threads.append(group)

        refined_threads.sort(key=lambda th: th[0].date_dt or datetime.min)
        return refined_threads

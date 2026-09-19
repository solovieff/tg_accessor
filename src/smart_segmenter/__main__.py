"""CLI entry point: smart, TypeSafe-powered thread segmentation from an exported CSV.

Runs entirely offline against Telegram (no network calls to Telegram), but
calls the TypeSafe API to make smarter thread-boundary and conflict-intensity
judgments than plain reply/time heuristics.

Example:
    python -m smart_segmenter --from-csv exported_dialogues/me/group_dialogue_export.csv
"""

import argparse
import logging
from pathlib import Path
from typing import List

from tqdm import tqdm

from common.segmenter import DialogueClusterer, ParsedMessage, load_exported_csv
from dotenv import load_dotenv

from .classifier import ThreadInsightClassifier
from .client import DEFAULT_MAX_REQUESTS_PER_MINUTE, CachedTypeSafeClient
from .exporter import export_thread_csv, export_thread_markdown, sanitize_filename
from .linker import DEFAULT_CONCURRENCY, SmartThreadLinker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smart_segmenter")

# The TypeSafe SDK logs every HTTP request/response at INFO level, which
# clutters the tqdm progress bars above. Quiet it down; real errors still
# surface via raised exceptions.
logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Smart TypeSafe-powered thread segmentation over an exported Telegram CSV",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--from-csv",
        required=True,
        help="Path to a CSV previously exported by the group-seeker CLI (full group dump)",
    )
    parser.add_argument(
        "--output-dir",
        default="smart_exported_dialogues",
        help="Directory to store refined threads (markdown + csv)",
    )
    parser.add_argument(
        "--session-gap-minutes",
        type=int,
        default=45,
        help="Raw time-gap used to form initial candidate sessions before smart splitting",
    )
    parser.add_argument(
        "--min-thread-messages",
        type=int,
        default=5,
        help=(
            "Minimum messages for a refined thread to be exported. We care about "
            "actual topical conversations, not 3-message drive-by exchanges; "
            "raise this further if the output still feels noisy."
        ),
    )
    parser.add_argument(
        "--skip-insights",
        action="store_true",
        help="Skip the heat/unresolved-conflict scoring pass (linking only, cheaper)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the local TypeSafe response cache (always call the API)",
    )
    parser.add_argument(
        "--cache-path",
        default=".typesafe_cache.json",
        help="Path to the local JSON cache of TypeSafe responses",
    )
    parser.add_argument(
        "--split-users",
        action="store_true",
        help=(
            "Instead of one flat set of threads for the whole CSV, split messages "
            "per active user first (each user's own messages + their dialogue "
            "context), then smart-segment each user's slice independently into "
            "smart_exported_dialogues/users/<name>/"
        ),
    )
    parser.add_argument(
        "--min-user-messages",
        type=int,
        default=3,
        help="Minimum own messages a user must have to get a --split-users slice",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            "How many TypeSafe API calls to have in flight at once. TypeSafe's "
            "requests/minute cap (see --max-requests-per-minute) is the real "
            "throughput ceiling; this just needs to be high enough to keep it "
            "saturated given per-call network latency (5-10 is normally plenty)"
        ),
    )
    parser.add_argument(
        "--max-requests-per-minute",
        type=float,
        default=DEFAULT_MAX_REQUESTS_PER_MINUTE,
        help=(
            "Target ceiling for combined TypeSafe requests/minute across all "
            "workers. TypeSafe's published limit is 1,200 req/min; this defaults "
            "to a conservative fraction of that to leave headroom"
        ),
    )
    return parser.parse_args()


def process_messages(
    messages: List[ParsedMessage],
    output_dir: Path,
    client: CachedTypeSafeClient,
    session_gap_minutes: int,
    min_thread_messages: int,
    skip_insights: bool,
    concurrency: int = DEFAULT_CONCURRENCY,
    label: str = "",
) -> int:
    """Runs smart linking + optional conflict scoring on one slice of messages,
    exporting the resulting threads under output_dir/{markdown,csv}. Returns
    the number of threads exported."""
    prefix = f"[{label}] " if label else ""

    linker = SmartThreadLinker(client, concurrency=concurrency)
    logger.info("%sRefining sessions into topic-coherent threads via TypeSafe...", prefix)
    threads = linker.refine_sessions_into_threads(
        messages, raw_session_gap_minutes=session_gap_minutes
    )

    logger.info(
        "%sChecking %d threads for topics that resume after a pause...", prefix, len(threads)
    )
    threads = linker.bridge_adjacent_threads(threads)

    threads = [t for t in threads if len(t) >= min_thread_messages]
    logger.info("%sGot %d threads with >= %d messages.", prefix, len(threads), min_thread_messages)

    insights = [None] * len(threads)
    if not skip_insights and threads:
        logger.info("%sScoring thread conflict intensity / resolution via TypeSafe...", prefix)
        classifier = ThreadInsightClassifier(client)
        insights = classifier.analyze(threads)

    md_dir = output_dir / "markdown"
    csv_dir = output_dir / "csv"
    for idx, (thread, insight) in enumerate(zip(threads, insights), 1):
        start_str = thread[0].date_str.replace(" ", "_").replace(":", "").replace("-", "") if thread[0].date_str else "unknown"
        base_name = f"thread_{idx:03d}_{start_str}"
        export_thread_csv(thread, csv_dir / f"{base_name}.csv")
        export_thread_markdown(thread, md_dir / f"{base_name}.md", index=idx, insight=insight)

    return len(threads)


def main():
    load_dotenv()
    args = parse_args()

    csv_path = Path(args.from_csv)
    if not csv_path.exists():
        logger.error("CSV file not found: %s", csv_path)
        return

    logger.info("Loading messages from %s...", csv_path)
    messages = load_exported_csv(csv_path)
    logger.info("Loaded %d messages.", len(messages))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with CachedTypeSafeClient(
        cache_path=args.cache_path,
        disable_cache=args.no_cache,
        max_requests_per_minute=args.max_requests_per_minute,
    ) as client:
        if args.split_users:
            clusterer = DialogueClusterer(messages)
            user_dialogues = clusterer.extract_all_users_dialogues(
                min_user_messages=args.min_user_messages
            )
            logger.info(
                "Split into %d active users (with >= %d own messages).",
                len(user_dialogues), args.min_user_messages,
            )

            users_root_dir = output_dir / "users"
            total_threads = 0
            for user_name, user_msgs in tqdm(
                user_dialogues.items(), desc="Processing users", unit="user"
            ):
                safe_name = sanitize_filename(user_name)
                user_dir = users_root_dir / safe_name
                logger.debug("=== User: %s (%d messages) ===", user_name, len(user_msgs))
                count = process_messages(
                    user_msgs,
                    user_dir,
                    client,
                    session_gap_minutes=args.session_gap_minutes,
                    min_thread_messages=args.min_thread_messages,
                    skip_insights=args.skip_insights,
                    concurrency=args.concurrency,
                    label=user_name,
                )
                total_threads += count

            logger.info(
                "Done. Exported %d refined threads across %d users to %s",
                total_threads, len(user_dialogues), users_root_dir.resolve(),
            )
        else:
            total_threads = process_messages(
                messages,
                output_dir,
                client,
                session_gap_minutes=args.session_gap_minutes,
                min_thread_messages=args.min_thread_messages,
                skip_insights=args.skip_insights,
                concurrency=args.concurrency,
            )
            logger.info("Done. Exported %d refined threads to %s", total_threads, output_dir.resolve())


if __name__ == "__main__":
    main()

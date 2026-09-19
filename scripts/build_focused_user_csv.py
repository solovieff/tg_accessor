#!/usr/bin/env python3
"""Build a tightly-scoped, per-user dialogue CSV from a full group export.

Unlike group-seeker's --split-users (which pulls in an *entire* thread the
moment the user appears anywhere in it, even with a one-word reply), this
keeps only:
  1. Threads the user meaningfully participated in (>= --min-thread-msgs
     messages from them in that thread).
  2. The user's own messages everywhere.
  3. Direct one-hop reply context (who they replied to, who replied to them).
Trivial no-text sticker reactions are dropped by default.

This is meant as a cheap, local pre-filter before spending TypeSafe API
calls in smart-segmenter, so we don't pay to analyze thousands of messages
between other people the target user barely touched.

Usage:
    uv run python scripts/build_focused_user_csv.py \\
        --from-csv group_dialogue_export.csv \\
        --user "Alice Bank (@the_alice)" \\
        --since 2024-09-19 \\
        --output /tmp/alice_focused.csv
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from common.segmenter import DialogueClusterer, load_exported_csv  # noqa: E402

CSV_FIELDS = [
    "message_id",
    "date",
    "sender_id",
    "sender_name",
    "is_target_user",
    "context_relation",
    "reply_to_msg_id",
    "media_type",
    "views",
    "forwards",
    "text",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-csv", required=True, help="Full raw group export CSV.")
    parser.add_argument("--user", required=True, help="Exact sender_name to focus on, e.g. 'Alice Bank (@the_alice)'.")
    parser.add_argument("--output", required=True, help="Where to write the focused CSV.")
    parser.add_argument("--since", default=None, help="Only keep messages on/after this date (YYYY-MM-DD).")
    parser.add_argument("--until", default=None, help="Only keep messages on/before this date (YYYY-MM-DD).")
    parser.add_argument("--context-window", type=int, default=3, help="How many neighboring messages (chronologically) to keep around each of the user's own messages (default: 3).")
    parser.add_argument("--keep-trivial-reactions", action="store_true", help="Don't drop text-less sticker reactions.")
    args = parser.parse_args()

    print(f"Loading full export from {args.from_csv} (this may take a while for large dumps)...")
    all_messages = load_exported_csv(args.from_csv)
    print(f"Loaded {len(all_messages)} total messages.")

    since_dt = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    until_dt = datetime.strptime(args.until, "%Y-%m-%d") if args.until else None
    if since_dt or until_dt:
        before = len(all_messages)
        all_messages = [
            m for m in all_messages
            if (since_dt is None or (m.date_dt and m.date_dt >= since_dt))
            and (until_dt is None or (m.date_dt and m.date_dt <= until_dt))
        ]
        print(f"Date filter {args.since or '...'}..{args.until or '...'}: {before} -> {len(all_messages)} messages.")

    clusterer = DialogueClusterer(all_messages, session_gap_minutes=15)
    focused = clusterer.extract_focused_user_dialogue(
        user_name=args.user,
        context_window=args.context_window,
        drop_trivial_reactions=not args.keep_trivial_reactions,
    )

    own_count = sum(1 for m in focused if m.sender_name == args.user)
    print(f"Focused dialogue for '{args.user}': {len(focused)} messages kept ({own_count} are their own).")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for m in focused:
            writer.writerow({
                "message_id": m.message_id,
                "date": m.date_str,
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "is_target_user": m.sender_name == args.user,
                "context_relation": m.context_relation,
                "reply_to_msg_id": m.reply_to_msg_id or "",
                "media_type": m.media_type,
                "views": m.views,
                "forwards": m.forwards,
                "text": m.text,
            })

    print(f"Wrote {output_path} ({output_path.stat().st_size / 1024:.1f} KB).")


if __name__ == "__main__":
    main()

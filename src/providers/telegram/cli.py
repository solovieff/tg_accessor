"""CLI arguments and configuration for the Telegram provider (group-seeker CLI)."""

import argparse
import os
import sys
from typing import Tuple


def parse_args():
    parser = argparse.ArgumentParser(
        description="Telegram Group Message Purger, Context Exporter, and Analyzer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "group_id",
        nargs="?",
        default=None,
        help="Telegram group ID (e.g. -1001234567890), username (@groupname), or join link",
    )
    parser.add_argument(
        "--output-csv",
        "-o",
        default="group_dialogue_export.csv",
        help="Path to CSV file where conversation context will be saved",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum number of recent messages to scan (default: all messages)",
    )
    parser.add_argument(
        "--dump-all",
        action="store_true",
        default=True,
        help="Export all group messages (not only target user dialogues)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete target user messages after successful CSV export",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate deletion without actually removing messages",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Run group activity & dialogue statistics",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Target user ID to delete/export (defaults to TELEGRAM_USER_ID from .env)",
    )

    # Per-user & thread segmentation flags
    parser.add_argument(
        "--split-users",
        action="store_true",
        help="Split exported CSV into dialogues and dossiers for EACH user in the group",
    )
    parser.add_argument(
        "--split-threads",
        action="store_true",
        help="Split exported CSV into conversational threads / discussion sessions",
    )
    parser.add_argument(
        "--min-user-messages",
        type=int,
        default=3,
        help="Minimum messages written by a user to generate their dossier (filters out passersby with 1-2 msgs)",
    )
    parser.add_argument(
        "--min-thread-messages",
        type=int,
        default=3,
        help="Minimum messages in a thread to consider it a meaningful discussion",
    )
    parser.add_argument(
        "--export-md",
        action="store_true",
        help="Export segmented dialogues / threads as readable Markdown files",
    )
    parser.add_argument(
        "--output-dir",
        default="exported_dialogues",
        help="Directory to store split user/thread dialogue files",
    )
    parser.add_argument(
        "--from-csv",
        help="Run offline segmentation/analysis directly from an existing CSV file without connecting to Telegram",
    )

    return parser.parse_args()


def get_credentials(cli_user_id: int | None) -> Tuple[int, str, int]:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    user_id = cli_user_id or os.getenv("TELEGRAM_USER_ID")

    missing = []
    if not api_id:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not user_id:
        missing.append("TELEGRAM_USER_ID")

    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    try:
        return int(api_id), api_hash, int(user_id)
    except (ValueError, TypeError):
        print("Error: TELEGRAM_API_ID and TELEGRAM_USER_ID must be numbers", file=sys.stderr)
        sys.exit(1)

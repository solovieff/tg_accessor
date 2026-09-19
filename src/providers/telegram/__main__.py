"""Main executable entry point for the Telegram provider."""

import asyncio
import csv
import logging
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

from common.segmenter import (
    DialogueClusterer,
    export_dialogue_to_markdown,
    export_user_dossier_markdown,
    load_exported_csv,
    sanitize_filename,
)

from .analyzer import GroupAnalyzer
from .cli import get_credentials, parse_args
from .seeker import GroupSeeker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("group_seeker")


def handle_segmentation(
    csv_path: Path,
    output_dir: Path,
    split_users: bool,
    split_threads: bool,
    export_md: bool,
    min_user_messages: int = 3,
    min_thread_messages: int = 3,
):
    """Processes dialogue segmentation from CSV file into organized user/thread directories."""
    if not csv_path.exists():
        logger.error(f"CSV file not found: {csv_path}")
        return

    logger.info(f"Loading dialogue from: {csv_path}...")
    messages = load_exported_csv(csv_path)
    if not messages:
        logger.warning("No messages loaded from CSV.")
        return

    clusterer = DialogueClusterer(messages)
    output_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "message_id", "date", "sender_id", "sender_name",
        "is_target_user", "context_relation", "reply_to_msg_id",
        "media_type", "views", "forwards", "text",
    ]

    # 1. Per-User dossiers & context: separate folder for each user!
    if split_users:
        users_root_dir = output_dir / "users"
        users_root_dir.mkdir(parents=True, exist_ok=True)
        user_dialogues = clusterer.extract_all_users_dialogues(min_user_messages=min_user_messages)
        logger.info(f"Extracted dossiers for {len(user_dialogues)} active users (with >= {min_user_messages} msgs).")

        for user_name, u_msgs in user_dialogues.items():
            safe_name = sanitize_filename(user_name)
            user_folder = users_root_dir / safe_name
            user_folder.mkdir(parents=True, exist_ok=True)

            csv_file = user_folder / "dialogue_context.csv"
            with open(csv_file, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pm in u_msgs:
                    writer.writerow({
                        "message_id": pm.message_id,
                        "date": pm.date_str,
                        "sender_id": pm.sender_id,
                        "sender_name": pm.sender_name,
                        "is_target_user": pm.is_target_user,
                        "context_relation": pm.context_relation,
                        "reply_to_msg_id": pm.reply_to_msg_id,
                        "media_type": pm.media_type,
                        "views": pm.views,
                        "forwards": pm.forwards,
                        "text": pm.text,
                    })

            if export_md:
                md_file = user_folder / "dossier.md"
                export_user_dossier_markdown(
                    user_name=user_name,
                    messages=u_msgs,
                    output_path=md_file,
                )

        logger.info(f"User folders created in: {users_root_dir.resolve()}")

    # 2. General threads / Discussion sessions: organized under threads/
    if split_threads:
        threads_dir = output_dir / "threads"
        threads_csv_dir = threads_dir / "csv"
        threads_md_dir = threads_dir / "markdown"
        threads_csv_dir.mkdir(parents=True, exist_ok=True)
        if export_md:
            threads_md_dir.mkdir(parents=True, exist_ok=True)

        threads = clusterer.extract_threads(min_messages=min_thread_messages, min_participants=2)
        logger.info(f"Extracted {len(threads)} discussion threads (with >= {min_thread_messages} msgs and >= 2 participants).")

        for idx, th in enumerate(threads, 1):
            start_date = th[0].date_dt.strftime("%Y%m%d_%H%M") if th[0].date_dt else f"thread_{idx}"
            th_file = threads_csv_dir / f"thread_{idx:03d}_{start_date}.csv"
            with open(th_file, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pm in th:
                    writer.writerow({
                        "message_id": pm.message_id,
                        "date": pm.date_str,
                        "sender_id": pm.sender_id,
                        "sender_name": pm.sender_name,
                        "is_target_user": pm.is_target_user,
                        "context_relation": pm.context_relation,
                        "reply_to_msg_id": pm.reply_to_msg_id,
                        "media_type": pm.media_type,
                        "views": pm.views,
                        "forwards": pm.forwards,
                        "text": pm.text,
                    })

            if export_md:
                md_file = threads_md_dir / f"thread_{idx:03d}_{start_date}.md"
                participants = ", ".join(sorted({m.sender_name for m in th}))
                export_dialogue_to_markdown(
                    title=f"Thread #{idx:03d} ({th[0].date_str}) | Participants: {participants}",
                    messages=th,
                    output_path=md_file,
                )

        logger.info(f"Threads saved to: {threads_dir.resolve()}")


async def main_async():
    load_dotenv()
    args = parse_args()

    # Offline mode: process existing CSV without logging into Telegram
    if args.from_csv:
        csv_path = Path(args.from_csv)
        out_dir = Path(args.output_dir)
        split_users = args.split_users or not args.split_threads
        split_threads = args.split_threads or True
        export_md = args.export_md or True
        handle_segmentation(
            csv_path,
            out_dir,
            split_users=split_users,
            split_threads=split_threads,
            export_md=export_md,
            min_user_messages=args.min_user_messages,
            min_thread_messages=args.min_thread_messages,
        )
        return

    if not args.group_id:
        logger.error("Please provide a group_id or use --from-csv <path_to_csv>")
        return

    api_id, api_hash, user_id = get_credentials(args.user_id)

    session_name = "group_seeker_user"
    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()

    me = await client.get_me()
    logger.info("Logged in as: %s (ID: %s)", getattr(me, "first_name", "User"), me.id)
    if user_id != me.id:
        logger.warning("Target user ID (%s) differs from logged-in session account (%s)", user_id, me.id)

    seeker = GroupSeeker(client=client, target_user_id=user_id)

    try:
        group_peer = await seeker.resolve_peer(args.group_id)
        peer_title = getattr(group_peer, "title", str(args.group_id))
        logger.info("Target group resolved: '%s'", peer_title)
    except Exception as e:
        logger.error("Could not resolve group peer '%s': %s", args.group_id, e)
        await client.disconnect()
        return

    # Download messages
    all_messages, target_messages, export_records = await seeker.collect_messages(
        group_peer=group_peer,
        message_limit=args.limit,
        dump_all=args.dump_all,
    )

    logger.info("Total messages downloaded: %d", len(all_messages))
    logger.info("Found %d messages sent by target user.", len(target_messages))

    csv_path = seeker.export_to_csv(export_records.values(), args.output_csv)
    logger.info("Export finished: %s", Path(csv_path).resolve())

    if args.analyze:
        logger.info("\n========== GROUP ANALYTICS REPORT ==========")
        analysis = GroupAnalyzer.analyze_messages(all_messages, target_user_id=user_id)
        logger.info("Total scanned messages: %d", analysis["total_messages"])
        logger.info("--- Top 10 Active Users ---")
        for rank, (u_name, u_count) in enumerate(analysis["top_users"], 1):
            logger.info("  %2d. %s: %d messages", rank, u_name, u_count)

        logger.info("--- Media Distribution ---")
        for media_t, m_count in analysis["media_distribution"].items():
            logger.info("  - %s: %d", media_t, m_count)

        if analysis["target_user_stats"]:
            ts = analysis["target_user_stats"]
            logger.info("--- Target User Activity ---")
            logger.info("  - Messages: %d", ts["messages"])
            logger.info("  - Replies sent: %d", ts["replies_sent"])
        logger.info("============================================\n")

    # Segment into per-user dossiers and threads
    out_dir = Path(args.output_dir)
    handle_segmentation(
        Path(csv_path),
        out_dir,
        split_users=True,
        split_threads=True,
        export_md=args.export_md or True,
        min_user_messages=args.min_user_messages,
        min_thread_messages=args.min_thread_messages,
    )

    # Immediate deletion without confirmation prompts if --delete is set
    if args.delete:
        if not target_messages:
            logger.info("No messages from target user found to delete.")
        else:
            deleted_count = await seeker.delete_messages(
                group_peer=group_peer,
                messages=target_messages,
                dry_run=args.dry_run,
            )
            logger.info("Done. Total deleted: %d messages.", deleted_count)

    await client.disconnect()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

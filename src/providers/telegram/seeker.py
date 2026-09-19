"""Dialogue collector and message deleter for Telegram groups."""

import asyncio
import csv
import logging
from dataclasses import asdict
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.custom.message import Message

from common.models import ExportedMessageRecord

from .analyzer import extract_media_type, format_sender_name

logger = logging.getLogger(__name__)


class GroupSeeker:
    """Handles fetching messages, context assembly, CSV export, and deletion."""

    def __init__(self, client: TelegramClient, target_user_id: int):
        self.client = client
        self.target_user_id = target_user_id

    async def resolve_peer(self, chat_id_or_username: str | int) -> Any:
        try:
            return await self.client.get_entity(int(chat_id_or_username))
        except (ValueError, TypeError):
            return await self.client.get_entity(chat_id_or_username)

    async def collect_messages(
        self,
        group_peer: Any,
        message_limit: Optional[int] = None,
        dump_all: bool = True,
    ) -> Tuple[List[Message], List[Message], Dict[int, ExportedMessageRecord]]:
        """
        Scans messages from group.
        If dump_all=True, exports ALL messages in group history.
        Identifies target user messages for deletion.
        """
        logger.info("Scanning messages from group...")
        all_messages: List[Message] = []
        target_messages: List[Message] = []

        count = 0
        async for msg in self.client.iter_messages(group_peer, limit=message_limit, wait_time=0.5):
            count += 1
            if count % 500 == 0:
                logger.info(f"Scanned {count} messages...")
            all_messages.append(msg)
            if msg.sender_id == self.target_user_id:
                target_messages.append(msg)
            if count % 100 == 0:
                await asyncio.sleep(0.1)

        logger.info(f"Loaded {len(all_messages)} messages total. Found {len(target_messages)} messages from target user.")

        sender_cache: Dict[int, str] = {}
        export_records: Dict[int, ExportedMessageRecord] = {}

        # Reverse to chronological order (oldest first)
        ordered_msgs = list(reversed(all_messages))
        target_msg_ids = {m.id for m in target_messages}

        for m in ordered_msgs:
            sender_id = m.sender_id
            sender_name = "Unknown"
            if m.sender:
                sender_name = format_sender_name(m.sender)
                if sender_id:
                    sender_cache[sender_id] = sender_name
            elif sender_id in sender_cache:
                sender_name = sender_cache[sender_id]
            elif sender_id:
                sender_name = f"ID_{sender_id}"

            is_target = (sender_id == self.target_user_id)
            if is_target:
                relation = "target_message"
            elif m.reply_to_msg_id and m.reply_to_msg_id in target_msg_ids:
                relation = "reply_to_target"
            else:
                relation = "group_message"

            date_str = m.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if m.date else ""
            export_records[m.id] = ExportedMessageRecord(
                message_id=m.id,
                date=date_str,
                sender_id=sender_id,
                sender_name=sender_name,
                is_target_user=is_target,
                text=m.raw_text or "",
                reply_to_msg_id=m.reply_to_msg_id,
                context_relation=relation,
                media_type=extract_media_type(m),
                views=getattr(m, "views", None),
                forwards=getattr(m, "forwards", None),
            )

        return all_messages, target_messages, export_records

    def export_to_csv(self, records: Iterable[ExportedMessageRecord], filepath: str | Path) -> Path:
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
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
        records_list = list(records)
        with open(filepath, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for rec in records_list:
                writer.writerow(asdict(rec))
        logger.info(f"Successfully exported {len(records_list)} messages to {filepath}")
        return filepath

    async def delete_messages(
        self,
        group_peer: Any,
        messages: List[Message],
        batch_size: int = 100,
        dry_run: bool = False,
    ) -> int:
        msg_ids = [m.id for m in messages]
        total = len(msg_ids)
        if dry_run:
            logger.info(f"[DRY-RUN] Would delete {total} messages.")
            return total
        logger.info(f"Deleting {total} messages in batches of {batch_size} (without confirmation prompts)...")
        deleted_count = 0
        for i in range(0, total, batch_size):
            batch = msg_ids[i : i + batch_size]
            try:
                await self.client.delete_messages(group_peer, batch)
                deleted_count += len(batch)
                logger.info(f"Deleted {deleted_count}/{total} messages...")
                await asyncio.sleep(1.0)
            except FloodWaitError as e:
                logger.warning(f"Rate limit hit! Sleeping for {e.seconds} seconds...")
                await asyncio.sleep(e.seconds + 1)
                await self.client.delete_messages(group_peer, batch)
                deleted_count += len(batch)
            except Exception as e:
                logger.error(f"Error deleting batch: {e}")
        return deleted_count

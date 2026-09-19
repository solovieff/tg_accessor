"""Analytics module for Telegram groups."""

from collections import defaultdict
from typing import Any, Dict, List, Optional
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, Chat, User


def format_sender_name(sender: Any) -> str:
    """Format sender entity to a readable string."""
    if isinstance(sender, User) or (hasattr(sender, "first_name") and hasattr(sender, "last_name")):
        parts = [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""]
        full = " ".join(p for p in parts if p).strip()
        username = getattr(sender, "username", None)
        if username:
            return f"{full} (@{username})" if full else f"@{username}"
        return full or f"User_{getattr(sender, 'id', 'unknown')}"
    elif isinstance(sender, (Channel, Chat)) or hasattr(sender, "title"):
        title = getattr(sender, "title", "")
        username = getattr(sender, "username", None)
        if username:
            return f"{title} (@{username})" if title else f"@{username}"
        return title or f"Peer_{getattr(sender, 'id', 'unknown')}"
    return "Unknown"


def extract_media_type(message: Message) -> Optional[str]:
    """Detect media type of a message."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.sticker:
        return "sticker"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    if message.poll:
        return "poll"
    if message.contact:
        return "contact"
    if message.geo:
        return "geo"
    return None


class GroupAnalyzer:
    """Provides analytical insights on group discussions."""

    @staticmethod
    def analyze_messages(messages: List[Message], target_user_id: Optional[int] = None) -> Dict[str, Any]:
        """Calculates statistics on top users, activity by hour, and media types."""
        user_message_counts: Dict[str, int] = defaultdict(int)
        hourly_distribution: Dict[int, int] = defaultdict(int)
        media_distribution: Dict[str, int] = defaultdict(int)
        target_stats = {"messages": 0, "replies_sent": 0}

        for m in messages:
            sender_name = format_sender_name(m.sender) if m.sender else f"ID_{m.sender_id}"
            user_message_counts[sender_name] += 1

            if m.date:
                hourly_distribution[m.date.hour] += 1

            media_type = extract_media_type(m) or "text"
            media_distribution[media_type] += 1

            if target_user_id and m.sender_id == target_user_id:
                target_stats["messages"] += 1
                if m.reply_to_msg_id:
                    target_stats["replies_sent"] += 1

        top_users = sorted(user_message_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "total_messages": len(messages),
            "top_users": top_users,
            "hourly_distribution": dict(sorted(hourly_distribution.items())),
            "media_distribution": dict(sorted(media_distribution.items(), key=lambda x: x[1], reverse=True)),
            "target_user_stats": target_stats if target_user_id else None,
        }

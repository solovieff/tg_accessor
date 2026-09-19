"""Data structures for group message extraction and export."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ExportedMessageRecord:
    message_id: int
    date: str
    sender_id: Optional[int]
    sender_name: str
    is_target_user: bool
    text: str
    reply_to_msg_id: Optional[int]
    context_relation: str  # target_message | replied_to_by_target | reply_to_target | surrounding_context
    media_type: Optional[str] = None
    views: Optional[int] = None
    forwards: Optional[int] = None

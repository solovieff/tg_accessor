import csv
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from common.models import ExportedMessageRecord
from providers.telegram.seeker import GroupSeeker
from providers.telegram.analyzer import GroupAnalyzer, format_sender_name, extract_media_type


def test_export_to_csv(tmp_path: Path):
    mock_client = MagicMock()
    seeker = GroupSeeker(client=mock_client, target_user_id=123)
    
    records = [
        ExportedMessageRecord(
            message_id=10,
            date="2026-09-18 12:00:00 UTC",
            sender_id=456,
            sender_name="Alice",
            is_target_user=False,
            text="First message from Alice",
            reply_to_msg_id=None,
            context_relation="surrounding_context",
        ),
        ExportedMessageRecord(
            message_id=11,
            date="2026-09-18 12:01:00 UTC",
            sender_id=123,
            sender_name="TargetUser",
            is_target_user=True,
            text="My reply",
            reply_to_msg_id=10,
            context_relation="target_message",
        ),
    ]

    csv_file = tmp_path / "test_dialogue.csv"
    seeker.export_to_csv(records, csv_file)

    assert csv_file.exists()
    with open(csv_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert reader[0]["message_id"] == "10"
        assert reader[0]["sender_name"] == "Alice"
        assert reader[1]["message_id"] == "11"
        assert reader[1]["is_target_user"] == "True"
        assert reader[1]["reply_to_msg_id"] == "10"


def test_analyzer():
    class DummyUser:
        def __init__(self, id, first_name, username=None):
            self.id = id
            self.first_name = first_name
            self.last_name = ""
            self.username = username

    class DummyMessage:
        def __init__(self, id, sender_id, text, reply_to_msg_id=None, hour=14):
            self.id = id
            self.sender_id = sender_id
            self.sender = DummyUser(sender_id, f"User_{sender_id}")
            self.raw_text = text
            self.reply_to_msg_id = reply_to_msg_id
            self.date = datetime(2026, 9, 18, hour, 0, tzinfo=timezone.utc)
            self.photo = None
            self.video = None
            self.sticker = None
            self.voice = None
            self.audio = None
            self.document = None
            self.poll = None
            self.contact = None
            self.geo = None

    messages = [
        DummyMessage(1, 100, "Hello"),
        DummyMessage(2, 200, "Hi there", reply_to_msg_id=1),
        DummyMessage(3, 100, "How are you?"),
    ]

    report = GroupAnalyzer.analyze_messages(messages, target_user_id=100)
    assert report["total_messages"] == 3
    assert len(report["top_users"]) == 2
    assert report["top_users"][0][1] == 2  # User 100 has 2 messages
    assert report["target_user_stats"]["messages"] == 2
    assert report["target_user_stats"]["replies_sent"] == 0

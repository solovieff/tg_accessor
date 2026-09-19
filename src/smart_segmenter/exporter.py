"""Markdown/CSV export for smart-segmented threads."""

import csv
import re
from pathlib import Path
from typing import List, Optional

from common.segmenter import ParsedMessage

from .classifier import ThreadInsight

FIELDNAMES = [
    "message_id", "date", "sender_id", "sender_name",
    "reply_to_msg_id", "media_type", "text",
]


def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|@]', "", name)
    clean = clean.replace(" ", "_").strip("._")
    return clean or "thread"


def export_thread_csv(thread: List[ParsedMessage], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for m in thread:
            writer.writerow({
                "message_id": m.message_id,
                "date": m.date_str,
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "reply_to_msg_id": m.reply_to_msg_id,
                "media_type": m.media_type,
                "text": m.text,
            })


def export_thread_markdown(
    thread: List[ParsedMessage],
    path: Path,
    index: int,
    insight: Optional[ThreadInsight] = None,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    participants = sorted({m.sender_name for m in thread})
    start = thread[0].date_str if thread else "N/A"
    end = thread[-1].date_str if thread else "N/A"

    heat_emoji = {
        "calm": "🟢",
        "mild_disagreement": "🟡",
        "clear_dispute": "🟠",
        "hostile": "🔴",
    }

    with open(path, mode="w", encoding="utf-8") as f:
        f.write(f"# Разговор #{index}\n\n")
        f.write(f"- **Период:** {start} — {end}\n")
        f.write(f"- **Сообщений:** {len(thread)}\n")
        f.write(f"- **Участники:** {', '.join(participants)}\n")
        if insight is not None:
            emoji = heat_emoji.get(insight.heat_label, "")
            f.write(
                f"- **Накал:** {emoji} {insight.heat_label} (score={insight.heat_score:.2f})\n"
            )
            if insight.more_convincing_confidence is None:
                f.write(
                    "- **Кто убедительнее в споре:** "
                    "н/д (спокойный разговор без спора)\n"
                )
            elif insight.more_convincing_participant is None:
                f.write(
                    f"- **Кто убедительнее в споре:** "
                    f"не удалось определить / ничья "
                    f"(confidence={insight.more_convincing_confidence:.2f})\n"
                )
            else:
                f.write(
                    f"- **Кто убедительнее в споре:** "
                    f"{insight.more_convincing_participant} "
                    f"(confidence={insight.more_convincing_confidence:.2f})\n"
                )
        f.write("\n---\n\n")

        for m in thread:
            reply_info = f" *(ответ на #{m.reply_to_msg_id})*" if m.reply_to_msg_id else ""
            media_info = f" `[{m.media_type}]`" if m.media_type else ""
            f.write(f"### **{m.sender_name}** | {m.date_str or 'N/A'} (ID: {m.message_id}){reply_info}{media_info}\n")
            body = m.text.strip() or f"*(Вложение: {m.media_type or 'media'})*"
            f.write(f"{body}\n\n---\n\n")

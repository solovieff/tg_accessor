"""Module for segmenting exported dialogue CSV into threads, per-user dossiers, and readable formats."""

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ParsedMessage:
    message_id: int
    date_str: str
    date_dt: Optional[datetime]
    sender_id: str
    sender_name: str
    is_target_user: bool
    context_relation: str
    reply_to_msg_id: Optional[int]
    media_type: str
    views: str
    forwards: str
    text: str


def load_exported_csv(csv_path: str | Path) -> List[ParsedMessage]:
    messages = []
    with open(csv_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mid = int(row["message_id"])
            date_s = row.get("date", "")
            dt = None
            if date_s:
                try:
                    clean_date = date_s.replace(" UTC", "")
                    dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    dt = None

            reply_id = None
            raw_reply = row.get("reply_to_msg_id", "").strip()
            if raw_reply and raw_reply.isdigit():
                reply_id = int(raw_reply)

            messages.append(
                ParsedMessage(
                    message_id=mid,
                    date_str=date_s,
                    date_dt=dt,
                    sender_id=row.get("sender_id", ""),
                    sender_name=row.get("sender_name", "Unknown"),
                    is_target_user=row.get("is_target_user", "").lower() == "true",
                    context_relation=row.get("context_relation", ""),
                    reply_to_msg_id=reply_id,
                    media_type=row.get("media_type", ""),
                    views=row.get("views", ""),
                    forwards=row.get("forwards", ""),
                    text=row.get("text", "") or "",
                )
            )
    messages.sort(key=lambda m: (m.date_dt or datetime.min, m.message_id))
    return messages


class DialogueClusterer:
    def __init__(self, messages: List[ParsedMessage], session_gap_minutes: int = 15):
        self.messages = messages
        self.session_gap_minutes = session_gap_minutes
        self.by_id: Dict[int, ParsedMessage] = {m.message_id: m for m in messages}

    def extract_threads(
        self,
        min_messages: int = 2,
        min_participants: int = 1,
    ) -> List[List[ParsedMessage]]:
        visited: Set[int] = set()
        threads: List[List[ParsedMessage]] = []

        adj = defaultdict(set)
        for m in self.messages:
            if m.reply_to_msg_id and m.reply_to_msg_id in self.by_id:
                adj[m.message_id].add(m.reply_to_msg_id)
                adj[m.reply_to_msg_id].add(m.message_id)

        for m in self.messages:
            if m.message_id in visited:
                continue

            if adj[m.message_id]:
                component = []
                queue = [m.message_id]
                visited.add(m.message_id)
                while queue:
                    curr = queue.pop(0)
                    component.append(self.by_id[curr])
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(neighbor)
                component.sort(key=lambda x: (x.date_dt or datetime.min, x.message_id))
                threads.append(component)

        session: List[ParsedMessage] = []
        for m in self.messages:
            if m.message_id in visited:
                continue
            if not session:
                session.append(m)
            else:
                prev = session[-1]
                gap = (
                    (m.date_dt - prev.date_dt).total_seconds() / 60
                    if (m.date_dt and prev.date_dt)
                    else 0
                )
                if gap <= self.session_gap_minutes:
                    session.append(m)
                else:
                    threads.append(session)
                    session = [m]
            visited.add(m.message_id)

        if session:
            threads.append(session)

        filtered = []
        for th in threads:
            if len(th) < min_messages:
                continue
            participants = {m.sender_name for m in th}
            if len(participants) < min_participants:
                continue
            filtered.append(th)

        filtered.sort(key=lambda th: th[0].date_dt or datetime.min)
        return filtered

    def extract_focused_user_dialogue(
        self,
        user_name: str,
        context_window: int = 3,
        drop_trivial_reactions: bool = True,
    ) -> List[ParsedMessage]:
        """A tight, local-context alternative to extract_all_users_dialogues().

        extract_all_users_dialogues() (and thread-based filtering) pulls in
        entire session-clustered threads, which in a busy chat can mean "the
        whole day's chat activity" once gaps are under session_gap_minutes.
        That's not useful for focusing on one user's actual arguments.

        Instead, this keeps only a LOCAL window around each of the user's own
        messages, by chronological position (not by session):
          1. The user's own messages.
          2. Reply chains: whatever they directly replied to, and whoever
             directly replied to them (one hop each way).
          3. +/- context_window neighboring messages (by chronological index)
             around each of the user's own messages, so short back-and-forth
             around their reply is visible without dragging in the whole day.
        Optionally drops trivial no-text sticker reactions.
        """
        own_indices = [i for i, m in enumerate(self.messages) if m.sender_name == user_name]
        keep_ids: Set[int] = set()

        own_ids = {self.messages[i].message_id for i in own_indices}
        keep_ids.update(own_ids)

        for i in own_indices:
            lo = max(0, i - context_window)
            hi = min(len(self.messages), i + context_window + 1)
            for j in range(lo, hi):
                keep_ids.add(self.messages[j].message_id)

        for m in self.messages:
            if m.sender_name == user_name and m.reply_to_msg_id and m.reply_to_msg_id in self.by_id:
                keep_ids.add(m.reply_to_msg_id)
            if m.reply_to_msg_id and m.reply_to_msg_id in own_ids:
                keep_ids.add(m.message_id)

        result = [self.by_id[mid] for mid in keep_ids if mid in self.by_id]

        if drop_trivial_reactions:
            result = [
                m
                for m in result
                if m.sender_name == user_name or m.text.strip() or m.media_type not in {"sticker", ""}
            ]

        result.sort(key=lambda x: (x.date_dt or datetime.min, x.message_id))
        return result

    def extract_all_users_dialogues(self, min_user_messages: int = 3) -> Dict[str, List[ParsedMessage]]:
        all_threads = self.extract_threads(min_messages=2)
        user_msg_ids: Dict[str, Set[int]] = defaultdict(set)
        user_own_counts: Dict[str, int] = defaultdict(int)

        for m in self.messages:
            user_own_counts[m.sender_name] += 1

        meaningful_users = {u for u, cnt in user_own_counts.items() if cnt >= min_user_messages}

        for user in meaningful_users:
            own_ids = {m.message_id for m in self.messages if m.sender_name == user}
            user_msg_ids[user].update(own_ids)

            for m in self.messages:
                if m.sender_name == user and m.reply_to_msg_id and m.reply_to_msg_id in self.by_id:
                    user_msg_ids[user].add(m.reply_to_msg_id)

                if m.reply_to_msg_id and m.reply_to_msg_id in own_ids:
                    user_msg_ids[user].add(m.message_id)

            for th in all_threads:
                if any(m.sender_name == user for m in th):
                    for m in th:
                        user_msg_ids[user].add(m.message_id)

        result: Dict[str, List[ParsedMessage]] = {}
        for user in meaningful_users:
            msgs = [self.by_id[mid] for mid in user_msg_ids[user] if mid in self.by_id]
            msgs.sort(key=lambda x: (x.date_dt or datetime.min, x.message_id))
            result[user] = msgs

        return result


def export_user_dossier_markdown(
    user_name: str,
    messages: List[ParsedMessage],
    output_path: Path,
    session_gap_minutes: int = 30,
):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sub_clusterer = DialogueClusterer(messages, session_gap_minutes=session_gap_minutes)
    disputes = sub_clusterer.extract_threads(min_messages=2)

    total_context_msgs = len(messages)
    user_own_msgs = [m for m in messages if m.sender_name == user_name]
    other_msgs = [m for m in messages if m.sender_name != user_name]

    first_date = messages[0].date_str if messages else "N/A"
    last_date = messages[-1].date_str if messages else "N/A"

    partner_counts = defaultdict(int)
    for m in other_msgs:
        partner_counts[m.sender_name] += 1
    top_partners = sorted(partner_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    with open(output_path, mode="w", encoding="utf-8") as f:
        f.write(f"# Досье участника: {user_name}\n\n")
        f.write("## Сводка\n")
        f.write(f"- **Пользователь:** {user_name}\n")
        f.write(f"- **Период активности:** {first_date} — {last_date}\n")
        f.write(f"- **Сообщений пользователя в выгрузке:** {len(user_own_msgs)}\n")
        f.write(f"- **Сообщений в контексте диалогов:** {total_context_msgs}\n")
        f.write(f"- **Количество отдельных раундов / споров:** {len(disputes)}\n")
        if top_partners:
            f.write("- **Основные собеседники / оппоненты:**\n")
            for p_name, p_cnt in top_partners:
                f.write(f"  - {p_name}: {p_cnt} реплик\n")
        f.write("\n---\n\n")

        for idx, disp in enumerate(disputes, 1):
            start_time = disp[0].date_str or "N/A"
            participants = ", ".join(sorted({m.sender_name for m in disp}))
            f.write(f"## Раунд / Спор #{idx} ({start_time}, сообщений: {len(disp)})\n")
            f.write(f"*Участники: {participants}*\n\n")
            for m in disp:
                is_curr_user = (m.sender_name == user_name)
                author_badge = f"**>>> [{m.sender_name}] <<<**" if is_curr_user else f"**{m.sender_name}**"
                reply_info = f" *(ответ на #{m.reply_to_msg_id})*" if m.reply_to_msg_id else ""
                media_info = f" `[{m.media_type}]`" if m.media_type else ""
                f.write(f"> {author_badge} ({m.date_str or 'N/A'}{reply_info}{media_info}):\n")
                body = m.text.strip() or f"*(Вложение: {m.media_type or 'media'})*"
                for line in body.splitlines():
                    f.write(f"> {line}\n")
                f.write(">\n")
            f.write("\n---\n\n")


def export_dialogue_to_markdown(
    title: str,
    messages: List[ParsedMessage],
    output_path: Path,
):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, mode="w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"*Всего сообщений: {len(messages)}*\n\n---\n\n")
        for m in messages:
            author_badge = f"**{m.sender_name}**"
            date_info = m.date_str or "N/A"
            reply_info = f" *(ответ на #{m.reply_to_msg_id})*" if m.reply_to_msg_id else ""
            media_info = f" `[{m.media_type}]`" if m.media_type else ""
            f.write(f"### {author_badge} | {date_info} (ID: {m.message_id}){reply_info}{media_info}\n")
            t = m.text.strip() or f"*(Вложение: {m.media_type or 'media'})*"
            f.write(f"{t}\n\n---\n\n")


def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|@]', "", name)
    clean = clean.replace(" ", "_").strip("._")
    return clean or "unknown_user"

#!/usr/bin/env python3
"""Build a one-page analytical profile from smart-segmenter thread output.

smart-segmenter writes one Markdown file per conversation thread (with a
heat rating and a "who was more convincing" judgment in its header), but
nothing that summarizes across hundreds/thousands of those files for one
person. This script reads all the already-generated thread Markdown files
in a smart-segmenter output directory and writes a single PROFILE.md
summarizing:
  - How many conversations, broken down by heat level (calm/mild/dispute/hostile).
  - How often the target user was judged more convincing vs. their opponents,
    vs. no clear winner.
  - Top conflict opponents (who they clash with most, weighted towards
    heated threads).
  - The most heated threads, linked for quick access.

This is pure local text parsing of the Markdown headers already produced by
smart-segmenter's export_thread_markdown() -- no TypeSafe API calls, so it's
free and instant to (re)run.

Usage:
    uv run python scripts/build_user_profile.py \\
        --dir smart_exported_dialogues/alice \\
        --user "Alice (@alice_handle)"
"""

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Optional

HEAT_ORDER = ["calm", "mild_disagreement", "clear_dispute", "hostile"]
HEAT_EMOJI = {"calm": "\U0001F7E2", "mild_disagreement": "\U0001F7E1", "clear_dispute": "\U0001F7E0", "hostile": "\U0001F534"}
HEADER_RE = re.compile(
    r"# Thread #(?P<index>\d+)\n\n"
    r"- \*\*Period:\*\* (?P<start>[^\u2014]+)\u2014 (?P<end>[^\n]+)\n"
    r"- \*\*Messages:\*\* (?P<count>\d+)\n"
    r"- \*\*Participants:\*\* (?P<participants>[^\n]+)\n"
    r"- \*\*Heat:\*\* \S+ (?P<heat_label>\w+) \(score=(?P<heat_score>[\d.]+)\)\n"
    r"- \*\*More convincing side:\*\* (?P<convincing>[^\n]+)\n",
)

CONVINCING_NA_RE = re.compile(r"^n/a")
CONVINCING_TIE_RE = re.compile(r"^undetermined")
CONVINCING_NAMED_RE = re.compile(r"^(?P<name>.+?) \(confidence=(?P<confidence>[\d.]+)\)$")


def parse_thread_file(path: Path) -> Optional[dict]:
    text = path.read_text(encoding="utf-8")
    m = HEADER_RE.search(text)
    if not m:
        return None
    participants = [p.strip() for p in m.group("participants").split(",")]

    convincing_raw = m.group("convincing").strip()
    convincing_participant = None
    if CONVINCING_NA_RE.match(convincing_raw):
        convincing_status = "n/a"
    elif CONVINCING_TIE_RE.match(convincing_raw):
        convincing_status = "tie"
    else:
        named = CONVINCING_NAMED_RE.match(convincing_raw)
        if named:
            convincing_status = "named"
            convincing_participant = named.group("name").strip()
        else:
            convincing_status = "unknown"

    return {
        "path": path,
        "index": int(m.group("index")),
        "start": m.group("start").strip(),
        "end": m.group("end").strip(),
        "count": int(m.group("count")),
        "participants": participants,
        "heat_label": m.group("heat_label"),
        "heat_score": float(m.group("heat_score")),
        "convincing_status": convincing_status,
        "convincing_participant": convincing_participant,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", required=True, help="A smart-segmenter output dir (containing markdown/*.md).")
    parser.add_argument("--user", required=True, help="The target user's exact sender_name, e.g. 'Alice (@alice_handle)'.")
    parser.add_argument("--top-opponents", type=int, default=8, help="How many top opponents to list (default: 8).")
    parser.add_argument("--top-heated", type=int, default=10, help="How many hottest threads to link (default: 10).")
    args = parser.parse_args()

    md_dir = Path(args.dir) / "markdown"
    if not md_dir.is_dir():
        raise SystemExit(f"No markdown/ directory found under {args.dir}")

    threads = []
    for path in sorted(md_dir.glob("*.md")):
        parsed = parse_thread_file(path)
        if parsed:
            threads.append(parsed)

    if not threads:
        raise SystemExit(f"No parseable thread files found in {md_dir}")

    user_threads = [t for t in threads if args.user in t["participants"]]
    if not user_threads:
        raise SystemExit(
            f"No threads found with participant '{args.user}'. "
            f"Check the exact sender_name spelling (case/handle included)."
        )

    heat_counts = Counter(t["heat_label"] for t in user_threads)
    convincing_counts = Counter(t["convincing_status"] for t in user_threads)
    user_won = sum(
        1 for t in user_threads
        if t["convincing_status"] == "named" and t["convincing_participant"] == args.user
    )
    opponent_won = sum(
        1 for t in user_threads
        if t["convincing_status"] == "named" and t["convincing_participant"] != args.user
    )

    opponent_counts = Counter()
    opponent_heated_counts = Counter()
    for t in user_threads:
        for p in t["participants"]:
            if p == args.user:
                continue
            opponent_counts[p] += 1
            if t["heat_label"] in ("clear_dispute", "hostile"):
                opponent_heated_counts[p] += 1

    dates = [t["start"] for t in user_threads if t["start"]] + [t["end"] for t in user_threads if t["end"]]
    first_date = min(dates) if dates else "N/A"
    last_date = max(dates) if dates else "N/A"

    heated_threads = sorted(
        user_threads, key=lambda t: t["heat_score"], reverse=True
    )[: args.top_heated]

    top_opponents = opponent_counts.most_common(args.top_opponents)

    profile_path = Path(args.dir) / "PROFILE.md"
    total = len(user_threads)
    with open(profile_path, mode="w", encoding="utf-8") as f:
        f.write(f"# Analytical profile: {args.user}\n\n")
        f.write(f"- **Period:** {first_date} — {last_date}\n")
        f.write(f"- **Total threads:** {total}\n\n")

        f.write("## Heat distribution\n\n")
        for label in HEAT_ORDER:
            count = heat_counts.get(label, 0)
            pct = (count / total * 100) if total else 0
            f.write(f"- {HEAT_EMOJI[label]} {label}: {count} ({pct:.1f}%)\n")
        f.write("\n")

        disputed_total = convincing_counts.get("named", 0) + convincing_counts.get("tie", 0)
        f.write("## Convincingness in disputes\n\n")
        if disputed_total == 0:
            f.write("*No disputes with enough heat to score this.*\n\n")
        else:
            f.write(f"- User was judged more convincing: {user_won} ")
            f.write(f"({user_won / disputed_total * 100:.0f}% of scored disputes)\n")
            f.write(f"- Opponent was more convincing: {opponent_won} ")
            f.write(f"({opponent_won / disputed_total * 100:.0f}%)\n")
            tie = convincing_counts.get("tie", 0)
            f.write(f"- Tie / undetermined: {tie} ({tie / disputed_total * 100:.0f}%)\n\n")

        f.write("## Top opponents\n\n")
        if not top_opponents:
            f.write("*No data.*\n\n")
        else:
            for name, count in top_opponents:
                heated = opponent_heated_counts.get(name, 0)
                f.write(f"- {name} — {count} shared threads ({heated} of them \U0001F7E0/\U0001F534)\n")
            f.write("\n")

        f.write("## Most heated threads\n\n")
        for rank, t in enumerate(heated_threads, 1):
            rel_path = f"markdown/{t['path'].name}"
            f.write(
                f"{rank}. [{t['path'].stem}]({rel_path}) — {t['count']} messages, "
                f"{HEAT_EMOJI[t['heat_label']]} {t['heat_label']} (score={t['heat_score']:.2f})\n"
            )

    print(f"Wrote {profile_path} ({total} threads analyzed).")


if __name__ == "__main__":
    main()

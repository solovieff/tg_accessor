# Telegram Accessor (`group_seeker` + `smart_segmenter`)

CLI tools for exporting Telegram group messages and their surrounding dialogue context, running group activity analytics, and smart (LLM-powered) thread segmentation.

> **Note:** `smart_segmenter` is one of the first practical examples of using
> [TypeSafe](https://typesafe.ai) (the Jev "System One" model) for a specific
> task — reconstructing conversation structure in group chats even when there
> is no explicit reply link. TypeSafe is a very young product, so treat this
> as an experimental showcase rather than a battle-tested solution.

## Features
- Export a group's full message history, or just a target user's messages plus their surrounding dialogue context (replies, mentions, nearby messages), to CSV (`UTF-8`).
- Built-in group activity analytics (`--analyze`).
- Heuristic segmentation of an exported CSV by user and by thread (`group_seeker --from-csv ...`).
- **`smart_segmenter`**: TypeSafe (Jev)-powered thread segmentation — links messages that continue the same topic/argument even without an explicit reply, and rates each thread's conflict intensity and whether it was resolved.
- **`build_user_profile.py`**: rolls up a person's already-segmented threads into a single `PROFILE.md` — heat distribution, how often they were judged more convincing, top opponents, links to their hottest threads. Pure local Markdown parsing, no extra API calls.
- Advanced/optional: deleting the target user's own messages after export (see below) — not the main point of this project.

## Setup

Fill in the environment variables in `.env` (see `.env.example`):
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — from https://my.telegram.org
- `TELEGRAM_USER_ID` — your own numeric Telegram user ID (from `@userinfobot`).
  Only needed if you want to use the optional `--delete` flag (see below).
- `TYPESAFE_API_KEY` — only required for `smart_segmenter`, get one at
  https://console.typesafe.ai/settings/keys

## Quick start

All you need to get going: a Telegram group ID/handle (in `.env` or passed on
the command line) and a `TYPESAFE_API_KEY` (see Setup above). The full flow
is: **export the group once**, then **smart-segment it** — either for
everyone at once, or focused on one person — and optionally roll the result
up into a one-page **analytical profile**.

**Step 1 — export a group's full history:**
```bash
./run.sh -1001234567890 --analyze --export-md
```
Replace `-1001234567890` with your own group ID or `@groupname`. This connects
to Telegram, downloads the entire message history, prints an activity report
(`--analyze`), and writes `exported_dialogues/group_dialogue_export.csv` (the
raw CSV that everything else below reads) plus per-user dossiers and
per-thread files as Markdown (`--export-md`).

**Step 2 (recommended default) — smart-segment one specific person's
dialogue.** Most of the time you care about one person's conversations, not
the whole group at once, and this is dramatically cheaper (fewer TypeSafe
calls) than segmenting everything. First narrow the raw export down to that
person's own messages plus their immediate reply context, then run the
smart segmenter on that slice:
```bash
uv run python scripts/build_focused_user_csv.py \
  --from-csv exported_dialogues/group_dialogue_export.csv \
  --user "Alice (@alice_handle)" \
  --output /tmp/alice_focused.csv

uv run smart-segmenter \
  --from-csv /tmp/alice_focused.csv \
  --output-dir smart_exported_dialogues/alice
```
`--user` must match a `sender_name` exactly as it appears in the exported CSV
(`Name (@handle)`, or just `Name` if they have no handle — check the CSV or
`exported_dialogues/users/` if unsure). Add `--since 2025-06-19` to restrict
to the last few months instead of the whole history (see "More examples"
below for the full focused-CSV options).

**Step 2 (alternative) — smart-segment every active user at once.** If you'd
rather get everyone's threads in one pass instead of picking a person first,
use `--split-users` on the full export directly:
```bash
uv run smart-segmenter \
  --from-csv exported_dialogues/group_dialogue_export.csv \
  --split-users --min-user-messages 10
```
This writes `smart_exported_dialogues/users/<name>/{markdown,csv}/`, one
folder per user with at least `--min-user-messages` own messages. Note
`--split-users` pulls in an *entire* thread the moment a user appears
anywhere in it, so it can balloon in cost on someone active all day — the
focused-CSV approach in Step 2 above is the cheaper, tighter option for a
single person.

Either way, `smart_segmenter` runs fully offline against Telegram (no group
access needed at this step), calls TypeSafe to link messages that continue
the same topic/argument even without an explicit reply, scores each
resulting thread's conflict intensity, and writes `markdown/*.md` and
`csv/*.csv` — one file per thread with at least `--min-thread-messages`
(default 5) messages.

**Step 3 (optional) — build a one-page analytical profile.** Once
`smart_segmenter` has written its `markdown/*.md` thread files for a person
(Step 2), `build_user_profile.py` reads all of them and rolls them up into a
single `PROFILE.md`: how many threads fall into each heat level, how often
this person was judged more convincing than their opponent (or vice versa,
or a tie), their top opponents by shared thread count, and links to their
hottest threads. This is pure local Markdown parsing — no TypeSafe calls, so
it's free and instant to (re)run after every segmenting pass:
```bash
uv run python scripts/build_user_profile.py \
  --dir smart_exported_dialogues/alice \
  --user "Alice (@alice_handle)"
```
`--user` must match the same `sender_name` used in Step 2. This writes
`smart_exported_dialogues/alice/PROFILE.md`. Optional flags:
`--top-opponents N` (default 8) and `--top-heated N` (default 10) control
how many opponents/threads are listed.

Example profile output (real run, names changed):
```markdown
# Аналитический профиль: Alice (@alice_handle)

- **Период:** 2025-06-20 05:18:45 UTC — 2026-09-18 16:34:06 UTC
- **Всего разговоров:** 1710

## Распределение по накалу

- 🟢 спокойные (calm): 216 (12.6%)
- 🟡 лёгкие разногласия (mild_disagreement): 534 (31.2%)
- 🟠 явные споры (clear_dispute): 809 (47.3%)
- 🔴 агрессивные конфликты (hostile): 151 (8.8%)

## Убедительность в спорах

- Пользователь был признан более убедительным: 329 (26% от оцененных споров)
- Оппонент был убедительнее: 841 (66%)
- Ничья / не удалось определить: 106 (8%)

## Главные оппоненты

- Bob (@bob_handle) — 437 совместных разговоров (из них 229 🟠/🔴)
- Carol (@carol_handle) — 400 совместных разговоров (из них 289 🟠/🔴)

## Самые ожесточённые разговоры

1. [thread_503_20251021_144008_UTC](markdown/thread_503_20251021_144008_UTC.md) — 85 сообщений, 🔴 hostile (score=3.00)
```

Example thread output (real run, names changed):
```markdown
# Разговор #12

- **Период:** 2025-06-24 15:19:45 UTC — 2025-06-24 18:02:01 UTC
- **Сообщений:** 31
- **Участники:** Alice, Bob, Carol, Dan, Eve, Frank
- **Накал:** 🔴 hostile (score=2.56)
- **Кто убедительнее в споре:** Bob (confidence=0.71)

---

### **Bob** | 2025-06-24 15:19:45 UTC (ID: 439709) *(ответ на #439707)*
...message text...
```
A calm thread with no real disagreement gets `**Кто убедительнее в споре:**
н/д (спокойный разговор без спора)` instead — the question is only asked when
the heat score shows an actual dispute (see `src/smart_segmenter/classifier.py`
for why this is phrased as "who was more convincing" rather than a plain
yes/no "was it resolved", which turned out to be a near-useless signal in
practice).

TypeSafe responses are cached in `.typesafe_cache.json`, so re-running the
same export does not spend extra tokens.

> Exported dialogues (`exported_dialogues/`, `smart_exported_dialogues/`) contain
> real private messages and must never be committed to git — both directories
> are already in `.gitignore`.

## More `smart_segmenter` examples

**Skip conflict scoring (cheaper, linking only):**
```bash
uv run smart-segmenter --from-csv exported_dialogues/group_dialogue_export.csv --skip-insights
```

**Force a fresh run, ignoring the cache** (e.g. after tuning thresholds and
wanting to re-judge everything):
```bash
uv run smart-segmenter --from-csv exported_dialogues/group_dialogue_export.csv --no-cache
```

**Tune throughput.** TypeSafe's published limit is 250,000 tokens/sec and
1,200 requests/minute. `smart_segmenter` sends batches concurrently
(`--concurrency`, default `10` workers) through a shared rate limiter
(`--max-requests-per-minute`, default `500` — a conservative fraction of the
published cap). Raise both together to go faster, or lower them if you'd
rather stay well under the limit:
```bash
uv run smart-segmenter --from-csv exported_dialogues/group_dialogue_export.csv --concurrency 15 --max-requests-per-minute 800
```

**All `build_focused_user_csv.py` options** (used in Step 2 of Quick start
above) — `--since`/`--until` restrict to a date range, `--context-window`
controls how many neighboring messages are kept around each of the user's
own messages (default 3):
```bash
uv run python scripts/build_focused_user_csv.py \
  --from-csv exported_dialogues/group_dialogue_export.csv \
  --user "Alice (@alice_handle)" \
  --since 2025-06-19 \
  --until 2025-09-19 \
  --context-window 2 \
  --output /tmp/alice_focused_3mo.csv
```

## Advanced: deleting your own messages

`group_seeker` can optionally delete the target user's own messages from a
group right after exporting them, via `--delete` (add `--dry-run` first to
just see how many would be removed, with no changes made). This requires
`TELEGRAM_USER_ID` to be set and is an advanced, opt-in feature — most users
only need the export/analysis/segmentation flow above.
```bash
./run.sh -1001234567890 --delete --dry-run   # preview only
./run.sh -1001234567890 --delete             # actually deletes, no extra prompts
```

## The exported CSV format (the provider contract)

Every chat platform this project can talk to (currently only Telegram) exports
the same message-level CSV shape, defined once in the provider-agnostic core
and consumed by both the heuristic segmenter and `smart_segmenter`. This is
the contract a new provider (Discord, Slack, ...) would need to produce to
plug into the rest of the toolkit for free.

| Column | Type | Meaning |
| --- | --- | --- |
| `message_id` | int | Unique, chronologically sortable ID within the chat |
| `date` | string | `YYYY-MM-DD HH:MM:SS UTC` |
| `sender_id` | int/string | Stable per-author identifier |
| `sender_name` | string | Human-readable author label (name and/or `@handle`) |
| `is_target_user` | bool | Whether this message was sent by the account running the export |
| `context_relation` | string | Why this row was included: `target_message`, `reply_to_target`, `replied_to_by_target`, `surrounding_context`, or `group_message` for a full dump |
| `reply_to_msg_id` | int/empty | `message_id` this message replies to, if any |
| `media_type` | string/empty | `photo`, `video`, `sticker`, `voice`, `audio`, `document`, `poll`, `contact`, `geo`, or empty for plain text |
| `views` | int/empty | View count, if the platform exposes one |
| `forwards` | int/empty | Forward/share count, if the platform exposes one |
| `text` | string | Message body (empty for pure media) |

## Architecture

- **`src/providers/telegram/`** — the only platform-specific code: talks to
  the Telegram API via Telethon (`seeker.py`), turns live Telethon message
  objects into analytics (`analyzer.py`), and handles Telegram env vars/CLI
  (`cli.py`). A future Discord or Slack integration would live next to it as
  its own `src/providers/<platform>/` package and just needs to produce the
  CSV format described above.
- **`src/common/`** — the provider-agnostic core: the CSV schema
  (`ExportedMessageRecord`) and the segmentation/dossier logic
  (`ParsedMessage`, `DialogueClusterer`, Markdown/CSV export) only ever read
  the CSV format above; they have no idea the data came from Telegram.
- **`src/smart_segmenter/`** — also fully provider-agnostic: it consumes the
  same CSV/`ParsedMessage` structures (via `common.segmenter`) and calls
  TypeSafe to refine threads and score conflict intensity. It would work
  unmodified on a Discord or Slack export that follows the same CSV contract.



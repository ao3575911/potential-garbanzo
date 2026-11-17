# Circuit Perth Bot

Self-hosted Telegram reputation, anonymous chat, scam reports and moderation. Python 3.12+, SQLite, one bot process. No paid API, AI service, public web server or database server is needed.

## Member flows

**`/report` asks exactly two questions:** the member’s @username, then the scam report. After the second answer it immediately saves the dossier record and queues a structured anonymous report to **the channel only** by default. There is no approval or confirmation step. The owner can optionally change `REPORT_DESTINATION` to `group` or `both`. Unreviewed reports are labelled as allegations.

**`/post` displays `Bot2Channel` and `Bot2Group` buttons.** After the choice, the user sends one text message. It immediately queues a fresh message headed **Anonymous chat** to that destination. Sender profiles, forwarding headers and Telegram formatting are not forwarded. Common text identifiers are removed. Photos, files, voice messages and other media are declined, so file metadata cannot leak through this flow.

**`/vouch`** collects a member and an experience description, immediately publishes a structured **Vouch** to the **channel**, and saves it in the dossier and count. **`/check @username`** returns the member’s dossier, counts, dated records, review status, distinct reporter count and observed group activity. Every public record is accessible through page buttons. **`/self`** delivers your own dossier privately.

Unknown usernames are accepted. Their records remain in a clearly marked **unverified handle dossier**, separate from Telegram ID records. A username can change owners. An association with an ID means the bot previously observed that username; it is not proof of current ownership. `/handle @username` opens handle-only records.

## Fast install with Docker

1. Create **Circuit Perth Bot** in [@BotFather](https://t.me/BotFather). Its username must end in `bot`. Keep its token private.
2. Add it as administrator to your **supergroup** and **channel**. Grant the rights listed in [Operations](docs/OPERATIONS.md).
3. Copy `.env.example` to `.env`. Enter your token, numeric owner ID, supergroup ID and channel ID locally. Never paste the token into chat or commit `.env`.

```sh
cp .env.example .env
# Edit .env with your local editor.
docker compose up -d --build
docker compose logs --tail=50 -f bot
```

Need the IDs first? With `.env.example` copied to `.env`, build and run discovery **before starting production**:

```sh
docker compose build
docker compose run --rm bot circuit-bot discover
```

Enter your token at the hidden prompt. Send `/id` privately to the bot, then in the group and channel. Discovery prints the IDs for two minutes; Ctrl+C stops it. Copy your own private user ID to `OWNER_IDS`. Set the distinct negative chat IDs in `.env`, then start Compose. Discovery consumes setup updates; stop other instances first.

Check readiness and manage the bot:

```sh
docker compose exec bot circuit-bot doctor
docker compose ps
docker compose restart bot
docker compose logs --tail=100 bot
```

Open the bot privately, send `/start`, then `/admin group` or `/admin channel`. `/help` lists member commands; `/admin` lists the administration commands. A private chat remembers your selected administration destination.

## Install directly on Linux

```sh
sh scripts/install.sh
.venv/bin/circuit-bot setup
.venv/bin/circuit-bot doctor
.venv/bin/circuit-bot run
```

Setup can discover IDs and creates a private `.env` without overwriting an existing file. Use the supplied systemd service for unattended hosting; paths and account creation instructions are in [Operations](docs/OPERATIONS.md). The runtime uses Linux file locking; use Docker on Windows or macOS.

## Included administration

Member bans, timed bans, kicks, mutes, warnings and reversals; scoped deletion and purge; pin/unpin; group lock/unlock; titles and descriptions; expiring one-use invites and revocation; limited staff promotion; join-request decisions; optional arithmetic join challenge with manual recovery; flood controls; link/media blocking and external channel-sender blocking; literal banned phrases; quiet hours; welcome/goodbye messages; custom commands and keyword replies; scheduled and recurring text announcements; report review, rejection, resolution and appeals; private submission status, withdrawal, delivery recovery, audit logs and health checks.

Telegram rights are checked before actions. Bot owners are configured by numeric ID, not username. Only the configured group/channel are managed. Anonymous administrators use private chat to establish their identity. Moderator decisions involving the moderator’s own account require another reviewer.

## Reliability and privacy

Conversations and records persist across restarts. Incoming batches are saved before advancing Telegram’s polling offset. The outgoing queue persists to disk. Rate-limited sends wait; uncertain network outcomes are held for inspection, because automatically retrying them could duplicate a public message. Failed or uncertain sends also queue a private notice to each configured owner. The owner can inspect `/deliveries`, `/inbox` and `/captchas`. Completed input bodies are erased from the inbox; the sanitized submission remains in the record tables and required processing metadata remains in the inbox/audit tables. Pending inputs and backups can contain original messages.

This is anonymous **to the public audience**. The hosting owner controls a database containing contributor IDs and audit records. Text redaction cannot guarantee removal of identifying details expressed in ordinary prose. A person may identify themselves by describing an event. `/privacy` states these limits plainly.

No bot can guarantee zero failures or uninterrupted Telegram service. Permission changes, outages, deleted accounts, Telegram limits, disk failure and uncertain network outcomes still need handling. This implementation exposes those states instead of claiming delivery succeeded. It cannot retrieve past history, list every Telegram member, determine real account age, prove a scam, or transfer chat ownership.

## Owner maintenance

```sh
docker compose exec bot circuit-bot backup --to /app/data/backup-2026-11-17.sqlite3
docker compose cp bot:/app/data/backup-2026-11-17.sqlite3 ./backup-2026-11-17.sqlite3
```

Keep encrypted copies off the host. Restore and export instructions, troubleshooting, configuration and the live acceptance checklist are in [Operations](docs/OPERATIONS.md).

## Development and scope

```sh
uv sync --extra dev --frozen
uv run pytest -q
uv run ruff check .
```

[Group Help comparison and optional additions](docs/GROUPHELP-COMPARISON.md) records the published features reviewed, implemented equivalents and remaining scope. This is a focused first release, not a claim of complete Group Help parity. [Validation](docs/VALIDATION.md) records the executed checks and their limits.

Project layout: `public.py` for member dialogs; `admin.py` and `moderation.py` for administration; `service.py` for reputation rules; `delivery.py` and `runtime.py` for durable processing; `db.py` for persistence; `cli.py` for installation and maintenance.

# Circuit Perth owner guide

## Permissions and setup

Use a Telegram supergroup, plus a distinct channel. A small test group can be upgraded to a supergroup by enabling topics or making it public in Telegram, then disabling that setting again if desired. Confirm the type using discovery or `doctor`; migration changes the group ID, which must be updated in `.env`.

For the **group**, grant delete messages, ban/restrict users, pin messages, change chat information and invite users. Promotion is optional: grant add administrators only if you intend to use `/promote` and `/demote`.

For the **channel**, grant post messages, edit messages, delete messages, change chat information and invite users. Ban/restrict subscriber access is needed for channel bans; promotion needs add administrators. Startup requires the core deletion/restriction rights in the group and posting rights in the channel; `doctor` reports further missing rights.

Bots added as administrators receive the group messages needed for moderation. In BotFather, `/setprivacy` can be disabled if necessary; re-add the bot after changing that setting. Telegram does not deliver every kind of other-bot activity or historical content. The bot only processes observed messages and member updates.

Keep one instance running. A file lock blocks two instances sharing the same data directory. Telegram returns a conflict if separate hosts compete for polling; the process exits for the supervisor to report. A configured webhook stops startup without deleting it. Deliberately remove an old webhook using Telegram’s `deleteWebhook` method with `drop_pending_updates=false` if migrating from a webhook deployment.

`OWNER_IDS` grants full bot control. Group administrators can review dossiers and use actions allowed by their current Telegram rights. Channel administration requires rights in the channel. Telegram itself enforces the bot’s maximum rights. The bot does not transfer ownership, delete entire chats, change privacy settings reserved for owners, or restore messages after deletion.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | required | Local BotFather token |
| `OWNER_IDS` | required | Comma-separated positive Telegram user IDs |
| `GROUP_ID` | required | Negative ID of the managed supergroup |
| `CHANNEL_ID` | required | Negative ID of the managed channel |
| `DATA_DIR` | `data` | SQLite, lock and health heartbeat directory; Docker uses `/app/data` |
| `TIMEZONE` | `Australia/Perth` | Display dates and quiet hours |
| `REPORT_DESTINATION` | `channel` | `channel`, `group` or `both` |
| `REVIEW_POSTS` | `false` | Optional moderation before anonymous chat delivery |
| `REVIEW_REPORTS` | `false` | Optional moderation before scam report publication |
| `REVIEW_VOUCHES` | `false` | Optional moderation before dossier publication |
| `DAILY_SUBMISSION_LIMIT` | `20` | Per sender across all submission types in the previous 24 hours |
| `SESSION_MINUTES` | `30` | Form expiry, not extended by each answer |

Restart after changing environment variables. With Compose use `docker compose up -d --force-recreate` to load changed `.env` values. `docker compose restart` does not reload them.

## Group controls

Use `/admin group` privately, then `/settings`. Flood prevention starts enabled: more than six observed messages in ten seconds triggers deletion of the current message and a one-minute mute. Restart clears this short flood window. External channel-sender blocking also starts enabled; anonymous group administrators and automatic forwards from the configured channel are exempt. Other optional controls start off. Staff are exempt, with a fresh permission check before destructive automatic actions. Edited messages are rescanned for blocked content but never replay form submissions or commands. Private interactions allow a burst of 20 actions per ten seconds, so a normal multi-step form can be completed without a per-message delay. Submissions themselves require a 15-second interval.

Examples:

```text
/set rules No spam, no scams and no private contact details.
/set welcome Welcome, {name}. Please read /rules.
/set goodbye off
/set warn_limit 3
/set mute_minutes 60
/filter add prohibited phrase
/filter remove prohibited phrase
/set quiet_start 22
/set quiet_end 7
```

Quiet hours delete new non-staff messages during the selected hours; they do not change Telegram’s global chat permissions. Set either value to `-1` to disable quiet hours. Setting both to the same hour makes quiet hours active all day. Banned phrases use literal case-insensitive substring matching. There is no regex or inferred-image filter. Link and media controls delete future messages; they do not scan history. An automatic flood mute is separate from the manual warning counter. Warnings persist until removed.

`/lock` snapshots current group permissions before disabling writing; `/unlock` restores that snapshot. Avoid changing group permissions manually while a bot lock is active. `/mute` preserves the group defaults and will not overwrite a pre-existing manual restriction. `/unmute` only removes a mute recorded by this bot and intersects saved permissions with current defaults. Other staff can still change permissions in Telegram.

Optional join verification asks an arithmetic multiple-choice question in the group. Only the named member can answer. It deters casual automation; it is not a strong identity test or bot-proof CAPTCHA. After three minutes, an unsolved challenge leads to a 60-second ban, then Telegram permits rejoining. `/captchas` lists pending or held challenges; `/verify USER_ID` restores saved permissions for a challenge from this bot. Use `/unban USER_ID` first if the member is still temporarily banned. Permission or network problems hold the challenge for an administrator instead of silently releasing it.

## Dossiers and reports

One active vouch and one active scam report per sender/subject are allowed. Withdrawal, rejection or resolution closes a record. New incidents can be submitted after closure; repeat incidents from the same person count as separate reports but as one distinct reporter. Limits prevent rapid repeated submissions, but cannot establish one human per Telegram account.

Vouches immediately publish to the channel and enter the member’s dossier/count. They are structured separately from anonymous chat and scam reports. With the optional `REVIEW_VOUCHES=true` setting, publication waits for approval instead.

Report status is shown in the dossier: unreviewed, moderator reviewed, or resolved. Rejected and withdrawn records are excluded from public dossier counts. A resolved report remains in the historical report total. Pending records are private until approved. There is no calculated trust score, automatic conviction, or automatic ban based on an allegation count.

Private `/queue` lists the first 20 items needing review; `/review ID` opens any known item. `/approve ID` publishes held content or marks an immediate report as reviewed. `/reject ID REASON` hides it. `/resolve ID REASON` closes a report while retaining its dossier history. Review reasons are kept in the private audit record. The author and target cannot decide their own report. If you are the only administrator and are involved, add an independent administrator or manage the issue through Telegram.

After a vouch or report is withdrawn or rejected, the bot replaces known public copies with a status notice. Resolved reports also receive a status notice. If Telegram refuses the edit, or the send outcome is unknown, the bot reports that a manual check is needed. Republishing, screenshots and copies held by recipients cannot be retracted. The original public report states its status **at publication** and directs readers to the current dossier.

`/appeal ID` accepts a private response from the Telegram ID targeted by that report. Moderators inspect the appeal and explicitly decide the underlying report. An approved appeal does not automatically reverse the report. For unknown/reassigned handles, ownership needs manual investigation; the bot never automatically merges handle records into an ID dossier.

## Schedules and custom responses

Owner commands in private chat:

```text
/schedule channel 30m The event starts shortly.
/repeat group 6h Please read /rules before trading.
/timers
/canceltimer 1
/setcommand tradinghelp Describe the help available here.
/delcommand tradinghelp
/autoreply opening hours | Community support is available in the group.
/delreply opening hours
```

Timers take relative durations from one minute to 365 days. At most 50 active timers and 50 responses per type. Missed recurring occurrences coalesce into one delivery after downtime. Canceling a timer cancels queued occurrences; an in-progress or uncertain send needs a destination check. Custom responses are plain text and cannot execute other commands. Staff promotion is intentionally limited and does not grant the ability to promote other administrators. Demotion applies only to staff promoted by this bot; Telegram must also allow the bot to edit that role.

## Delivery and failure handling

`/mysubmissions` lists your own records and per-destination delivery statuses; `/status ID` opens one. New outgoing messages are queued immediately and normally sent on the next worker tick, subject to Telegram’s limits. Two destinations have separate statuses; one may succeed while the other needs attention.

| State | Action |
| --- | --- |
| `queued` | Waiting for the worker or Telegram rate limit |
| `sending` | A request is in progress |
| `sent` | Telegram returned the message ID |
| `failed` | Telegram rejected the request; check rights and destination |
| `uncertain` | Telegram may have accepted it; inspect the destination first |
| `canceled` | A queued send was canceled or a confirmed public copy was closed |

Owner: `/deliveries`, then `/retry DELIVERY_ID` shows the exact destination and message. After checking the destination, `/retry DELIVERY_ID confirm` retries it. `/canceldelivery ID` stops a queued/held item. Retrying an uncertain item can produce a duplicate. Closed dossier submissions cannot be republished through this tool.

Incoming updates are persisted before advancing the polling offset. If the process stops during an action, it appears under `/inbox` and is held rather than automatically replayed. Inspect saved state and repeat the command if needed. Raw input is cleared after processing or interruption recovery; original data can still exist in pending inputs, SQLite pages or earlier backups. This design chooses visible uncertainty over a false exactly-once guarantee.

The heartbeat reports that the maintenance worker is alive; it does not prove Telegram is reachable. An unhealthy Docker container requires investigation; Docker restart policies restart exited containers, not a still-running unhealthy process. `/health`, `/deliveries`, `/inbox`, `/captchas`, `doctor` and rotated process logs provide the owner view. Logs omit message bodies and redact token-shaped strings.

## Backups, restore and exports

SQLite uses WAL and online backups. Copying only the main `.sqlite3` file while the bot is running can lose recent transactions; use the backup command.

```sh
docker compose exec bot circuit-bot backup --to /app/data/backup-2026-11-17.sqlite3
docker compose cp bot:/app/data/backup-2026-11-17.sqlite3 ./backup-2026-11-17.sqlite3
```

Restore a copied backup into the mounted data volume while the bot is stopped:

```sh
docker compose stop bot
docker compose run --rm -v "$(pwd)/backup-2026-11-17.sqlite3:/restore.sqlite3:ro" bot circuit-bot restore --from /restore.sqlite3
docker compose up -d bot
```

Restore validates the database and saves a `before-restore-*.sqlite3` copy of the existing database. Restoring an older snapshot can resurrect queued records that were already sent later. Inspect destinations and `/deliveries` before retries. Do not restore alongside a running original instance. Preserve both `.env` and the database; a backup does not include the token or environment.

Owner-only local administrative data export (may include third-party contributor IDs in records involving the selected user; review before sharing):

```sh
docker compose exec bot circuit-bot export --to /app/data/member-export.json --user-id 123456789
docker compose cp bot:/app/data/member-export.json ./member-export.json
```

Without `--user-id`, the export includes all data in its selected record tables. Exports contain sensitive contributor and audit information; deliver them only to an authorized recipient. File permissions are private. Data is kept until managed by the owner; completed incoming update metadata is pruned after seven days. The `withdraw` command hides contributions but does not erase audit records. Establish your own retention and data-correction procedure before launch.

Never run `docker compose down -v` unless deliberately deleting the bot’s data volume. A normal `docker compose down` retains it. Back up before upgrades.

## Direct Linux service

Install Python 3.12+ and the project under `/opt/circuit-perth-bot`, then install its `.venv` using the script. Create a dedicated `circuit` system user/group. Run setup locally, create `/opt/circuit-perth-bot/data`, and give that account read access to the project and `.env` and ownership of `data`. Keep `.env` mode `600`. Copy `scripts/circuit-bot.service` into `/etc/systemd/system/`, then enable/start it with your system administrator account. The template’s `ProtectSystem=strict` permits writes only to the data directory; adjust `ReadWritePaths` if you change `DATA_DIR`.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now circuit-bot
sudo journalctl -u circuit-bot -n 100 --no-pager
```

## Build behind a trusted certificate proxy

The normal Docker build uses standard TLS validation. For an organization’s trusted certificate bundle, pass it as a BuildKit secret:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem -t circuit-perth-bot:local .
```

The certificate secret is available only while installing build dependencies; it is not copied into the image. Runtime custom trust and proxy settings must be configured on your host if needed. Never disable TLS verification.

## Live acceptance before inviting the public

Use a test group/channel and two ordinary test accounts. Verify `/report` asks username then report and publishes only to the channel. Check `/post` offers exactly the two destination buttons and sends only to the selected chat. Verify `/vouch` publishes to the channel, and check `/check`, `/self`, all dossier pages, counts and withdrawals. Include an unknown handle and a user who changes their username. Confirm no contributor identity appears in public output.

Use a non-admin test member for bans, mutes, warnings and reversals, group lock restoration, optional join verification and join requests. Confirm ordinary members cannot use admin commands or callback controls. Test channel edits and report tombstones with the bot’s real permissions.

Restart during an open form and resume it. Create a short timer. Verify online backup/restore against a test data volume. Remove posting permission, submit a report and check it is shown as failed, then restore permission and retry only after inspecting the destination. No real Telegram token or live group was provided during development, so these live checks remain required.

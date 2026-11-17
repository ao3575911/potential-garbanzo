# Release validation

Checks below were executed on 17 November 2026 with Python 3.12.14 and python-telegram-bot 22.8. Before GitHub publication, the report default was corrected to channel-only as requested, and its existing test expectations and setup documentation were updated. The checks were not rerun after that correction, following the instruction to stop testing.

## Executed checks

- `ruff check .`: passed.
- `PTB_TIMEDELTA=true python -m pytest -q`: **71 passed**.
- Real PTB `Application`, update parsing and Bot API request serialization exercised through an offline HTTP transport.
- Guided report/vouch input, anonymous posting buttons, public output, dossier totals and pagination, author binding, callback ownership, replay protection, withdrawal, review, unknown handles, expiry and username reassignment checked.
- Missing rights, role protection, outside restrictions, permission snapshots, join verification, channel-sender blocking and failed-notification loop prevention checked.
- Outgoing success, permission rejection, rate-limit delay, network uncertainty, restart recovery and a withdrawal racing delivery checked.
- Durable incoming offset/queue behavior, erased processed input, timer deduplication, private scoped export and backup/restore checked.
- Docker image built with the normal Dockerfile and a temporary trusted certificate secret for this Cloud environment’s HTTPS proxy. TLS verification remained enabled; no certificate was copied into the image.
- Container CLI and non-root SQLite access verified against an isolated temporary Docker volume, with the root filesystem read-only.
- Release ZIP integrity and absence of credentials, databases, environments and cache files checked during packaging.

## Scope of evidence

No real Telegram bot token, live channel or live group was supplied. No public messages or real moderation actions were performed. Test transports simulate Telegram responses; they cannot establish Telegram availability, current chat permissions or real delivery latency. Run the live acceptance checklist in `OPERATIONS.md` with test accounts before a public launch.

The included Group Help comparison describes the first-release scope and optional additions. It is not a claim of complete feature parity. Automatic text redaction removes detectable identifiers; it cannot guarantee removal of identifying meaning from arbitrary prose.

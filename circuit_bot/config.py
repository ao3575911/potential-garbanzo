from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


def flag(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise ConfigError("Boolean settings must be true or false.")
    return value.lower() == "true"


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    owners: frozenset[int]
    group_id: int
    channel_id: int
    data_dir: Path
    timezone: str = "Australia/Perth"
    review_posts: bool = False
    review_reports: bool = False
    review_vouches: bool = False
    report_destination: str = "channel"
    daily_limit: int = 20
    session_minutes: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / "circuit.sqlite3"

    @classmethod
    def load(cls, env_file: str = ".env") -> Config:
        load_dotenv(env_file, override=False)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{25,}", token):
            raise ConfigError("Set BOT_TOKEN to the token from @BotFather in .env.")
        try:
            owners = frozenset(int(v.strip()) for v in os.getenv("OWNER_IDS", "").split(",") if v.strip())
            group = int(os.getenv("GROUP_ID", "0"))
            channel = int(os.getenv("CHANNEL_ID", "0"))
            daily = int(os.getenv("DAILY_SUBMISSION_LIMIT", "20"))
            minutes = int(os.getenv("SESSION_MINUTES", "30"))
        except ValueError as exc:
            raise ConfigError("Owner IDs, chat IDs and limits must be integers.") from exc
        if not owners or any(v <= 0 for v in owners):
            raise ConfigError("Set OWNER_IDS to your numeric Telegram user ID(s).")
        if group >= 0 or channel >= 0 or group == channel:
            raise ConfigError("Set distinct negative GROUP_ID and CHANNEL_ID values.")
        if not 1 <= daily <= 1000 or not 5 <= minutes <= 120:
            raise ConfigError("Daily limit must be 1–1000; session duration must be 5–120 minutes.")
        timezone = os.getenv("TIMEZONE", "Australia/Perth")
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError) as exc:
            raise ConfigError("TIMEZONE must be an IANA timezone such as Australia/Perth.") from exc
        report_destination = os.getenv("REPORT_DESTINATION", "channel").lower()
        if report_destination not in {"channel", "group", "both"}:
            raise ConfigError("REPORT_DESTINATION must be channel, group or both.")
        return cls(
            token=token, owners=owners, group_id=group, channel_id=channel,
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(), timezone=timezone,
            review_posts=flag(os.getenv("REVIEW_POSTS", "false")),
            review_reports=flag(os.getenv("REVIEW_REPORTS", "false")),
            review_vouches=flag(os.getenv("REVIEW_VOUCHES", "false")),
            report_destination=report_destination,
            daily_limit=daily, session_minutes=minutes,
        )


DEFAULT_SETTINGS = {
    "antiflood": True,
    "antilink": False,
    "block_media": False,
    "block_sender_chats": True,
    "captcha": False,
    "welcome": "Welcome, {name}. Please read /rules before posting.",
    "goodbye": "",
    "rules": "Treat members respectfully. No scams, spam or sharing private information.",
    "banned_words": [],
    "warn_limit": 3,
    "flood_count": 6,
    "flood_seconds": 10,
    "mute_minutes": 60,
    "quiet_start": -1,
    "quiet_end": -1,
}

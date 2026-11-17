from __future__ import annotations

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from .config import Config
from .service import UserError

ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


class Access:
    def __init__(self, config: Config, bot):
        self.config, self.bot = config, bot

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.config.owners

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            return member.status in ADMIN_STATUSES
        except TelegramError:
            return False  # Permission checks fail closed.

    async def reviewer(self, user_id: int):
        if not await self.is_admin(self.config.group_id, user_id):
            raise UserError("This command is available to Circuit Perth group administrators.")

    def owner(self, user_id: int):
        if not self.is_owner(user_id):
            raise UserError("This command is available to the bot owner.")

    async def require(self, chat_id: int, user_id: int, right: str | None = None, owner_only=False):
        if chat_id not in {self.config.group_id, self.config.channel_id}:
            raise UserError("This chat is not configured for Circuit Perth Bot.")
        if owner_only:
            self.owner(user_id)
        if not self.is_owner(user_id):
            try:
                actor = await self.bot.get_chat_member(chat_id, user_id)
            except TelegramError as exc:
                raise UserError("I could not confirm your administrator permissions. Please try again.") from exc
            if actor.status not in ADMIN_STATUSES or (
                right and actor.status != ChatMemberStatus.OWNER and not getattr(actor, right, False)
            ):
                raise UserError("You need the matching Telegram administrator permission for this action.")
        try:
            bot_member = await self.bot.get_chat_member(chat_id, self.bot.id)
        except TelegramError as exc:
            raise UserError("I could not confirm my permissions. Please try again.") from exc
        if bot_member.status not in ADMIN_STATUSES or (right and not getattr(bot_member, right, False)):
            raise UserError("I need the matching administrator permission in that chat. Ask the owner to update my permissions.")

    async def protect_target(self, chat_id: int, user_id: int):
        if user_id in self.config.owners or user_id == self.bot.id:
            raise UserError("That account is protected from moderation actions.")
        member = await self.bot.get_chat_member(chat_id, user_id)
        if member.status in ADMIN_STATUSES:
            raise UserError("Administrators are protected. Change their role in Telegram first.")

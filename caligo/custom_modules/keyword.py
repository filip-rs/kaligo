"""Alert you in Saved Messages when a keyword appears in any chat you can see.

    .watch <keyword>      start watching for a keyword (case-insensitive)
    .unwatch <keyword>    stop watching a keyword
    .watchlist            show watched keywords and whether alerts are on
    .watchtoggle [on|off] turn Saved Messages alerts on or off

Your own messages are ignored, so you won't get pinged for words you type.
"""

from typing import Any, ClassVar, List, Optional, Set

from pyrogram.types import Message

from caligo import command, module, util
from caligo.core import database


def msg_link(chat: Any, msg_id: int) -> Optional[str]:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    cid = getattr(chat, "id", None)
    if cid is not None and str(cid).startswith("-100"):
        return f"https://t.me/c/{str(cid).replace('-100', '', 1)}/{msg_id}"
    return None  # private chats / basic groups have no public link


class Keyword(module.Module):
    name: ClassVar[str] = "Keyword"

    db: database.AsyncCollection
    cfg: database.AsyncCollection
    words: Set[str]
    enabled: bool

    async def on_load(self) -> None:
        self.db = self.bot.db.get_collection("KEYWORD")
        self.cfg = self.bot.db.get_collection("KEYWORD_SETTINGS")
        self.words = set()
        self.enabled = True

    async def on_start(self, time_us: int) -> None:  # skipcq: PYL-W0613
        async for doc in self.db.find({}):
            self.words.add(doc["_id"])

        settings = await self.cfg.find_one({"_id": 0})
        if settings is not None:
            self.enabled = settings.get("enabled", True)

        if self.words:
            self.log.info(
                "Watching %d keyword(s), alerts %s",
                len(self.words),
                "on" if self.enabled else "off",
            )

    async def on_message(self, message: Message) -> None:
        if not self.enabled or not self.words:
            return
        # Ignore our own messages (including the alerts we send to Saved Messages).
        if getattr(message, "outgoing", False):
            return
        if message.from_user and message.from_user.id == self.bot.uid:
            return

        text = message.text or message.caption
        if not text:
            return

        lowered = text.lower()
        hits = [w for w in self.words if w in lowered]
        if not hits:
            return

        chat = message.chat
        name = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "?"
        link = msg_link(chat, message.id)
        jump = f"\n[jump to message]({link})" if link else ""
        sender = (
            util.tg.mention_user(message.from_user)
            if message.from_user
            else getattr(getattr(message, "sender_chat", None), "title", "unknown")
        )

        await self.bot.client.send_message(
            "me",
            util.tg.truncate(
                f"🔔 Keyword **{', '.join(sorted(hits))}** in **{name}**\n"
                f"From: {sender}{jump}\n\n{text}"
            ),
            disable_web_page_preview=True,
        )

    @command.desc("Watch for a keyword and get alerted in Saved Messages")
    @command.usage("[keyword or phrase]")
    async def cmd_watch(self, ctx: command.Context) -> str:
        word = ctx.input.strip().lower()
        if not word:
            return "__Give a keyword to watch, e.g.__ `watch rtx 4090`__.__"
        if word in self.words:
            return f"__Already watching__ `{word}`__.__"

        await self.db.update_one({"_id": word}, {"$set": {"_id": word}}, upsert=True)
        self.words.add(word)
        state = "" if self.enabled else " __(alerts are currently off — `watchtoggle on`)__"
        return f"🔔 Now watching for `{word}`.{state}"

    @command.desc("Stop watching a keyword")
    @command.usage("[keyword or phrase]")
    async def cmd_unwatch(self, ctx: command.Context) -> str:
        word = ctx.input.strip().lower()
        if word not in self.words:
            return f"__Not watching__ `{word}`__.__"

        await self.db.delete_one({"_id": word})
        self.words.discard(word)
        return f"🔕 Stopped watching `{word}`."

    @command.desc("List watched keywords")
    async def cmd_watchlist(self, ctx: command.Context) -> str:
        state = "🔔 on" if self.enabled else "🔕 off"
        if not self.words:
            return f"__No keywords watched.__ Alerts: {state}."
        words = "\n".join(f"• `{w}`" for w in sorted(self.words))
        return f"**Watched keywords** (alerts: {state}):\n{words}"

    @command.desc("Turn keyword alerts to Saved Messages on or off")
    @command.usage("[on | off?]", optional=True)
    async def cmd_watchtoggle(self, ctx: command.Context) -> str:
        arg = ctx.input.strip().lower()
        if arg in ("on", "enable", "enabled", "1"):
            self.enabled = True
        elif arg in ("off", "disable", "disabled", "0"):
            self.enabled = False
        else:
            self.enabled = not self.enabled  # bare toggle

        await self.cfg.update_one(
            {"_id": 0}, {"$set": {"enabled": self.enabled}}, upsert=True
        )
        return f"Keyword alerts are now **{'on 🔔' if self.enabled else 'off 🔕'}**."

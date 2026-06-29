"""Log edited and deleted messages from chats/topics you choose, to Saved Messages.

Telegram's delete update carries no content (and edits only carry the *new* text),
so this module caches messages from watched chats as they arrive, then reports the
original when one is edited or deleted. Caching is scoped to what you watch, and
cache entries self-expire (see CACHE_TTL_DAYS).

Pick what to watch by running the command inside the chat/topic, or pass a link:
    .logwatch                      watch the current chat (or topic, if in a forum)
    .logwatch t.me/mchub/539       watch topic 539 in mchub
    .logwatch @somegroup           watch a whole chat
    .logunwatch [same target]      stop watching
    .logwatches                    list what's watched

Notes:
  - Reliable for channels/supergroups/forums (deletes there include the chat).
  - Only messages seen *after* you start watching can be reported.
"""

from datetime import datetime, timezone
from typing import Any, ClassVar, List, Optional, Tuple

from pyrogram import enums
from pyrogram.types import Message

from caligo import command, module, util
from caligo.core import database

# How long cached messages are kept (so edits/deletes can be reported).
CACHE_TTL_DAYS = 3

_FORUM_TYPES = (enums.ChatType.FORUM, enums.ChatType.MONOFORUM)


def _is_forum(chat: Any) -> bool:
    return getattr(chat, "type", None) in _FORUM_TYPES


def parse_tme_link(text: str) -> Optional[Tuple[Any, List[int]]]:
    """(chat_ref, [trailing numbers]) for a t.me link, or None. See scheduler module."""
    text = text.strip()
    low = text.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            text, low = text[len(prefix) :], low[len(prefix) :]
            break
    if not low.startswith("t.me/"):
        return None

    parts = [p for p in text[len("t.me/") :].split("/") if p]
    private = bool(parts) and parts[0].lower() == "c"
    if private:
        parts = parts[1:]
    if not parts:
        return None
    chat, rest = parts[0], parts[1:]
    if not all(n.isdigit() for n in rest):
        return None
    if private and not chat.isdigit():
        return None
    chat_ref: Any = int(f"-100{chat}") if private else chat
    return chat_ref, [int(n) for n in rest]


def msg_link(chat_id: int, username: Optional[str], msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    internal = str(chat_id).replace("-100", "", 1)
    return f"https://t.me/c/{internal}/{msg_id}"


def describe_content(message: Message) -> str:
    text = message.text or message.caption or ""
    media = getattr(message, "media", None)
    if media is not None:
        label = getattr(media, "value", str(media))
        return f"[{label}] {text}".strip()
    return text or "(no text)"


def describe_sender(message: Message) -> str:
    user = getattr(message, "from_user", None)
    if user is not None:
        return util.tg.mention_user(user)
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return getattr(sender_chat, "title", None) or "a channel"
    return "unknown"


class Logger(module.Module):
    name: ClassVar[str] = "Logger"

    db: database.AsyncCollection
    cache: database.AsyncCollection
    watches: List[Tuple[int, Optional[int]]]

    async def on_load(self) -> None:
        self.db = self.bot.db.get_collection("LOGGER_WATCH")
        self.cache = self.bot.db.get_collection("LOGGER_CACHE")
        self.watches = []

    async def on_start(self, time_us: int) -> None:  # skipcq: PYL-W0613
        # Self-expiring cache + a lookup index for delete/edit resolution.
        await self.cache.create_index("ts", expireAfterSeconds=CACHE_TTL_DAYS * 86400)
        await self.cache.create_index([("chat_id", 1), ("msg_id", 1)])

        async for w in self.db.find({}):
            self.watches.append((w["chat_id"], w.get("topic_id")))
        if self.watches:
            self.log.info("Watching %d chat/topic(s) for edits/deletes", len(self.watches))

    def _matches(self, chat_id: int, topic_id: Optional[int]) -> bool:
        for w_chat, w_topic in self.watches:
            if w_chat == chat_id and (w_topic is None or w_topic == topic_id):
                return True
        return False

    async def _save_target(self, ctx: command.Context, arg: str):
        """Returns (chat, topic_id) for a watch target ('here' or a t.me link)."""
        if not arg:
            return ctx.chat, getattr(ctx.msg, "message_thread_id", None)

        link = parse_tme_link(arg)
        if link is not None:
            chat = await self.bot.client.get_chat(link[0])
            nums = link[1]
            if len(nums) >= 2:
                topic = nums[0]
            elif len(nums) == 1:
                topic = nums[0] if _is_forum(chat) else None
            else:
                topic = None
            return chat, topic

        # Bare @username / id / invite link.
        ref: Any = arg.lstrip("@")
        if ref.lstrip("-").isdigit():
            ref = int(ref)
        return await self.bot.client.get_chat(ref), None

    # --- Live caching + reporting ---------------------------------------

    async def on_message(self, message: Message) -> None:
        if not message.chat:
            return
        topic = getattr(message, "message_thread_id", None)
        if not self._matches(message.chat.id, topic):
            return

        await self.cache.update_one(
            {"chat_id": message.chat.id, "msg_id": message.id},
            {
                "$set": {
                    "chat_id": message.chat.id,
                    "msg_id": message.id,
                    "topic_id": topic,
                    "chat_name": message.chat.title or message.chat.first_name or "",
                    "chat_username": message.chat.username,
                    "sender": describe_sender(message),
                    "content": describe_content(message),
                    "ts": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def on_message_edit(self, message: Message) -> None:
        if not message.chat:
            return
        topic = getattr(message, "message_thread_id", None)
        if not self._matches(message.chat.id, topic):
            return

        cached = await self.cache.find_one(
            {"chat_id": message.chat.id, "msg_id": message.id}
        )
        old = cached["content"] if cached else "__(not cached)__"
        new = describe_content(message)
        link = msg_link(message.chat.id, message.chat.username, message.id)
        name = message.chat.title or message.chat.first_name or str(message.chat.id)

        await self.bot.client.send_message(
            "me",
            util.tg.truncate(
                f"✏️ **Edited** in **{name}**\n"
                f"By: {describe_sender(message)}\n"
                f"[jump to message]({link})\n\n"
                f"**Before:**\n{old}\n\n**After:**\n{new}"
            ),
            disable_web_page_preview=True,
        )

        # Refresh the cache so a later edit shows the latest as "before".
        if cached:
            await self.cache.update_one(
                {"_id": cached["_id"]},
                {"$set": {"content": new, "ts": datetime.now(timezone.utc)}},
            )

    async def on_message_delete(self, messages: List[Message]) -> None:
        for message in messages:
            chat = getattr(message, "chat", None)
            if chat is None:
                continue  # private/basic-group deletes carry no chat; can't resolve

            cached = await self.cache.find_one(
                {"chat_id": chat.id, "msg_id": message.id}
            )
            if not cached or not self._matches(chat.id, cached.get("topic_id")):
                continue

            link = msg_link(chat.id, cached.get("chat_username"), message.id)
            await self.bot.client.send_message(
                "me",
                util.tg.truncate(
                    f"🗑️ **Deleted** in **{cached.get('chat_name') or chat.id}**\n"
                    f"By: {cached.get('sender', 'unknown')}\n"
                    f"[jump to topic]({link})\n\n{cached['content']}"
                ),
                disable_web_page_preview=True,
            )
            await self.cache.delete_one({"_id": cached["_id"]})

    # --- Commands -------------------------------------------------------

    @command.desc("Watch a chat/topic for edited & deleted messages (logs to Saved)")
    @command.usage("[here (default) | t.me topic link | @chat]", optional=True)
    async def cmd_logwatch(self, ctx: command.Context) -> str:
        try:
            chat, topic = await self._save_target(ctx, ctx.input.strip())
        except Exception as e:  # skipcq: PYL-W0703
            return f"__Couldn't resolve that chat:__ `{e}`"

        name = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        if (chat.id, topic) in self.watches:
            return f"__Already watching **{name}**{f' (topic {topic})' if topic else ''}.__"

        await self.db.insert_one(
            {"chat_id": chat.id, "topic_id": topic, "chat_name": name}
        )
        self.watches.append((chat.id, topic))
        topic_str = f" (topic {topic})" if topic else ""
        return f"👁️ Now logging edits & deletes in **{name}**{topic_str}."

    @command.desc("Stop watching a chat/topic for edits & deletes")
    @command.usage("[here (default) | t.me topic link | @chat]", optional=True)
    async def cmd_logunwatch(self, ctx: command.Context) -> str:
        try:
            chat, topic = await self._save_target(ctx, ctx.input.strip())
        except Exception as e:  # skipcq: PYL-W0703
            return f"__Couldn't resolve that chat:__ `{e}`"

        if (chat.id, topic) not in self.watches:
            return "__That chat/topic isn't being watched.__"

        await self.db.delete_one({"chat_id": chat.id, "topic_id": topic})
        self.watches.remove((chat.id, topic))
        name = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        return f"🚫 Stopped logging **{name}**{f' (topic {topic})' if topic else ''}."

    @command.desc("List chats/topics being watched for edits & deletes")
    async def cmd_logwatches(self, ctx: command.Context) -> str:
        lines = []
        async for w in self.db.find({}, sort=[("chat_name", 1)]):
            topic = w.get("topic_id")
            topic_str = f" (topic {topic})" if topic else ""
            lines.append(f"• **{w.get('chat_name') or w['chat_id']}**{topic_str}")

        if not lines:
            return "__Not watching anything. Use__ `logwatch` __in a chat/topic.__"
        return "**Watched for edits/deletes:**\n" + "\n".join(lines)

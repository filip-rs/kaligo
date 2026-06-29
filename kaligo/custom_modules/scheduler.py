"""Periodically repost a chosen message to a chosen chat, with optional jitter.

There are two ways to pick the message to repost:

  1. Reply to it:
        .schedule <target> <interval> [jitter?]

  2. Point at it with a message link (no reply needed):
        .schedule <message link> [target?] <interval> [jitter?]
     If you omit the target, it reposts into the link's own channel.

Forum topics: in a forum, opening a topic gives a link like t.me/mchub/539 -- the
lone number (539) is the *topic*, not a message. Use that as the target and the
post lands in that topic. A two-number link t.me/mchub/5/30084 means "message
30084 inside topic 5". Just copy the link from the exact topic you want.

Arguments:
    target        where to post: "here", an @username, a forum-topic link
                  (e.g. t.me/mchub/539), a chat/invite link, a numeric id, or
                  "same" to reuse the source channel/topic
    message link  (no-reply mode) the message to repost: t.me/channel/<msg> or,
                  in a forum, the full t.me/forum/<topic>/<msg>
    interval      how often to send, e.g. 30m, 2h, 1h30m, 90s (bare number = minutes)
    jitter        optional max extra random delay per send, e.g. 15m, 1h
    for <dur>     optional: auto-stop after this long, e.g. "for 7d" (anywhere in
                  the command). You get a Saved Messages note when it ends.

If sends keep failing (kicked, message deleted, flood wait), you get an alert in
Saved Messages after a few tries, and another note when it recovers.

Examples (reply to your message, then run):
    .schedule t.me/mchub/539 6h for 7d      -> into topic 539 every 6h, stops in 7d
    .schedule t.me/mchub/539 1h 15m         -> every hour + 0-15m jitter
    .schedule @mychannel 2h                 -> into a normal channel

No-reply mode (repost an existing message):
    .schedule t.me/mchub/5/30084 1h         -> repost that message into its own topic
    .schedule t.me/mchub/5/30084 @other 2h  -> ...into @other instead
"""

import asyncio
import random
import re
from datetime import timedelta
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple

from pyrogram import enums

from kaligo import command, module, util
from kaligo.core import database

_FORUM_TYPES = (enums.ChatType.FORUM, enums.ChatType.MONOFORUM)


def _is_forum(chat: Any) -> bool:
    return getattr(chat, "type", None) in _FORUM_TYPES

# Matches duration tokens like "2h", "30m", "90s", "1d", or a bare number.
_DURATION_TOKEN = re.compile(r"(\d+)([smhd]?)", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}  # bare = minutes

# Lower bound to avoid accidentally hammering Telegram (and risking a flood ban).
MIN_INTERVAL = 30

# Alert Saved Messages after this many consecutive failed sends.
FAILURE_ALERT_THRESHOLD = 3


def parse_duration(text: str) -> Optional[int]:
    """Parses a human duration string into seconds, or None if invalid."""
    text = text.strip().lower()
    if not text:
        return None

    # Reject anything that isn't made purely of duration tokens.
    if re.sub(r"\d+[smhd]?", "", text).strip():
        return None

    total = 0
    matched = False
    for token in _DURATION_TOKEN.finditer(text):
        total += int(token.group(1)) * _UNIT_SECONDS[token.group(2)]
        matched = True

    return total if matched else None


def parse_tme_link(text: str) -> Optional[Tuple[Any, List[int]]]:
    """Parses any t.me link into (chat_ref, [trailing numbers]), or None.

    chat_ref is a username string, or a -100... peer id for "t.me/c/<id>/..."
    links. The numbers are the path parts after the chat, e.g.:
        t.me/mchub            -> ("mchub", [])
        t.me/mchub/539        -> ("mchub", [539])          (forum topic OR message)
        t.me/mchub/5/30084    -> ("mchub", [5, 30084])     (message 30084 in topic 5)
        t.me/c/1234567/890    -> (-1001234567, [890])

    Interpreting a lone number as a topic vs a message needs the chat itself
    (whether it's a forum), so that decision is left to the caller.
    """
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
    # Everything after the chat must be numeric; reject invite links etc.
    if not all(n.isdigit() for n in rest):
        return None
    if private and not chat.isdigit():
        return None

    chat_ref: Any = int(f"-100{chat}") if private else chat
    return chat_ref, [int(n) for n in rest]


def fmt_duration(seconds: int) -> str:
    return util.time.format_duration_td(timedelta(seconds=seconds))


class Scheduler(module.Module):
    name: ClassVar[str] = "Scheduler"

    db: database.AsyncCollection
    tasks: Dict[int, "asyncio.Task[None]"]

    async def on_load(self) -> None:
        self.db = self.bot.db.get_collection(self.name.upper())
        self.tasks = {}

    async def on_start(self, time_us: int) -> None:  # skipcq: PYL-W0613
        # Resume every saved schedule once the client is connected.
        count = 0
        async for sched in self.db.find({}):
            self._spawn(sched)
            count += 1

        if count:
            self.log.info("Resumed %d scheduled message(s)", count)

    async def on_stop(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        self.tasks.clear()

    def _spawn(self, sched: Mapping[str, Any]) -> None:
        sid = sched["_id"]

        existing = self.tasks.get(sid)
        if existing and not existing.done():
            existing.cancel()

        self.tasks[sid] = self.bot.loop.create_task(self._runner(dict(sched)))

    async def _notify(self, text: str) -> None:
        """Sends an alert to Saved Messages, swallowing any failure."""
        try:
            await self.bot.client.send_message(
                "me", text, disable_web_page_preview=True
            )
        except Exception as e:  # skipcq: PYL-W0703
            self.log.error("Couldn't send scheduler alert: %s", e)

    async def _expire(self, sched: Dict[str, Any]) -> None:
        sid = sched["_id"]
        await self.db.delete_one({"_id": sid})
        self.tasks.pop(sid, None)
        self.log.info("Schedule #%d expired and was removed", sid)
        await self._notify(
            f"⌛ Schedule **#{sid}** → **{sched['target_name']}** reached its end "
            f"time and was removed."
        )

    async def _runner(self, sched: Dict[str, Any]) -> None:
        sid: int = sched["_id"]
        interval: int = sched["interval"]
        jitter: int = sched.get("jitter", 0)
        expires_at: Optional[int] = sched.get("expires_at")
        fails = 0

        while True:
            wait = interval + (random.randint(0, jitter) if jitter > 0 else 0)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break

            # Stop once the schedule's lifetime is over (don't send past it).
            if expires_at and util.time.sec() >= expires_at:
                await self._expire(sched)
                break

            try:
                kwargs = {}
                if sched.get("target_topic"):
                    kwargs["message_thread_id"] = sched["target_topic"]
                await self.bot.client.copy_message(
                    chat_id=sched["target_id"],
                    from_chat_id=sched["from_chat"],
                    message_id=sched["from_msg"],
                    **kwargs,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:  # skipcq: PYL-W0703
                fails += 1
                self.log.error(
                    "Scheduled message #%d failed (%d in a row): %s", sid, fails, e
                )
                if fails == FAILURE_ALERT_THRESHOLD:
                    await self._notify(
                        f"⚠️ Schedule **#{sid}** → **{sched['target_name']}** has "
                        f"failed {fails} times in a row.\nLast error: `{e}`\n"
                        f"Still retrying — `unschedule {sid}` to stop."
                    )
                continue

            if fails >= FAILURE_ALERT_THRESHOLD:
                await self._notify(
                    f"✅ Schedule **#{sid}** → **{sched['target_name']}** is working "
                    f"again after {fails} failed attempt(s)."
                )
            fails = 0

            await self.db.update_one(
                {"_id": sid}, {"$set": {"last_run": util.time.sec()}}
            )
            self.log.info(
                "Sent scheduled message #%d to %s", sid, sched["target_name"]
            )

    async def _next_id(self) -> int:
        last = await self.db.find_one({}, sort=[("_id", -1)])
        return (last["_id"] + 1) if last else 1

    async def _resolve_target(
        self, ctx: command.Context, target: str
    ) -> Tuple[Any, Optional[int]]:
        """Returns (chat, topic_id) for a target spec."""
        if target.lower() in ("here", "this"):
            return ctx.chat, getattr(ctx.msg, "message_thread_id", None)

        target = target.strip()

        # A t.me link names a channel and (for forums) a topic.
        link = parse_tme_link(target)
        if link is not None:
            chat_ref, nums = link
            chat = await self.bot.client.get_chat(chat_ref)
            if len(nums) >= 2:
                topic = nums[0]  # t.me/chat/<topic>/<msg>
            elif len(nums) == 1:
                # Lone number: a topic in a forum, else a message id we ignore.
                topic = nums[0] if _is_forum(chat) else None
            else:
                topic = None
            return chat, topic

        # Otherwise accept a leading @, a numeric id, or an invite link as-is.
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if target.lower().startswith(prefix):
                target = target[len(prefix) :].rstrip("/")
                break

        ref: Any = target
        stripped = target.lstrip("-")
        if stripped.isdigit():
            ref = int(target)

        return await self.bot.client.get_chat(ref), None

    @command.desc(
        "Periodically repost your replied message to a chat or forum topic"
    )
    @command.usage(
        "[target: here | @channel | forum-topic link e.g. t.me/mchub/539] "
        "[interval e.g. 30m] [jitter? e.g. 15m] [for <duration>? e.g. for 7d]  "
        "(reply to the message to repost)",
        reply=True,
        optional=True,
    )
    @command.alias("sched")
    async def cmd_schedule(self, ctx: command.Context) -> str:
        args = list(ctx.args)
        reply = ctx.reply_msg

        # Pull an optional "for <duration>" clause (auto-stop after that long).
        expiry_seconds: Optional[int] = None
        lowered = [a.lower() for a in args]
        if "for" in lowered:
            i = lowered.index("for")
            if i + 1 >= len(args):
                return "__`for` needs a duration, e.g.__ `for 7d`__.__"
            expiry_seconds = parse_duration(args[i + 1])
            if expiry_seconds is None:
                return f"__Invalid duration after `for`:__ `{args[i + 1]}`"
            del args[i : i + 2]

        usage = (
            "__Usage (reply to your message):__ `schedule [target] [interval] [jitter?]`\n"
            "__target__ = `here`, `@channel`, or a forum-topic link like "
            "`t.me/mchub/539`\n"
            "__Examples:__ `schedule t.me/mchub/539 30m` · "
            "`schedule @mychannel 1h 15m`"
        )

        # --- Figure out the source message (what gets reposted) ---
        from_chat: Any
        from_msg: int
        source_chat = None  # resolved chat object, used for the "same"/default target
        source_topic: Optional[int] = None

        if reply:
            from_chat, from_msg = ctx.chat.id, reply.id
        elif args and (link := parse_tme_link(args[0])) and link[1]:
            chat_ref, nums = link
            try:
                source_chat = await self.bot.client.get_chat(chat_ref)
            except Exception as e:  # skipcq: PYL-W0703
                return f"__Couldn't resolve the linked message's chat:__ `{e}`"

            if len(nums) >= 2:
                source_topic, from_msg = nums[0], nums[-1]
            elif _is_forum(source_chat):
                # A lone number on a forum is a topic, not a single message.
                return (
                    "__That link points to a whole topic, not one message. "
                    "Reply to the message you want reposted, or use a full "
                    "message link like__ `t.me/mchub/5/30084`__.__"
                )
            else:
                from_msg = nums[0]

            from_chat = source_chat.id
            args = args[1:]  # consume the link
        else:
            return "__Reply to a message, or pass a t.me message link.__\n\n" + usage

        # --- Remaining args: [target?] interval [jitter?] ---
        if not args:
            return usage

        # If the first remaining arg is a duration, no explicit target was given.
        target_arg: Optional[str] = None
        if parse_duration(args[0]) is None:
            target_arg = args.pop(0)

        if not args:
            return "__Missing interval.__\n\n" + usage

        interval = parse_duration(args[0])
        if interval is None:
            return f"__Invalid interval:__ `{args[0]}` __(try `30m`, `2h`, `1h30m`).__"
        if interval < MIN_INTERVAL:
            return f"__Interval too short. Minimum is {fmt_duration(MIN_INTERVAL)}.__"

        jitter = 0
        if len(args) > 1:
            jitter = parse_duration(args[1])  # type: ignore
            if jitter is None:
                return f"__Invalid jitter:__ `{args[1]}` __(try `15m`, `1h`).__"

        if expiry_seconds is not None and expiry_seconds <= interval:
            return (
                f"__`for` ({fmt_duration(expiry_seconds)}) must be longer than the "
                f"interval ({fmt_duration(interval)}), or it would never send.__"
            )

        # --- Resolve the target chat (and forum topic, if any) ---
        target_topic: Optional[int] = None
        if target_arg is None or target_arg.lower() == "same":
            if source_chat is None:
                return (
                    "__No target given. When replying you must name a target "
                    "(e.g. `here` or `@channel`).__\n\n" + usage
                )
            chat, target_topic = source_chat, source_topic
        else:
            try:
                chat, target_topic = await self._resolve_target(ctx, target_arg)
            except Exception as e:  # skipcq: PYL-W0703
                return f"__Couldn't resolve target__ `{target_arg}`__:__ `{e}`"

        target_name = (
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or getattr(chat, "username", None)
            or str(chat.id)
        )

        sid = await self._next_id()
        expires_at = (
            util.time.sec() + expiry_seconds if expiry_seconds is not None else None
        )
        sched = {
            "_id": sid,
            "target_id": chat.id,
            "target_name": target_name,
            "target_topic": target_topic,
            "from_chat": from_chat,
            "from_msg": from_msg,
            "interval": interval,
            "jitter": jitter,
            "expires_at": expires_at,
            "created": util.time.sec(),
            "last_run": None,
        }
        await self.db.insert_one(sched)
        self._spawn(sched)

        jitter_str = f" + up to {fmt_duration(jitter)} jitter" if jitter else ""
        topic_str = f" (topic {target_topic})" if target_topic else ""
        expiry_str = (
            f"\nRuns for **{fmt_duration(expiry_seconds)}**, then stops."
            if expiry_seconds
            else ""
        )
        return (
            f"✅ Scheduled as **#{sid}** → **{target_name}**{topic_str}\n"
            f"Every **{fmt_duration(interval)}**{jitter_str}.{expiry_str}\n"
            f"First send in ~{fmt_duration(interval)}. "
            f"Use `unschedule {sid}` to stop."
        )

    @command.desc("List active scheduled messages")
    @command.alias("scheds", "schedules")
    async def cmd_schedulelist(self, ctx: command.Context) -> str:
        lines = []
        async for sched in self.db.find({}, sort=[("_id", 1)]):
            sid = sched["_id"]
            jitter = sched.get("jitter", 0)
            jitter_str = f" (+0-{fmt_duration(jitter)})" if jitter else ""
            last = sched.get("last_run")
            last_str = (
                f"{fmt_duration(util.time.sec() - last)} ago" if last else "not yet"
            )
            running = "" if sid in self.tasks and not self.tasks[sid].done() else " ⚠️"
            topic = sched.get("target_topic")
            topic_str = f" (topic {topic})" if topic else ""
            exp = sched.get("expires_at")
            exp_str = (
                f" · ends in {fmt_duration(max(0, exp - util.time.sec()))}"
                if exp
                else ""
            )
            lines.append(
                f"**#{sid}**{running} → {sched['target_name']}{topic_str}\n"
                f"  every {fmt_duration(sched['interval'])}{jitter_str} · "
                f"last: {last_str}{exp_str}"
            )

        if not lines:
            return "__No scheduled messages. Reply to a message and use__ `schedule`__.__"

        return "**Scheduled messages:**\n\n" + "\n".join(lines)

    @command.desc("Stop and remove a scheduled message by its ID")
    @command.usage("[schedule id]")
    @command.alias("unsched")
    async def cmd_unschedule(self, ctx: command.Context) -> str:
        if not ctx.input:
            return "__Pass the schedule ID to remove (see__ `schedules`__).__"

        try:
            sid = int(ctx.input.strip())
        except ValueError:
            return f"__Invalid ID:__ `{ctx.input}`"

        sched = await self.db.find_one({"_id": sid})
        if not sched:
            return f"__No schedule with ID__ `{sid}`__.__"

        task = self.tasks.pop(sid, None)
        if task and not task.done():
            task.cancel()

        await self.db.delete_one({"_id": sid})
        return f"🗑️ Removed schedule **#{sid}** ({sched['target_name']})."

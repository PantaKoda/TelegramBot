"""Screenshot filing bot.

Plainly: you tell it a date, send it screenshots, and say you're done. It puts
them in cloud storage under a folder named after that date, numbered in the
order you sent them. If you file the same date twice, the second batch lands in
a new version folder (v2, v3, ...) rather than overwriting the first.

Nothing is uploaded until /finish_job. An abandoned session expires on its own
and leaves storage untouched.
"""

import logging
import os

import telebot
from telebot import custom_filters
from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.states import State, StatesGroup
from telebot.states.sync.context import StateContext
from telebot.states.sync.middleware import StateMiddleware
from telebot.storage import StateMemoryStorage
from telebot.types import BotCommand, BotCommandScopeChat, Message, ForceReply
from telebot.util import extract_arguments

from .config import bucket, config, r2
from .dates import DATE_EG, DATE_FMT, parse_job_date, prefix_for
from .sessions import SessionTimers
from .storage import next_version, put_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegrambot")

SESSION_TTL = 600  # seconds of inactivity before an open session is dropped
MAX_DOWNLOAD = 20 * 1024 * 1024  # bots cannot fetch files larger than this

HELP_TEXT = (
    "*Screenshot filing*\n\n"
    f"`/job_start {DATE_EG}` — open a session for that date ({DATE_FMT})\n"
    "send screenshots — queued in the order you send them\n"
    "`/finish_job` — upload them and close\n"
    "`/job_cancel` — discard without uploading\n"
    "`/status` — what's currently open\n\n"
    f"Sessions expire after {SESSION_TTL // 60} minutes of silence and upload nothing.\n"
    "Filing a date twice creates a new version; the highest version is the current one."
)

bot = telebot.TeleBot(
    config.bot_token,
    use_class_middlewares=True,
    state_storage=StateMemoryStorage(),
)


class Session(StatesGroup):
    collecting = State()
    awaiting_date = State()


class Whitelist(BaseMiddleware):
    """Drop every update from anyone not in ALLOWED_USER_IDS."""

    def __init__(self, allowed: frozenset[int]):
        super().__init__()
        self.update_types = ["message"]
        self.allowed = allowed

    def pre_process(self, message: Message, data):
        user = message.from_user
        if user is None or user.id not in self.allowed:
            return CancelUpdate()

    def post_process(self, message, data, exception):
        pass


def _on_expire(chat_id: int, user_id: int) -> None:
    """Runs on a timer thread, so it has no StateContext -- delete directly."""
    bot.delete_state(user_id=user_id, chat_id=chat_id)
    bot.send_message(
        chat_id,
        f"Timed out after {timers.ttl_minutes} minutes of inactivity. "
        f"Nothing was uploaded — start again with /job_start {DATE_EG}",
    )


timers = SessionTimers(SESSION_TTL, _on_expire)

# Both are required: the filter makes `state=` work in handlers, the middleware
# injects the `state` argument. An unregistered filter key fails silently.
bot.add_custom_filter(custom_filters.StateFilter(bot))
bot.setup_middleware(StateMiddleware(bot))
bot.setup_middleware(Whitelist(config.allowed_user_ids))


# --------------------------------------------------------------------------
# Opening a session.  The busy variant is registered first: first match wins.
# --------------------------------------------------------------------------

def _open_session(message: Message, state: StateContext, raw_date: str) -> None:
    """Validate a date and open a collecting session, or ask again."""
    d = parse_job_date(raw_date)
    if d is None:
        # Stay in awaiting_date if that is where we are: the user can just retype.
        bot.reply_to(
            message,
            f"Need a date like {DATE_EG} (format {DATE_FMT}).",
            reply_markup=ForceReply(input_field_placeholder=DATE_EG),
        )
        return

    prefix = prefix_for(d)
    version = next_version(r2, bucket, prefix)

    state.set(Session.collecting)
    state.add_data(prefix=prefix, version=version, shots=[])
    timers.arm(message.chat.id, message.from_user.id)

    note = "" if version == "v1" else " — this supersedes the earlier batch"
    bot.reply_to(
        message,
        f"Session open for {d} → {version}{note}.\n"
        f"Send the screenshots in order, then /finish_job.",
    )

@bot.message_handler(commands=["job_start"], state=Session.collecting)
def job_start_busy(message: Message, state: StateContext):
    with state.data() as d:
        prefix, version, n = d["prefix"], d["version"], len(d["shots"])
    bot.reply_to(
        message,
        f"A session is already open: {prefix}{version} with {n} screenshot(s). "
        f"Use /finish_job to file them or /job_cancel to discard.",
    )


@bot.message_handler(commands=["job_start"], state=Session.awaiting_date)
def job_start_awaiting(message: Message, state: StateContext):
    bot.reply_to(message, f"Still waiting for a date ({DATE_FMT}), or /job_cancel to drop it.")


@bot.message_handler(commands=["job_start"])
def job_start(message: Message, state: StateContext):
    arg = extract_arguments(message.text)
    if not arg:  # tapped from the command menu, which always sends immediately
        state.set(Session.awaiting_date)
        timers.arm(message.chat.id, message.from_user.id)
        bot.send_message(
            message.chat.id,
            f"Which date is this schedule for? ({DATE_FMT})",
            reply_markup=ForceReply(input_field_placeholder=DATE_EG),
        )
        return
    _open_session(message, state, arg)


@bot.message_handler(content_types=["text"], state=Session.awaiting_date)
def receive_date(message: Message, state: StateContext):
    _open_session(message, state, message.text)


# --------------------------------------------------------------------------
# Collecting.  Only queues file ids; no download or upload happens here.
# Documents only: the photo route is recompressed to JPEG by Telegram, which
# smears the edges of small text and ruins OCR.
# --------------------------------------------------------------------------

@bot.message_handler(content_types=["photo"], state=Session.collecting)
def reject_photo(message: Message):
    bot.reply_to(
        message,
        "*Not queued.* That came through as a photo, so Telegram recompressed it "
        "to JPEG and the text will be fuzzy.\n\n"
        "Send it as a *file* instead: attachment clip → File (not Gallery).",
        parse_mode="Markdown",
    )


@bot.message_handler(content_types=["document"], state=Session.collecting)
def collect(message: Message, state: StateContext):
    doc = message.document

    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        bot.reply_to(message, f"Not an image ({mime or 'unknown type'}). Not queued.")
        return

    if doc.file_size and doc.file_size > MAX_DOWNLOAD:
        bot.reply_to(message, "Over 20 MB — bots can't download that. Not queued.")
        return

    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".png" if mime == "image/png" else ".img"

    shot = {
        "file_id": doc.file_id,
        "ext": ext,
        "mime": mime,
        # message_id is monotonic per chat, so sorting on it at upload time
        # restores true send order even when an album arrives out of sequence.
        "message_id": message.message_id,
    }

    with state.data() as d:
        d["shots"] = d["shots"] + [shot]  # rebind; in-place append may not persist
        n = len(d["shots"])

    timers.arm(message.chat.id, message.from_user.id)
    warn = "" if mime == "image/png" else f"  (note: {mime}, not PNG)"
    bot.reply_to(message, f"{n} queued.{warn}")


# --------------------------------------------------------------------------
# Closing.
# --------------------------------------------------------------------------

@bot.message_handler(commands=["finish_job"], state=Session.collecting)
def finish_job(message: Message, state: StateContext):
    timers.disarm(message.from_user.id)

    with state.data() as d:
        prefix, shots = d["prefix"], list(d["shots"])

    if not shots:
        state.delete()
        bot.reply_to(message, "No screenshots in this session — nothing uploaded, session closed.")
        return

    # Recomputed rather than reusing the value from /job_start: that one was
    # only ever a prediction, and this costs a single list call.
    version = next_version(r2, bucket, prefix)
    shots.sort(key=lambda s: s["message_id"])

    uploaded = 0
    try:
        for n, shot in enumerate(shots, 1):
            info = bot.get_file(shot["file_id"])
            data = bot.download_file(info.file_path)
            put_image(r2, bucket, f"{prefix}{version}/{n:02d}{shot['ext']}", data, shot["mime"])
            uploaded += 1
    except Exception:
        log.exception("upload failed for %s%s", prefix, version)
        bot.reply_to(
            message,
            f"Upload failed after {uploaded} of {len(shots)}. The session is still open — "
            f"try /finish_job again, or /job_cancel to give up.",
        )
        timers.arm(message.chat.id, message.from_user.id)
        return

    state.delete()
    bot.reply_to(message, f"Filed {uploaded} screenshot(s) → {prefix}{version}/")


@bot.message_handler(commands=["job_cancel"], state=Session.collecting)
def job_cancel(message: Message, state: StateContext):
    timers.disarm(message.from_user.id)
    with state.data() as d:
        n = len(d.get("shots", []))
    state.delete()
    if n:
        bot.reply_to(message, f"Session discarded. {n} screenshot(s) dropped, nothing uploaded.")
    else:
        bot.reply_to(message, "Cancelled. Nothing was uploaded.")


@bot.message_handler(commands=["status"], state=Session.collecting)
def status_open(message: Message, state: StateContext):
    with state.data() as d:
        bot.reply_to(
            message,
            f"Open: {d['prefix']}{d['version']} — {len(d['shots'])} queued. "
            f"/finish_job to file, /job_cancel to discard.",
        )


# --------------------------------------------------------------------------
# Fallbacks.  No state= filter, so these only fire when nothing above matched.
# --------------------------------------------------------------------------

@bot.message_handler(commands=["status"])
def status_idle(message: Message):
    bot.reply_to(message, f"No session open. Start one with /job_start {DATE_EG}")


@bot.message_handler(commands=["finish_job", "job_cancel"])
def no_session(message: Message):
    bot.reply_to(message, f"No session open. Start one with /job_start {DATE_EG}")


@bot.message_handler(commands=["help", "start"])
def send_help(message: Message):
    bot.reply_to(message, HELP_TEXT, parse_mode="Markdown")


@bot.message_handler(content_types=["photo", "document"])
def stray_screenshot(message: Message):
    bot.reply_to(
        message,
        f"No session open — this was NOT saved. Run /job_start {DATE_EG} first, then resend.",
    )


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith("/"))
def unknown_command(message: Message):
    bot.reply_to(message, "Unknown command. /help lists what I understand.")


@bot.message_handler(content_types=["text"])
def fallback_text(message: Message):
    bot.reply_to(message, "I only handle screenshot sessions. /help for the commands.")


def publish_commands() -> None:
    """Fill the client's command menu. Cosmetic: handlers work without it."""
    commands = [
        BotCommand("job_start", f"Open a session for a date ({DATE_FMT})"),
        BotCommand("finish_job", "Upload the queued screenshots and close"),
        BotCommand("job_cancel", "Discard the session without uploading"),
        BotCommand("status", "Show the open session"),
        BotCommand("help", "How this bot works"),
    ]
    for user_id in config.allowed_user_ids:
        # Scoped, so the command list is not public even though the bot is.
        bot.set_my_commands(commands, scope=BotCommandScopeChat(user_id))


def main() -> None:
    publish_commands()
    log.info("polling started")
    bot.infinity_polling()


if __name__ == "__main__":
    main()
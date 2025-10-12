"""Telegram moderation bot for Goxxip box submissions."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()  # Load local .env if present.

logger = logging.getLogger("gossip_bot")

CHANNEL_ID: int | str | None = None
POLL_INTERVAL = 0
BATCH_LIMIT = 20
ADMINS_SET: frozenset[int] = frozenset()


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the Telegram bot."""

    bot_token: str
    channel_id_raw: str | None
    mongodb_uri: str
    db_name: str
    collection: str
    poll_interval: int
    batch_limit: int
    admin_ids: frozenset[int]

    @property
    def channel_id(self) -> int | str | None:
        """Return the parsed channel identifier (int or str)."""

        if not self.channel_id_raw:
            return None
        try:
            return int(self.channel_id_raw)
        except ValueError:
            return self.channel_id_raw

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with validation."""

        bot_token = (os.getenv("BOT_TOKEN") or "").strip()
        mongodb_uri = (
            os.getenv("MONGODB_URI")
            or os.getenv("MONGO_URL")
            or os.getenv("MONGODB_URL")
            or ""
        ).strip()
        db_name = os.getenv("DB_NAME", "gossip").strip()
        collection = os.getenv("COLLECTION", "submissions").strip()
        channel_id_raw = (os.getenv("CHANNEL_ID") or "").strip() or None
        poll_interval = int(os.getenv("POLL_INTERVAL", "0"))
        batch_limit = max(1, int(os.getenv("BATCH_LIMIT", "20")))
        admins_raw = (os.getenv("ADMINS") or "").strip()
        admin_ids = frozenset(
            int(item)
            for item in admins_raw.split(",")
            if item.strip().isdigit()
        )

        if not bot_token:
            raise RuntimeError("BOT_TOKEN is not configured")
        if not mongodb_uri:
            raise RuntimeError("MONGODB_URI is not configured")

        return cls(
            bot_token=bot_token,
            channel_id_raw=channel_id_raw,
            mongodb_uri=mongodb_uri,
            db_name=db_name,
            collection=collection,
            poll_interval=max(0, poll_interval),
            batch_limit=batch_limit,
            admin_ids=admin_ids,
        )


# MongoDB state is cached globally because Aiogram handlers do not accept dependencies.
mongo_client: AsyncIOMotorClient | None = None
col = None  # type: ignore[assignment]


def require_collection():
    """Return the active Mongo collection or raise if unavailable."""

    if col is None:
        raise RuntimeError("MongoDB collection is not initialised. Call init_mongo() first.")
    return col


async def init_mongo(settings: Settings) -> None:
    """Connect to MongoDB and prepare indexes."""

    global mongo_client, col

    mongo_client = AsyncIOMotorClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=10_000,
        retryWrites=True,
    )
    db = mongo_client[settings.db_name]
    col = db[settings.collection]
    await col.create_index([("status", 1), ("created_at", -1)])
    await col.create_index("created_at")
    await mongo_client.admin.command("ping")


def close_mongo() -> None:
    """Close the MongoDB client if it was initialised."""

    global mongo_client
    if mongo_client:
        mongo_client.close()
        mongo_client = None

# ---------- DB helpers ----------
async def add_submission(text: str, lang: str = "en") -> str:
    """Insert a new submission and return its identifier."""

    collection = require_collection()
    doc = {
        "text": text,
        "lang": lang,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    }
    res = await collection.insert_one(doc)
    return str(res.inserted_id)


async def list_pending(limit: int = 20) -> list[dict[str, Any]]:
    """Return newest pending submissions for manual review."""

    collection = require_collection()
    cursor = collection.find({"status": "pending"}).sort("created_at", -1).limit(limit)
    return [doc async for doc in cursor]


async def latest(limit: int = 10) -> list[dict[str, Any]]:
    """Return recently created submissions regardless of status."""

    collection = require_collection()
    cursor = collection.find({}).sort("created_at", -1).limit(limit)
    return [doc async for doc in cursor]


async def get_one(oid: str) -> dict[str, Any] | None:
    """Fetch a submission by Mongo ObjectId string."""

    collection = require_collection()
    try:
        _id = ObjectId(oid)
    except Exception:
        return None
    return await collection.find_one({"_id": _id})


async def set_status(oid: str, status: str) -> bool:
    """Update submission status and return success flag."""

    collection = require_collection()
    try:
        _id = ObjectId(oid)
    except Exception:
        return False
    res = await collection.update_one({"_id": _id}, {"$set": {"status": status}})
    return res.modified_count == 1


async def list_approved_for_publish(limit: int) -> list[dict[str, Any]]:
    """Return approved submissions in FIFO order for scheduled posting."""

    collection = require_collection()
    cursor = collection.find({"status": "approved"}).sort("created_at", 1).limit(limit)
    return [doc async for doc in cursor]


# ---------- Utils ----------
def esc_html(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

def format_submission(doc: dict[str, Any], short: bool = True) -> str:
    """Format a submission for Telegram with optional truncation."""

    _id = str(doc.get("_id"))
    text = doc.get("text", "")
    lang = doc.get("lang", "en")
    status = doc.get("status", "pending")
    created = doc.get("created_at")
    created_str = (
        created.strftime("%Y-%m-%d %H:%M UTC") if isinstance(created, datetime) else ""
    )
    if short and len(text) > 200:
        text = text[:200] + "…"
    return (
        f"<b>{_id}</b> [{lang}] <i>{status}</i>\n"
        f"{esc_html(text)}\n"
        f"<i>{created_str}</i>"
    )


def is_admin(message: Message) -> bool:
    """Return True when the sender is whitelisted (or no whitelist set)."""

    return bool(message.from_user and message.from_user.id in ADMINS_SET) or not ADMINS_SET

# ---------- Bot/Handlers ----------
router = Router()

@router.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Commands: /pending /latest /publish <id> /reject <id>"
        "\nOnly whitelisted moderators can manage submissions."
    )

@router.message(Command("pending"))
async def pending(message: Message):
    if not is_admin(message):
        return await message.answer("⛔️ Access denied.")
    items = await list_pending(20)
    if not items:
        return await message.answer("Queue is empty ✅")
    await message.answer("\n\n".join(format_submission(d) for d in items))

@router.message(Command("latest"))
async def cmd_latest(message: Message):
    if not is_admin(message):
        return await message.answer("⛔️ Access denied.")
    items = await latest(10)
    if not items:
        return await message.answer("No submissions yet.")
    await message.answer("\n\n".join(format_submission(d) for d in items))

@router.message(Command("publish"))
async def publish(message: Message):
    if not is_admin(message):
        return await message.answer("⛔️ Access denied.")
    if not CHANNEL_ID:
        return await message.answer("CHANNEL_ID is not configured.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: /publish <id>")
    oid = parts[1].strip()
    doc = await get_one(oid)
    if not doc:
        return await message.answer("Submission not found.")
    text = f"📝 <b>Anonymous submission</b>\n{esc_html(doc.get('text',''))}"
    await message.bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
    await set_status(oid, "published")
    await message.answer(f"Published: <code>{oid}</code> ✅")

@router.message(Command("reject"))
async def reject(message: Message):
    if not is_admin(message):
        return await message.answer("⛔️ Access denied.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: /reject <id>")
    oid = parts[1].strip()
    ok = await set_status(oid, "rejected")
    await message.answer("Rejected ✅" if ok else "Submission not found.")

# ---------- Background publisher ----------
async def publisher_worker(bot: Bot) -> None:
    """Background task that publishes approved submissions on schedule."""

    if not CHANNEL_ID or POLL_INTERVAL <= 0:
        return

    logger.info(
        "Publisher enabled: interval=%ss batch_limit=%s", POLL_INTERVAL, BATCH_LIMIT
    )

    while True:
        try:
            batch = await list_approved_for_publish(BATCH_LIMIT)
            if not batch:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for doc in batch:
                oid = str(doc["_id"])
                text = f"📝 <b>Anonymous submission</b>\n{esc_html(doc.get('text',''))}"
                await bot.send_message(
                    CHANNEL_ID, text, disable_web_page_preview=True
                )
                await set_status(oid, "published")
                logger.info("Published submission %s", oid)

            await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - we log and retry to keep the worker alive.
            logger.exception("Publisher worker failed; retrying in 5 seconds")
            await asyncio.sleep(5)


# ---------- Entrypoint ----------
async def main() -> None:
    """Entrypoint used by ``python gossip_bot.py``."""

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
    )

    settings = Settings.from_env()
    global CHANNEL_ID, POLL_INTERVAL, BATCH_LIMIT, ADMINS_SET
    CHANNEL_ID = settings.channel_id
    POLL_INTERVAL = settings.poll_interval
    BATCH_LIMIT = settings.batch_limit
    ADMINS_SET = settings.admin_ids

    await init_mongo(settings)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    logger.info(
        "Bot started as @%s (admins=%s)",
        me.username,
        ",".join(str(admin) for admin in sorted(ADMINS_SET)) or "<none>",
    )

    worker_task: asyncio.Task[None] | None = None
    if POLL_INTERVAL > 0 and CHANNEL_ID:
        worker_task = asyncio.create_task(publisher_worker(bot))

    try:
        await dp.start_polling(bot)
    finally:
        if worker_task:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        close_mongo()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

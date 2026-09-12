import os
import json
import re
import asyncio
import logging
from typing import List, Optional, Any
import asyncpg
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from copilot_ai import generate_operator_suggestions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("terminal")

DATABASE_URL = getattr(config, "DATABASE_URL", None) or os.getenv("DATABASE_URL")
BOT_TOKEN = (
        getattr(config, "BOT_TOKEN", None)
        or getattr(config, "TELEGRAM_BOT_TOKEN", None)
        or getattr(config, "TG_BOT_TOKEN", None)
        or os.getenv("BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TG_BOT_TOKEN")
)

app = FastAPI(title="Operator Story Terminal")
db_pool = None
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

LATEST_COPILOT_HINTS = {}
AUTOPILOT_ENABLED = False
AUTOPILOT_DELAY_SECONDS = 15
autopilot_task = None


class SendMessageRequest(BaseModel):
    session_id: int
    text: str
    split_messages: Optional[List[str]] = []
    attach_hints: bool = True
    custom_hints: Optional[List[str]] = []


class SendSinglePartRequest(BaseModel):
    session_id: int
    text: str
    attach_hints: bool = False
    custom_hints: Optional[List[str]] = []


class SendMediaRequest(BaseModel):
    session_id: int
    media_id: int
    caption: Optional[str] = None


class UpdateMediaRequest(BaseModel):
    media_id: int
    title: str


class DeleteMediaRequest(BaseModel):
    media_id: int


def build_inline_markup(hints: List[Any]) -> Optional[InlineKeyboardMarkup]:
    if not hints:
        return None
    keyboard = []
    for idx, h in enumerate(hints[:3]):
        phrase = h.get("text", "").strip() if isinstance(h, dict) else str(h).strip()
        if phrase:
            keyboard.append([
                InlineKeyboardButton(
                    text=phrase,
                    callback_data=f"h:{idx}"
                )
            ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def auto_split_into_messages(text: str) -> List[str]:
    cleaned = re.sub(r"^\[.*?\]\s*", "", (text or "")).strip()
    if not cleaned:
        return ["Привет!", "Как дела?"]

    match = re.match(r"^(.+?[.!?])\s+(.+)$", cleaned)
    if match:
        p1 = match.group(1).strip()
        p2 = match.group(2).strip()
        if len(p1) >= 3 and len(p2) >= 3:
            return [p1, p2]

    words = cleaned.split()
    if len(words) >= 4:
        mid = len(words) // 2
        return [" ".join(words[:mid]), " ".join(words[mid:])]

    return [cleaned, "😉"]


async def dispatch_actress_messages(session_id: int, user_id: int, tg_id: int, parts: List[str], hints: List[Any],
                                    attach_hints: bool):
    reply_markup = build_inline_markup(hints) if attach_hints else None

    raw_parts = [re.sub(r"^\[.*?\]\s*", "", str(p)).strip() for p in parts if p and str(p).strip()]
    if len(raw_parts) >= 2:
        clean_parts = [raw_parts[0], raw_parts[1]]
    else:
        clean_parts = auto_split_into_messages(raw_parts[0] if raw_parts else "Привет")

    p1, p2 = clean_parts[0], clean_parts[1]

    try:
        await bot.send_chat_action(chat_id=tg_id, action=ChatAction.TYPING)
        await bot.send_message(chat_id=tg_id, text=f"Алина: «{p1}»", reply_markup=None, parse_mode=None)
    except Exception as e:
        logger.error(f"❌ Part 1 error: {e}")
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id, sender_type, text, created_at) VALUES ($1, 'actress', $2, NOW());",
            session_id, p1
        )

    await asyncio.sleep(4.0)

    try:
        await bot.send_chat_action(chat_id=tg_id, action=ChatAction.TYPING)
        await bot.send_message(chat_id=tg_id, text=f"Алина: «{p2}»", reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"❌ Part 2 error: {e}")
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id, sender_type, text, created_at) VALUES ($1, 'actress', $2, NOW());",
            session_id, p2
        )
        await conn.execute("UPDATE users SET balance_messages = balance_messages - 1 WHERE id = $1;", user_id)
        await conn.execute(
            "UPDATE message_hold_queue SET status = 'released' WHERE user_id = $1 AND status != 'released';",
            user_id
        )
        await conn.execute("UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;", session_id)

    try:
        async with db_pool.acquire() as conn:
            session_row = await conn.fetchrow(
                "SELECT memory_state FROM character_sessions WHERE id = $1;", session_id
            )
            raw_mem = session_row["memory_state"] if session_row else {}
            mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

        suggestions = await generate_operator_suggestions(target_phrase=p2, memory_state=mem_dict,
                                                          sender_type="actress")
        LATEST_COPILOT_HINTS[session_id] = suggestions.get("user_hints", [])
        new_memory = suggestions.get("updated_memory")

        if new_memory:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE character_sessions SET memory_state = $1::jsonb WHERE id = $2;",
                    json.dumps(new_memory, ensure_ascii=False), session_id
                )
    except Exception as cp_err:
        logger.error(f"❌ Copilot auto error: {cp_err}")


async def autopilot_worker_loop():
    logger.info("Autopilot worker started.")
    while True:
        try:
            if AUTOPILOT_ENABLED and db_pool:
                async with db_pool.acquire() as conn:
                    pending_rows = await conn.fetch(
                        """
                        SELECT h.id as hold_id, h.user_id, h.message_text, h.created_at,
                               cs.id as session_id, cs.memory_state, u.telegram_id, u.balance_messages
                        FROM message_hold_queue h
                        JOIN users u ON u.id = h.user_id
                        JOIN character_sessions cs ON cs.user_id = h.user_id AND cs.session_status = 'active'
                        WHERE h.status IN ('pending', 'held') 
                          AND h.created_at <= NOW() - INTERVAL '1 second' * $1
                        ORDER BY h.id ASC
                        LIMIT 1;
                        """,
                        AUTOPILOT_DELAY_SECONDS
                    )

                    for row in pending_rows:
                        hold_id = row["hold_id"]
                        user_text = (row["message_text"] or "").strip()

                        if not user_text or user_text.isdigit():
                            await conn.execute("UPDATE message_hold_queue SET status = 'released' WHERE id = $1;", hold_id)
                            continue

                        updated = await conn.execute(
                            "UPDATE message_hold_queue SET status = 'processing' WHERE id = $1 AND status IN ('pending', 'held');",
                            hold_id
                        )
                        if updated == "UPDATE 0":
                            continue

                        if row["balance_messages"] <= 0:
                            await conn.execute("UPDATE message_hold_queue SET status = 'released' WHERE id = $1;", hold_id)
                            continue

                        session_id = row["session_id"]
                        tg_id = row["telegram_id"]
                        user_id = row["user_id"]
                        raw_mem = row["memory_state"]
                        mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

                        copilot_data = await generate_operator_suggestions(
                            target_phrase=user_text,
                            memory_state=mem_dict,
                            sender_type="user"
                        )
                        replies = copilot_data.get("actress_replies", [])
                        user_hints = copilot_data.get("user_hints", [])
                        new_mem = copilot_data.get("updated_memory", mem_dict)

                        chosen_reply = replies[1] if len(replies) > 1 else (replies[0] if replies else {})
                        parts = chosen_reply.get("split_messages")
                        if not parts or not isinstance(parts, list) or len(parts) < 2:
                            parts = auto_split_into_messages(chosen_reply.get("text", "..."))

                        await conn.execute(
                            "UPDATE character_sessions SET memory_state = $1::jsonb WHERE id = $2;",
                            json.dumps(new_mem, ensure_ascii=False), session_id
                        )

                        await dispatch_actress_messages(session_id, user_id, tg_id, parts, user_hints, True)

        except Exception as err:
            logger.error(f"[АВТОПИЛОТ ОШИБКА]: {err}", exc_info=True)

        await asyncio.sleep(3)


@app.on_event("startup")
async def startup():
    global db_pool, autopilot_task
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=20,
        command_timeout=7.0,
        ssl=False
    )
    autopilot_task = asyncio.create_task(autopilot_worker_loop())


@app.on_event("shutdown")
async def shutdown():
    if autopilot_task:
        autopilot_task.cancel()
    if db_pool:
        await db_pool.close()
    await bot.session.close()


@app.get("/", response_class=HTMLResponse)
async def serve_terminal_ui():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "terminal.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>templates/terminal.html не найден</h1>"


@app.get("/api/autopilot/status")
async def get_autopilot_status():
    return {"enabled": AUTOPILOT_ENABLED, "delay_sec": AUTOPILOT_DELAY_SECONDS}


@app.post("/api/autopilot/toggle")
async def toggle_autopilot():
    global AUTOPILOT_ENABLED
    AUTOPILOT_ENABLED = not AUTOPILOT_ENABLED
    return {"enabled": AUTOPILOT_ENABLED}


@app.get("/api/sessions")
async def get_active_sessions():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT cs.id, u.telegram_id, u.balance_messages, cs.updated_at,
                   COALESCE(
                       (
                           SELECT message_text FROM message_hold_queue 
                           WHERE user_id = u.id AND status != 'released'
                           ORDER BY id DESC LIMIT 1
                       ),
                       (
                           SELECT text FROM messages 
                           WHERE session_id = cs.id
                           ORDER BY id DESC LIMIT 1
                       ),
                       'Новый диалог'
                   ) as last_msg
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE cs.session_status = 'active' AND cs.deleted_at IS NULL
            ORDER BY cs.updated_at DESC;
            """
        )
        return [dict(r) for r in rows]


@app.get("/api/messages/{session_id}")
async def get_chat_messages(session_id: int):
    async with db_pool.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT cs.id, cs.user_id, cs.memory_state, u.telegram_id, u.balance_messages
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE cs.id = $1;
            """,
            session_id
        )

        if not session_row:
            return {"messages": [], "balance": 0, "telegram_id": 0, "memory_state": {}}

        user_id = session_row["user_id"]
        raw_mem = session_row["memory_state"]
        mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

        rows = await conn.fetch(
            """
            SELECT id, sender_type, COALESCE(text, '') as text, created_at
            FROM messages
            WHERE session_id = $1
            ORDER BY id ASC;
            """,
            session_id
        )

        pending = await conn.fetch(
            """
            SELECT id, 'user' as sender_type, message_text as text, created_at
            FROM message_hold_queue
            WHERE user_id = $1 AND status != 'released'
            ORDER BY id ASC;
            """,
            user_id
        )

        existing_texts = {r["text"] for r in rows}
        all_msgs = [
            {
                "id": r["id"],
                "sender_type": r["sender_type"],
                "text": r["text"],
                "created_at": str(r["created_at"])
            }
            for r in rows
        ]

        for p in pending:
            t = (p["text"] or "").strip()
            if t and t not in existing_texts:
                all_msgs.append({
                    "id": f"hold_{p['id']}",
                    "sender_type": "user",
                    "text": t,
                    "created_at": str(p["created_at"])
                })

        return {
            "messages": all_msgs,
            "balance": session_row["balance_messages"],
            "telegram_id": session_row["telegram_id"],
            "memory_state": mem_dict
        }


@app.get("/api/copilot/{session_id}")
async def get_copilot_hints(session_id: int, phrase: Optional[str] = None, sender: Optional[str] = "user"):
    target_phrase = (phrase or "").strip()
    author = sender or "user"
    mem_dict = {}

    async with db_pool.acquire() as conn:
        session_row = await conn.fetchrow(
            "SELECT user_id, memory_state FROM character_sessions WHERE id = $1;",
            session_id
        )
        if session_row:
            user_id = session_row["user_id"]
            raw_mem = session_row["memory_state"]
            mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

            if not target_phrase:
                hold_text = await conn.fetchval(
                    "SELECT message_text FROM message_hold_queue WHERE user_id = $1 AND status != 'released' ORDER BY id DESC LIMIT 1;",
                    user_id
                )
                if hold_text:
                    target_phrase = hold_text.strip()
                    author = "user"
                else:
                    msg_row = await conn.fetchrow(
                        "SELECT sender_type, text FROM messages WHERE session_id = $1 ORDER BY id DESC LIMIT 1;",
                        session_id
                    )
                    if msg_row and msg_row["text"]:
                        target_phrase = msg_row["text"].strip()
                        author = msg_row["sender_type"]

    if not target_phrase:
        target_phrase = "Привет, ты где пропал?"

    suggestions = await generate_operator_suggestions(
        target_phrase=target_phrase,
        memory_state=mem_dict,
        sender_type=author
    )
    LATEST_COPILOT_HINTS[session_id] = suggestions.get("user_hints", [])

    new_memory = suggestions.get("updated_memory")
    if new_memory:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE character_sessions SET memory_state = $1::jsonb WHERE id = $2;",
                json.dumps(new_memory, ensure_ascii=False), session_id
            )

    return suggestions


@app.get("/api/media")
async def get_media_catalog():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, media_type, category, duration_sec, cost_messages, file_path, file_id
            FROM media_assets
            ORDER BY category ASC, id ASC;
            """
        )
        return [dict(r) for r in rows]


@app.post("/api/media/update")
async def update_media_endpoint(req: UpdateMediaRequest):
    new_title = req.title.strip()
    if not new_title:
        return {"status": "error", "message": "Пустое название"}

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE media_assets SET title = $1 WHERE id = $2;", new_title, req.media_id)
    return {"status": "ok"}


@app.post("/api/media/delete")
async def delete_media_endpoint(req: DeleteMediaRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT file_path FROM media_assets WHERE id = $1;", req.media_id)
        if row and row["file_path"]:
            path = row["file_path"]
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(os.path.dirname(__file__), path))
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        await conn.execute("DELETE FROM media_assets WHERE id = $1;", req.media_id)
    return {"status": "ok"}


@app.post("/api/send_message_part")
async def send_message_part_endpoint(req: SendSinglePartRequest):
    async with db_pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT cs.id, cs.user_id, cs.memory_state, u.telegram_id, u.balance_messages
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE cs.id = $1;
            """,
            req.session_id
        )

        if not session or session["balance_messages"] <= 0:
            return {"status": "error", "message": "Недостаточно баланса сообщений"}

        tg_id = session["telegram_id"]
        user_id = session["user_id"]
        raw_mem = session["memory_state"]
        mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

    clean_text = re.sub(r"^\[.*?\]\s*", "", req.text).strip()

    reply_markup = None
    if req.attach_hints:
        hints = req.custom_hints
        if not hints:
            cached_hints = LATEST_COPILOT_HINTS.get(req.session_id, [])
            hints = cached_hints
        reply_markup = build_inline_markup(hints)

    try:
        await bot.send_chat_action(chat_id=tg_id, action=ChatAction.TYPING)
        await asyncio.sleep(0.3)
        await bot.send_message(chat_id=tg_id, text=f"Алина: «{clean_text}»", reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        logger.error(f"❌ Telegram send part error: {e}")
        return {"status": "error", "message": str(e)}

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (session_id, sender_type, text, created_at) VALUES ($1, 'actress', $2, NOW());",
            req.session_id, clean_text
        )
        if req.attach_hints:
            await conn.execute("UPDATE users SET balance_messages = balance_messages - 1 WHERE id = $1;", user_id)
            await conn.execute(
                "UPDATE message_hold_queue SET status = 'released' WHERE user_id = $1 AND status != 'released';",
                user_id
            )
        await conn.execute("UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;", req.session_id)

    return {"status": "ok"}


@app.post("/api/send_message")
async def send_message_endpoint(req: SendMessageRequest, bg_tasks: BackgroundTasks):
    async with db_pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT cs.id, cs.user_id, cs.memory_state, u.telegram_id, u.balance_messages
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE cs.id = $1;
            """,
            req.session_id
        )

        if not session or session["balance_messages"] <= 0:
            return {"status": "error", "message": "Недостаточно баланса сообщений"}

        tg_id = session["telegram_id"]
        user_id = session["user_id"]
        raw_mem = session["memory_state"]
        mem_dict = json.loads(raw_mem) if isinstance(raw_mem, str) else (raw_mem or {})

    if req.split_messages and isinstance(req.split_messages, list) and len(req.split_messages) >= 2:
        parts = [str(req.split_messages[0]).strip(), str(req.split_messages[1]).strip()]
    else:
        parts = auto_split_into_messages(req.text)

    hints_to_send = []
    if req.attach_hints:
        if req.custom_hints and isinstance(req.custom_hints, list):
            hints_to_send = [str(h).strip() for h in req.custom_hints if str(h).strip()]

        if not hints_to_send:
            cached_hints = LATEST_COPILOT_HINTS.get(req.session_id, [])
            if not cached_hints:
                copilot_data = await generate_operator_suggestions(
                    target_phrase=req.text,
                    memory_state=mem_dict,
                    sender_type="actress"
                )
                cached_hints = copilot_data.get("user_hints", [])
            hints_to_send = cached_hints

    bg_tasks.add_task(
        dispatch_actress_messages, req.session_id, user_id, tg_id, parts, hints_to_send, req.attach_hints
    )
    return {"status": "ok"}


@app.post("/api/send_media")
async def send_media_endpoint(req: SendMediaRequest):
    async with db_pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT cs.id, cs.user_id, u.telegram_id, u.balance_messages
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE cs.id = $1;
            """,
            req.session_id
        )
        media = await conn.fetchrow(
            """
            SELECT id, title, media_type, file_id, file_path, cost_messages 
            FROM media_assets 
            WHERE id = $1;
            """,
            req.media_id
        )

    if not session or not media:
        return {"status": "error", "message": "Данные не найдены"}

    cost = media["cost_messages"] or 1
    if session["balance_messages"] < cost:
        return {"status": "error", "message": f"Недостаточно баланса (нужно {cost})"}

    tg_id = session["telegram_id"]
    media_type = media["media_type"]
    file_id = media["file_id"]
    file_path = media["file_path"]

    try:
        if media_type == "video_note":
            await bot.send_chat_action(chat_id=tg_id, action=ChatAction.RECORD_VIDEO_NOTE)
        elif media_type == "voice":
            await bot.send_chat_action(chat_id=tg_id, action=ChatAction.RECORD_VOICE)
        elif media_type == "photo":
            await bot.send_chat_action(chat_id=tg_id, action=ChatAction.UPLOAD_PHOTO)
        await asyncio.sleep(0.8)
    except Exception:
        pass

    send_payload = file_id
    if not send_payload and file_path:
        path = file_path if os.path.isabs(file_path) else os.path.normpath(
            os.path.join(os.path.dirname(__file__), file_path))
        if os.path.exists(path):
            send_payload = FSInputFile(path)

    if not send_payload:
        return {"status": "error", "message": "Файл отсутствует на диске и нет file_id"}

    sent_msg = None
    try:
        if media_type == "video_note":
            sent_msg = await bot.send_video_note(chat_id=tg_id, video_note=send_payload)
        elif media_type == "voice":
            sent_msg = await bot.send_voice(chat_id=tg_id, voice=send_payload, caption=req.caption)
        elif media_type == "photo":
            sent_msg = await bot.send_photo(chat_id=tg_id, photo=send_payload, caption=req.caption)
    except Exception as e:
        logger.error(f"Media send error: {e}")
        return {"status": "error", "message": str(e)}

    if not file_id and sent_msg:
        new_fid = None
        if sent_msg.video_note:
            new_fid = sent_msg.video_note.file_id
        elif sent_msg.voice:
            new_fid = sent_msg.voice.file_id
        elif sent_msg.photo:
            new_fid = sent_msg.photo[-1].file_id

        if new_fid:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE media_assets SET file_id = $1 WHERE id = $2;", new_fid, media["id"])

    type_labels = {"video_note": "🎥 [Видеокружок]", "voice": "🎙 [Голосовое]", "photo": "📷 [Фото]"}
    label = f"{type_labels.get(media_type, '📁 [Медиа]')}: {media['title']}"

    async with db_pool.acquire() as conn:
        new_balance = session["balance_messages"] - cost
        await conn.execute("UPDATE users SET balance_messages = $1 WHERE id = $2;", new_balance, session["user_id"])
        await conn.execute(
            """
            INSERT INTO messages (session_id, sender_type, text, created_at)
            VALUES ($1, 'actress', $2, NOW());
            """,
            req.session_id, label
        )
        await conn.execute(
            "UPDATE message_hold_queue SET status = 'released' WHERE user_id = $1 AND status != 'released';",
            session["user_id"]
        )
        await conn.execute("UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;", req.session_id)

    return {"status": "ok", "new_balance": new_balance}
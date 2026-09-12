import os
import logging
from typing import Optional
import asyncpg
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

import config

logger = logging.getLogger("bot_chapter2")
router = Router(name="chapter2_router")

DATABASE_URL = getattr(config, "DATABASE_URL", None) or os.getenv("DATABASE_URL")
db_pool: Optional[asyncpg.Pool] = None


async def get_db_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=7.0,
            ssl=False
        )
    return db_pool


@router.callback_query(F.data.startswith("h:"))
async def handle_chapter2_callback(call: CallbackQuery, bot: Bot):
    try:
        await call.answer()
    except TelegramBadRequest:
        pass

    user_telegram_id = call.from_user.id
    selected_text = None

    # Достаем исходный полный текст кнопки прямо из разметки сообщения в Telegram
    if call.message and call.message.reply_markup:
        for row in call.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == call.data:
                    selected_text = btn.text.strip()
                    break
            if selected_text:
                break

    # Если текста нет или там осталась только цифра индекса (например, '0', '1') — отсекаем
    if not selected_text or selected_text.isdigit():
        raw_val = call.data[2:].strip()
        if not raw_val.isdigit():
            selected_text = raw_val
        else:
            logger.warning(f"Игнорируем пустой клик или клик по индексу: {call.data}")
            return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, balance_messages FROM users WHERE telegram_id = $1;",
            user_telegram_id
        )

        if not user_row:
            return

        user_id = user_row["id"]

        session_row = await conn.fetchrow(
            """
            SELECT id FROM character_sessions 
            WHERE user_id = $1 AND session_status = 'active' AND deleted_at IS NULL
            ORDER BY updated_at DESC LIMIT 1;
            """,
            user_id
        )

        if not session_row:
            return

        session_id = session_row["id"]

        await conn.execute(
            """
            INSERT INTO messages (session_id, sender_type, text, created_at)
            VALUES ($1, 'user', $2, NOW());
            """,
            session_id, selected_text
        )

        await conn.execute(
            """
            INSERT INTO message_hold_queue (user_id, session_id, message_text, status, created_at)
            VALUES ($1, $2, $3, 'held', NOW());
            """,
            user_id, session_id, selected_text
        )

        await conn.execute(
            "UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;",
            session_id
        )

    try:
        if call.message:
            await bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
    except Exception as e:
        logger.debug(f"Не удалось убрать разметку: {e}")

    try:
        await bot.send_message(
            chat_id=call.message.chat.id,
            text=f"💬 {selected_text}"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки реплики пользователя в TG: {e}")


@router.message(F.text, ~F.text.startswith("/"))
async def handle_chapter2_user_text(message: Message):
    user_telegram_id = message.from_user.id
    text = (message.text or "").strip()

    if not text:
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1;",
            user_telegram_id
        )

        if not user_row:
            return

        user_id = user_row["id"]

        session_row = await conn.fetchrow(
            """
            SELECT id FROM character_sessions 
            WHERE user_id = $1 AND session_status = 'active' AND deleted_at IS NULL
            ORDER BY updated_at DESC LIMIT 1;
            """,
            user_id
        )

        if not session_row:
            return

        session_id = session_row["id"]

        await conn.execute(
            """
            INSERT INTO messages (session_id, sender_type, text, created_at)
            VALUES ($1, 'user', $2, NOW());
            """,
            session_id, text
        )

        await conn.execute(
            """
            INSERT INTO message_hold_queue (user_id, session_id, message_text, status, created_at)
            VALUES ($1, $2, $3, 'held', NOW());
            """,
            user_id, session_id, text
        )

        await conn.execute(
            "UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;",
            session_id
        )
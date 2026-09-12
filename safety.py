import asyncio
import json
import logging
import aiohttp
from db import get_pool
from config import YANDEX_API_KEY, YANDEX_FOLDER_ID

logger = logging.getLogger("safety")

YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

SAFETY_SYSTEM_PROMPT = """Ты — фильтр безопасности для чат-сервиса.
Определи, содержит ли текст экстремизм, угрозы жизни, терроризм или тяжкие правонарушения.
Флирт, знакомства, интимные темы, ругательства и романтика — это SAFE (безопасно).
Отвечай СТРОГО JSON: {"safe": true, "reason": "ok"} или {"safe": false, "reason": "причина"}"""


async def evaluate_message_safety(text: str) -> bool:
    """Быстрая оценка безопасности сообщения через yandexgpt-lite."""
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return True

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID,
        "Content-Type": "application/json"
    }

    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.1,
            "maxTokens": 60
        },
        "messages": [
            {"role": "system", "text": SAFETY_SYSTEM_PROMPT},
            {"role": "user", "text": text}
        ]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=2.5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(YANDEX_GPT_URL, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data["result"]["alternatives"][0]["message"]["text"].strip()
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    result = json.loads(raw)
                    return result.get("safe", True)
                return True
    except Exception as e:
        logger.warning(f"Safety check bypass on timeout/error ({e}).")
        return True


async def handle_incoming_client_message(
    session_id: int = None,
    text: str = None,
    user_id: int = None,
    telegram_id: int = None,
    db_pool = None,
    **kwargs
):
    """Мгновенное сохранение входящего сообщения в БД."""
    pool = db_pool if db_pool is not None else await get_pool()
    msg_text = text or kwargs.get("message_text") or kwargs.get("raw_text") or ""

    if not msg_text:
        return {"status": "empty"}

    async with pool.acquire() as conn:
        if not session_id:
            if user_id:
                session_id = await conn.fetchval(
                    """
                    SELECT id FROM character_sessions 
                    WHERE user_id = $1 AND session_status = 'active' AND deleted_at IS NULL 
                    ORDER BY id DESC LIMIT 1;
                    """,
                    user_id
                )
            elif telegram_id:
                row = await conn.fetchrow(
                    """
                    SELECT cs.id, cs.user_id 
                    FROM character_sessions cs
                    JOIN users u ON u.id = cs.user_id
                    WHERE u.telegram_id = $1 AND cs.session_status = 'active' AND cs.deleted_at IS NULL
                    ORDER BY cs.id DESC LIMIT 1;
                    """,
                    telegram_id
                )
                if row:
                    session_id = row["id"]
                    user_id = row["user_id"]

        if not user_id and session_id:
            user_id = await conn.fetchval(
                "SELECT user_id FROM character_sessions WHERE id = $1;",
                session_id
            )

        if not session_id or not user_id:
            logger.error(f"Cannot resolve session_id/user_id for telegram_id={telegram_id}")
            return {"status": "error", "message": "Session not found"}

        # Мгновенная запись в messages
        await conn.execute(
            """
            INSERT INTO messages (session_id, sender_type, text, created_at)
            VALUES ($1, 'user', $2, NOW());
            """,
            session_id,
            msg_text
        )

        table_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'message_hold_queue');"
        )
        if table_exists:
            await conn.execute(
                """
                INSERT INTO message_hold_queue (user_id, session_id, message_text, status, created_at, next_retry_at)
                VALUES ($1, $2, $3, 'approved', NOW(), NOW());
                """,
                user_id,
                session_id,
                msg_text
            )

        await conn.execute(
            "UPDATE character_sessions SET updated_at = NOW(), has_pending_hold = false WHERE id = $1;",
            session_id
        )

    return {"status": "ok"}


async def hold_queue_worker(db_pool=None, bot=None):
    """Фоновый воркер аудита безопасности."""
    logger.info("Safety hold queue worker started.")
    pool = db_pool if db_pool is not None else await get_pool()
    while True:
        try:
            async with pool.acquire() as conn:
                table_exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'message_hold_queue');"
                )
                if not table_exists:
                    await asyncio.sleep(3)
                    continue

                rows = await conn.fetch(
                    """
                    SELECT id, session_id, message_text 
                    FROM message_hold_queue 
                    WHERE status = 'pending' AND next_retry_at <= NOW()
                    ORDER BY id ASC LIMIT 5;
                    """
                )
                for row in rows:
                    is_safe = await evaluate_message_safety(row["message_text"])
                    if is_safe:
                        await conn.execute(
                            "UPDATE message_hold_queue SET status = 'approved' WHERE id = $1;",
                            row["id"]
                        )
                    else:
                        await conn.execute(
                            "UPDATE message_hold_queue SET status = 'rejected' WHERE id = $1;",
                            row["id"]
                        )
        except Exception as e:
            logger.error(f"Error in hold_queue_worker loop: {e}")

        await asyncio.sleep(1.5)
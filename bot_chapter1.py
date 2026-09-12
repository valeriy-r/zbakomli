import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart
from db import get_pool

router = Router()
logger = logging.getLogger(__name__)

menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💎 Баланс"), KeyboardButton(text="ℹ️ О новелле")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие или пиши текст..."
)


async def get_or_create_user(telegram_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id, balance_messages)
            VALUES ($1, 1000)
            ON CONFLICT (telegram_id) 
            DO NOTHING
            RETURNING id, balance_messages;
            """,
            telegram_id
        )
        if not user:
            user = await conn.fetchrow(
                "SELECT id, balance_messages FROM users WHERE telegram_id = $1;",
                telegram_id
            )
        return user["id"], user["balance_messages"]


@router.message(CommandStart())
async def cmd_start(message: Message):
    try:
        user_id, balance = await get_or_create_user(message.from_user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO characters (id, name) VALUES (1, 'Алина') ON CONFLICT (id) DO NOTHING;")
            await conn.execute("INSERT INTO actresses (id, encrypted_pii, telegram_id, status) VALUES (1, '\\x00', 0, 'online') ON CONFLICT (id) DO NOTHING;")

            await conn.execute(
                """
                INSERT INTO character_sessions (user_id, character_id, actress_id, current_chapter, current_act, session_status, updated_at)
                VALUES ($1, 1, 1, 2, 1, 'active', NOW())
                ON CONFLICT (user_id, character_id, attempt_number) WHERE session_status = 'active' AND deleted_at IS NULL
                DO UPDATE SET updated_at = NOW();
                """,
                user_id
            )

        welcome_text = (
            "🔥 Добро пожаловать в <b>Interactive Story Terminal</b>... если готов зайти за грань обычной переписки.\n\n"
            "Ты попадаешь в иммерсивную новеллу, где правила диктуете вы вдвоем. Твоя собеседница — <b>Алина</b> (22 года). "
            "Дерзкая, чувственная, острая на язык, она умеет поддержать любой откровенный разговор и поставить на место одной фразой. "
            "Здесь нет скучных шаблонов — только химия, флирт и нарастающее напряжение, которое зависит исключительно от тебя.\n\n"
            "📌 <b>Что тебя ждет в этом мире:</b>\n"
            "• 💋 <b>Без цензуры на эмоции и желания:</b> Интимные темы, тайные фантазии, провокации и ночные разговоры по душам — всё зависит от того, насколько далеко ты готов зайти.\n"
            "• 🍷 <b>Динамическая шкала близости (1–5):</b> Каждое твое слово влияет на её настроение и доверие. Раскрой её скрытые стороны и узнай то, о чем она не говорит другим.\n"
            "• 📸 <b>Особый контент:</b> Пикантные фотографии, чувственные голосовые и личные видеокружки, которые она отправляет «только для твоих глаз».\n"
            "• 💳 <b>Твой баланс:</b> На счету уже зачислено <b>" + str(balance) + "</b> сообщений для старта[cite: 2].\n\n"
            "👉 <b>Рискнешь сделать первый шаг? Напиши ей то, о чем другие боялись даже подумать... 😉</b>"
        )

        await message.answer(welcome_text, parse_mode="HTML", reply_markup=menu_keyboard)
    except Exception as e:
        logger.error(f"Ошибка при выполнении /start: {e}", exc_info=True)


@router.message(F.text == "💎 Баланс")
async def check_balance_btn(message: Message):
    _, balance = await get_or_create_user(message.from_user.id)
    await message.answer(f"💳 Твой текущий баланс: <b>{balance}</b> сообщений.", parse_mode="HTML")


@router.message(F.text == "ℹ️ О новелле")
async def about_novel_btn(message: Message):
    info_text = (
        "📖 <b>Об истории:</b>\n"
        "Ты общаешься с реальным персонажем в режиме реального времени. "
        "Каждое сообщение расходует баланс. Если сообщения закончатся, ты сможешь пополнить их для продолжения истории.\n\n"
        "Просто отправляй текст в чат или нажимай интерактивные кнопки выбора реплик."
    )
    await message.answer(info_text, parse_mode="HTML")


@router.callback_query(F.data.startswith("hint_"))
async def handle_user_hint_selection(call: CallbackQuery, bot: Bot):
    await call.answer()

    user_reply_text = None
    if call.message and call.message.reply_markup:
        for row in call.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == call.data:
                    user_reply_text = btn.text
                    break
            if user_reply_text:
                break

    if not user_reply_text:
        user_reply_text = call.data.replace("hint_", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT cs.id, cs.user_id 
            FROM character_sessions cs
            JOIN users u ON u.id = cs.user_id
            WHERE u.telegram_id = $1 AND cs.session_status = 'active' AND cs.deleted_at IS NULL
            ORDER BY cs.id DESC LIMIT 1;
            """,
            call.from_user.id
        )

        if not session:
            return

        session_id = session["id"]

        await conn.execute(
            """
            INSERT INTO messages (session_id, sender_type, text, created_at)
            VALUES ($1, 'user', $2, NOW());
            """,
            session_id,
            user_reply_text
        )

        await conn.execute(
            "UPDATE character_sessions SET updated_at = NOW() WHERE id = $1;",
            session_id
        )

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.message.answer(f"Ты: {user_reply_text}")
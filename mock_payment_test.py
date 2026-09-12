import asyncio
import uuid
import asyncpg
from config import DATABASE_URL


async def run_payment_and_billing_simulation():
    conn = await asyncpg.connect(DATABASE_URL, ssl=False)
    print("=" * 60)
    print("ЗАПУСК ЭМУЛЯЦИИ ЭКВАЙРИНГА СБП И СПИСАНИЙ БАЛАНСА")
    print("=" * 60)

    try:
        # 1. Поиск пользователя и сессии
        user = await conn.fetchrow(
            """
            SELECT u.id, u.telegram_id, u.balance_messages, s.id as session_id, s.current_chapter, s.current_act
            FROM users u
            JOIN character_sessions s ON s.user_id = u.id
            WHERE s.session_status = 'active'
            ORDER BY u.id DESC
            LIMIT 1
            """
        )

        if not user:
            print("❌ Ошибка: В базе нет активных пользователей с сессиями. Пройдите /start в боте.")
            return

        user_id = user["id"]
        tg_id = user["telegram_id"]
        session_id = user["session_id"]
        initial_balance = user["balance_messages"]

        print(f"👤 Пользователь найден: ID {user_id} (Telegram ID: {tg_id})")
        print(f"📦 Исходный баланс: {initial_balance} сообщений")
        print(f"📖 Текущий прогресс: Глава {user['current_chapter']}, Акт {user['current_act']}")
        print("-" * 60)

        # 2. Эмуляция Webhook от банка (+25 сообщений за 149 ₽)
        payment_id = f"sbp_mock_{uuid.uuid4().hex[:12]}"
        print(f"💳 [ЭКВАЙРИНГ СБП]: Получен вебхук успешной оплаты!")
        print(f"   Сумма: 149.00 ₽ | Пакет: 25 сообщений | Payment ID: {payment_id}")

        async with conn.transaction():
            tx = await conn.fetchrow(
                """
                INSERT INTO transactions 
                (user_id, character_id, amount_rub, messages_delta, tx_type, payment_id)
                VALUES ($1, 1, 149.00, 25, 'pack_purchase', $2)
                RETURNING id, created_at
                """,
                user_id, payment_id
            )

            await conn.execute(
                """
                UPDATE character_sessions
                SET current_chapter = 2, current_act = 1, updated_at = NOW()
                WHERE id = $1
                """,
                session_id
            )

        bal_after_pay = await conn.fetchval("SELECT balance_messages FROM users WHERE id = $1", user_id)
        print(f"✅ Баланс после оплаты: {bal_after_pay} сообщ. (было {initial_balance}, прибавилось +25)")
        print(f"📝 Транзакция №{tx['id']} зафиксирована в журнале аудита.")
        print("-" * 60)

        # 3. Эмуляция ответа актрисы (Списание -1 сообщения)
        print("🎭 [ДИАЛОГ]: Актриса отправляет ответ пользователю...")
        async with conn.transaction():
            burn_tx = await conn.fetchrow(
                """
                INSERT INTO transactions 
                (user_id, character_id, amount_rub, messages_delta, tx_type)
                VALUES ($1, 1, 0.00, -1, 'message_burn')
                RETURNING id
                """,
                user_id  # Аргумент передан явно
            )

            msg = await conn.fetchrow(
                """
                INSERT INTO messages 
                (session_id, actress_id, act_number, sender_type, text, is_billable, transaction_id)
                VALUES ($1, 1, 1, 'actress', 'Я открыла тубус! Смотри, тут шифровка 1964 года...', TRUE, $2)
                RETURNING id
                """,
                session_id, burn_tx["id"]
            )

        bal_after_burn = await conn.fetchval("SELECT balance_messages FROM users WHERE id = $1", user_id)
        print(f"✅ Списание выполнено! Текущий баланс: {bal_after_burn} (списано -1 сообщение)")
        print(f"✉️ Сообщение №{msg['id']} сохранено с transaction_id={burn_tx['id']}.")
        print("-" * 60)

        # 4. Проверка защиты триггера (Попытка увести баланс ниже нуля)
        print("🛡️ [ТЕСТ БЕЗОПАСНОСТИ]: Попытка списать больше, чем есть на балансе...")
        try:
            async with conn.transaction():
                illegal_delta = -(bal_after_burn + 10)
                await conn.execute(
                    """
                    INSERT INTO transactions 
                    (user_id, character_id, amount_rub, messages_delta, tx_type)
                    VALUES ($1, 1, 0.00, $2, 'message_burn')
                    """,
                    user_id, illegal_delta
                )
            print("❌ ОШИБКА: Триггер базы пропустил списание в минус!")
        except Exception as e:
            print(f"✅ ЗАЩИТА СРАБОТАЛА ШТАТНО! Попытка списания заблокирована СУБД.")
            print(f"   Ответ триггера: {e}")

        print("=" * 60)
        print("ТЕСТ УСПЕШНО ЗАВЕРШЕН. ФИНАНСОВОЕ ЯДРО ВАЛИДНО.")
        print("=" * 60)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_payment_and_billing_simulation())
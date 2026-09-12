import asyncpg
import logging
from config import DATABASE_URL

logger = logging.getLogger("db")
pool: asyncpg.Pool = None

DDL_SCRIPT = """
-- 1. Персонажи
CREATE TABLE IF NOT EXISTS characters (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Пользователи
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    balance_messages INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
);

-- 3. Блок-лист несовершеннолетних (перманентный, переживает /delete_me)
CREATE TABLE IF NOT EXISTS blocked_underage_users (
    telegram_id BIGINT PRIMARY KEY,
    blocked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reason VARCHAR(64) DEFAULT 'underage_detected'
);

-- 4. Изолированная связка с аналитикой (разрывается при /delete_me)
CREATE TABLE IF NOT EXISTS user_analytics_links (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    analytics_uuid UUID NOT NULL UNIQUE
);

-- 5. Исполнительницы
CREATE TABLE IF NOT EXISTS actresses (
    id SERIAL PRIMARY KEY,
    encrypted_pii BYTEA NOT NULL,
    telegram_id BIGINT UNIQUE NOT NULL,
    status VARCHAR(20) DEFAULT 'offline',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 6. Игровые сессии
CREATE TABLE IF NOT EXISTS character_sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    character_id INT REFERENCES characters(id) NOT NULL,
    actress_id INT REFERENCES actresses(id) NOT NULL,
    attempt_number INT DEFAULT 1,
    current_chapter INT DEFAULT 1,
    current_act INT DEFAULT 1,
    act_message_count INT DEFAULT 0,
    msg_seq_counter INT DEFAULT 0,
    has_pending_hold BOOLEAN DEFAULT FALSE,
    session_status VARCHAR(20) DEFAULT 'active',
    user_dossier JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_character_session 
ON character_sessions (user_id, character_id, attempt_number) 
WHERE session_status = 'active' AND deleted_at IS NULL;

-- 7. Финансовый журнал
CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) NOT NULL,
    character_id INT REFERENCES characters(id) NOT NULL,
    amount_rub NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    messages_delta INT NOT NULL,
    tx_type VARCHAR(32) NOT NULL,
    payment_id VARCHAR(128) UNIQUE,
    compensates_tx_id BIGINT REFERENCES transactions(id) NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT chk_tx_delta_consistency CHECK (
        (tx_type = 'message_burn' AND messages_delta < 0 AND amount_rub = 0.00) OR
        (tx_type = 'burn_compensation' AND messages_delta > 0 AND amount_rub = 0.00 AND compensates_tx_id IS NOT NULL) OR
        (tx_type = 'pack_purchase' AND messages_delta > 0 AND amount_rub > 0.00) OR
        (tx_type = 'bonus_migration' AND messages_delta > 0 AND amount_rub = 0.00) OR
        (tx_type = 'refund_payout' AND messages_delta < 0 AND amount_rub < 0.00)
    )
);

-- Триггер аварийной защиты баланса
CREATE OR REPLACE FUNCTION sync_user_balance_from_tx()
RETURNS TRIGGER AS $$
DECLARE
    current_bal INT;
BEGIN
    SELECT balance_messages INTO current_bal 
    FROM users 
    WHERE id = NEW.user_id;

    IF (current_bal + NEW.messages_delta) < 0 THEN
        RAISE EXCEPTION 'Balance integrity violation: balance % cannot be decreased by %', 
            current_bal, NEW.messages_delta;
    END IF;

    UPDATE users 
    SET balance_messages = balance_messages + NEW.messages_delta
    WHERE id = NEW.user_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_apply_transaction_to_balance ON transactions;
CREATE TRIGGER trg_apply_transaction_to_balance
AFTER INSERT ON transactions
FOR EACH ROW
EXECUTE FUNCTION sync_user_balance_from_tx();

-- 8. Сообщения диалога
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT REFERENCES character_sessions(id) ON DELETE CASCADE,
    actress_id INT REFERENCES actresses(id),
    client_seq INT DEFAULT 0,
    act_number INT NOT NULL DEFAULT 1,
    sender_type VARCHAR(16) NOT NULL,
    text TEXT NULL,
    media_type VARCHAR(16),
    media_duration_sec INT DEFAULT 0,
    is_billable BOOLEAN DEFAULT TRUE,
    distress_marker BOOLEAN DEFAULT FALSE,
    delivery_status VARCHAR(16) DEFAULT 'sent',
    idempotency_key UUID UNIQUE,
    transaction_id BIGINT REFERENCES transactions(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    redacted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_distress_window 
ON messages (session_id, sender_type, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_messages_pending_reaper
ON messages (created_at)
WHERE delivery_status = 'pending';

-- 9. Очередь удержания сообщений (Hold Queue)
CREATE TABLE IF NOT EXISTS message_hold_queue (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) NOT NULL,
    session_id BIGINT REFERENCES character_sessions(id) ON DELETE CASCADE NOT NULL,
    client_seq INT NOT NULL DEFAULT 0,
    message_text TEXT NOT NULL,
    user_history_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    attempts INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'pending',
    last_error TEXT NULL,
    locked_at TIMESTAMP WITH TIME ZONE NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    next_retry_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hold_queue_worker_fetch 
ON message_hold_queue (session_id, status, next_retry_at, client_seq ASC);

-- 10. Журнал безопасности (сохраняется при удалении аккаунта)
CREATE TABLE IF NOT EXISTS safety_audit_logs (
    id BIGSERIAL PRIMARY KEY,
    analytics_uuid UUID NOT NULL,
    safety_level VARCHAR(16) NOT NULL,
    trigger_source VARCHAR(16) NOT NULL,
    action_taken VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 11. Аналитические события воронки
CREATE TABLE IF NOT EXISTS analytics_events (
    id BIGSERIAL PRIMARY KEY,
    analytics_uuid UUID NOT NULL,
    character_id INT NOT NULL,
    actress_id INT NOT NULL,
    event_name VARCHAR(64) NOT NULL,
    step_number INT,
    input_type VARCHAR(16),
    duration_ms INT,
    paywall_variant VARCHAR(16),
    payload JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        ssl=False,
        command_timeout=60
    )
    async with pool.acquire() as conn:
        await conn.execute(DDL_SCRIPT)
        await conn.execute("INSERT INTO characters (id, name) VALUES (1, 'Алина') ON CONFLICT (id) DO NOTHING")
        await conn.execute("""
            INSERT INTO actresses (id, encrypted_pii, telegram_id, status)
            VALUES (1, '\\x00', 0, 'online')
            ON CONFLICT (id) DO NOTHING
        """)
    logger.info("База данных успешно инициализирована.")

async def close_db():
    global pool
    if pool:
        await pool.close()
        logger.info("Пул соединений с БД закрыт.")

def get_pool() -> asyncpg.Pool:
    return pool
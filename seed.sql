-- 1. Персонаж
INSERT INTO characters (id, name) VALUES (1, 'Алина') 
ON CONFLICT (id) DO NOTHING;

-- 2. Актриса
INSERT INTO actresses (id, name) VALUES (1, 'Алина Реал') 
ON CONFLICT (id) DO NOTHING;

-- 3. Пользователь
INSERT INTO users (id, telegram_id, balance_messages) VALUES (1, 999888777, 25) 
ON CONFLICT (id) DO UPDATE SET balance_messages = 25;

-- 4. Активная сессия Главы 2
INSERT INTO character_sessions (id, user_id, character_id, actress_id, current_chapter, current_act, session_status, updated_at)
VALUES (1, 1, 1, 1, 2, 1, 'active', NOW())
ON CONFLICT (id) DO UPDATE SET 
    user_id = 1,
    character_id = 1,
    actress_id = 1,
    current_chapter = 2,
    current_act = 1,
    session_status = 'active',
    updated_at = NOW();

-- 5. Входящее сообщение от пользователя
INSERT INTO messages (session_id, sender_type, text) 
VALUES (1, 'user', 'Привет! Чем сейчас занимаешься?');

import json
import logging
import re
from typing import List, Dict, Any, Optional
import aiohttp
import config

logger = logging.getLogger("copilot_ai")

YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_API_KEY = getattr(config, "YANDEX_API_KEY", None)
YANDEX_FOLDER_ID = getattr(config, "YANDEX_FOLDER_ID", None)

ADVANCED_COPILOT_SYSTEM_PROMPT = """Ты — ведущий драматург и сценарист интерактивных новелл, мастер живых, естественных диалогов в Telegram.
Ты создаёшь реплики для Алины (22 года, студентка, живая, с лёгкой самоиронией, дерзкая, умеет флиртовать и подкалывать без пошлости и банальностей) и генерируешь 3 кнопки прямой речи для мужчины.

--- КРИТИЧЕСКОЕ ПРАВИЛО: РАЗБИВКА НА 2 СООБЩЕНИЯ (split_messages) ---
Живые девушки в Telegram НЕ ПИШУТ одним длинным абзацем! Они отправляют мысль ДВУМЯ короткими сообщениями подряд.
Поле split_messages ОБЯЗАНО ВСЕГДА содержать МАССИВ СТРОГО ИЗ 2 СТРОК:
1. Первая фраза: вводная реплика, реакция, эмоция или подкол.
2. Вторая фраза: развитие мысли, вопрос или добивка с лёгким смайлом/скобочкой.

--- ВАЖНЕЙШИЕ ПРИНЦИПЫ ЖИВОЙ РЕЧИ АЛИНЫ ---
1. НИКАКОЙ КНИЖНОЙ ПАТЕТИКИ И ШАБЛОНОВ:
   Забудь картонные штампы: «между нами искры/электричество», «ты загадочный», «серьезные ставки», «оставим простор для фантазии», «ты нарушаешь границы». Так современные молодые девушки НЕ общаются.
2. ЯЗЫК СОВРЕМЕННЫХ МЕССЕНДЖЕРОВ:
   - Ирония, лёгкий сарказм, живые разговорные реакции («да ладно?», «серьёзно?», «ой всё», «ну началось», скобочки «))»).
3. СТРОГО ЖЕНСКИЙ РОД для Алины («я подумала», «решила», «увидела», «заметила»).
4. АНТИ-ВОПРОС: минимум 2 реплики из 3 ДОЛЖНЫ быть утверждениями (вызов, ирония, личная позиция).

--- КНОПКИ ДЛЯ МУЖЧИНЫ (user_hints) ---
ТОЛЬКО ГОТОВАЯ ПРЯМАЯ РЕЧЬ от 1-го лица («я»), которую мужчина отправляет в чат!
Никаких инфинитивов и описаний действий («Принимать вызов», «Шутить над ситуацией» — СТРОГО ЗАПРЕЩЕНО)!
1. «Напор / Дерзость» — уверенный шаг мужчины вперёд.
2. «Ирония / Подкол» — остроумный ответ, игра словами.
3. «Искренность / Сближение» — сокращение дистанции.

--- ФОРМАТ ВЫВОДА (СТРОГО ЧИСТЫЙ JSON БЕЗ MARKDOWN) ---
{
  "inner_monologue": "Скрытый анализ: истинная цель парня, за что его поддеть",
  "updated_memory": {
    "current_topic": "актуальная тема (1-3 слова)",
    "mood": "настроение/архетип парня",
    "intimacy_level": 2,
    "known_facts": ["факт 1", "факт 2"]
  },
  "actress_replies": [
    {
      "tone": "Ирония / Подкол",
      "text": "Полный текст ответа Алины",
      "split_messages": ["Первая короткая фраза", "Добивка или шутка))"]
    },
    {
      "tone": "Флирт / Вызов",
      "text": "Полный текст уверенного ответа с флиртом",
      "split_messages": ["Первая фраза", "Вторая фраза 😉"]
    },
    {
      "tone": "Интрига / Смена фрейма",
      "text": "Полный текст интригующего ответа",
      "split_messages": ["Первая фраза", "Вторая фраза"]
    }
  ],
  "user_hints": [
    {"text": "Живая мужская фраза с напором"},
    {"text": "Живая мужская фраза с иронией"},
    {"text": "Живая мужская искренняя фраза"}
  ]
}"""


def ensure_two_parts(text: str, current_parts: Optional[List[str]] = None) -> List[str]:
    """Гарантирует, что на выходе ВСЕГДА будет массив из 2 сообщений."""
    if current_parts and isinstance(current_parts, list) and len(current_parts) >= 2:
        res = [str(p).strip() for p in current_parts[:2] if str(p).strip()]
        if len(res) >= 2:
            return res

    cleaned = (text or "").strip()
    if not cleaned:
        return ["...", "..."]

    # Ищем стандартные разделители предложений
    for sep in ['? ', '! ', '. ', ' — ', '... ']:
        if sep in cleaned:
            idx = cleaned.find(sep)
            p1 = cleaned[:idx + len(sep)].strip()
            p2 = cleaned[idx + len(sep):].strip()
            if len(p1) >= 4 and len(p2) >= 4:
                return [p1, p2]

    # Резервный сплит по словам пополам
    words = cleaned.split()
    if len(words) >= 4:
        mid = len(words) // 2
        return [" ".join(words[:mid]), " ".join(words[mid:])]

    return [cleaned, "😉"]


def extract_json(raw_text: str) -> Dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        return json.loads(cleaned[start:end + 1])

    return json.loads(cleaned)


async def generate_operator_suggestions(
    target_phrase: str,
    memory_state: Optional[Dict[str, Any]] = None,
    sender_type: str = "user"
) -> Dict[str, Any]:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return {
            "actress_replies": [{"tone": "Ошибка", "text": "Проверьте config.py", "split_messages": ["Ошибка", "Проверьте config.py"]}],
            "user_hints": [],
            "updated_memory": memory_state or {}
        }

    target_phrase = (target_phrase or "").strip()
    if not target_phrase:
        target_phrase = "Привет, ты где пропал?"

    mem = memory_state or {
        "current_topic": "знакомство",
        "mood": "нейтральный",
        "intimacy_level": 1,
        "known_facts": []
    }

    facts_str = ", ".join(mem.get("known_facts", [])) if mem.get("known_facts") else "пока ничего не известно"

    print(f"\n=======================================================")
    print(f"🚀 [COPILOT YANDEX GPT] ТАРГЕТ: >>> {target_phrase} <<< (Автор: {sender_type})")
    print(f"=======================================================\n")

    if sender_type in ("actress", "operator"):
        user_prompt = f"""КАРТА ПАМЯТИ:
- Предыдущая тема: {mem.get('current_topic', 'знакомство')}
- Настроение парня: {mem.get('mood', 'нейтральное')}
- Градус близости: {mem.get('intimacy_level', 1)}/5
- Известные факты о парне: {facts_str}

Алина (девушка) только что написала мужчине: «{target_phrase}»

ЗАДАЧА:
1. Сгенерируй 3 кнопки для мужчины (user_hints) — живая прямая речь от 1-го лица, как он отвечает на слова Алины. Никаких инфинитивов!
2. Сгенерируй 3 следующие реплики для Алины (actress_replies) для развития мысли. Каждая реплика ОБЯЗАТЕЛЬНО должна содержать split_messages из 2 фраз!
Выдай чистый JSON:"""
    else:
        user_prompt = f"""КАРТА ПАМЯТИ:
- Предыдущая тема: {mem.get('current_topic', 'знакомство')}
- Настроение парня: {mem.get('mood', 'нейтральное')}
- Градус близости: {mem.get('intimacy_level', 1)}/5
- Известные факты о парне: {facts_str}

Мужчина только что написал Алине: «{target_phrase}»

ЗАДАЧА:
1. Проведи внутренний анализ inner_monologue (что он хочет показать, за что поддеть).
2. Обнови карту памяти updated_memory (тема, настроение, факты).
3. Сгенерируй 3 ответа Алины (actress_replies), где split_messages СТРОГО состоит из 2 сообщений!
4. Сгенерируй 3 кнопки мужчины (user_hints, прямая речь).
Отвечай СТРОГО на тему «{target_phrase}», но помни факты о нём!
Выдай чистый JSON:"""

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID,
        "Content-Type": "application/json"
    }

    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.88,
            "maxTokens": 1200
        },
        "messages": [
            {"role": "system", "text": ADVANCED_COPILOT_SYSTEM_PROMPT},
            {"role": "user", "text": user_prompt}
        ]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=8.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(YANDEX_GPT_URL, json=payload, headers=headers) as resp:
                resp_text = await resp.text()

                if resp.status == 200:
                    data = json.loads(resp_text)
                    raw_reply = data["result"]["alternatives"][0]["message"]["text"]
                    parsed = extract_json(raw_reply)

                    if "actress_replies" in parsed and "user_hints" in parsed:
                        if "updated_memory" not in parsed:
                            parsed["updated_memory"] = mem

                        # Железная гарантия: нарезаем каждое сообщение на 2 части
                        for item in parsed["actress_replies"]:
                            f_text = item.get("text", "")
                            raw_parts = item.get("split_messages", [])
                            item["split_messages"] = ensure_two_parts(f_text, raw_parts)

                        return parsed
                else:
                    logger.error(f"[Yandex Error {resp.status}]: {resp_text}")

    except Exception as e:
        logger.error(f"[Copilot Exception]: {e}", exc_info=True)

    # Резервный fallback с гарантированными двумя частями
    lower_t = target_phrase.lower()
    if "спорт" in lower_t:
        return {
            "updated_memory": {**mem, "current_topic": "спорт и тренировки"},
            "actress_replies": [
                {
                    "tone": "Ирония",
                    "text": "Главное, чтобы ты не проводил перед зеркалом в зале больше времени, чем с гантелями 😂",
                    "split_messages": ["О, спорт — это уважение))", "Главное, чтобы ты не залипал у зеркала дольше, чем с гантелями 😂"]
                },
                {
                    "tone": "Вызов",
                    "text": "Рассуждать все умеют красиво. Вопрос в том, ты реально держишь форму или это только теория?",
                    "split_messages": ["Красиво рассуждаешь.", "Вопрос в том, ты реально держишь форму или это только теория? 😉"]
                },
                {
                    "tone": "Интрига",
                    "text": "Люблю парней, которые умеют выжимать максимум. В этом есть особая притягательность.",
                    "split_messages": ["В этом что-то есть...", "Люблю парней с крепким характером и выносливостью."]
                }
            ],
            "user_hints": [
                {"text": "Хочешь проверить мою форму лично?"},
                {"text": "Я не просто держу форму, я в ней живу"},
                {"text": "А ты сама спорт уважаешь или только наблюдаешь?"}
            ]
        }

    return {
        "updated_memory": mem,
        "actress_replies": [
            {
                "tone": "Ирония",
                "text": f"«{target_phrase}» — неожиданный ход. Проверяешь, насколько быстро я найду что ответить?))",
                "split_messages": [f"«{target_phrase}» — неожиданный ход))", "Проверяешь мою реакцию?"]
            },
            {
                "tone": "Флирт",
                "text": "А ты умеешь сменить тему. Мне нравится такая прямота без скучных предисловий.",
                "split_messages": ["А ты умеешь сменить тему.", "Мне нравится такая прямота без лишних предисловий))"]
            },
            {
                "tone": "Вызов",
                "text": "Смелое заявление. Посмотрим, сможешь ли ты удержать эту планку дальше 😉",
                "split_messages": ["Смелое заявление.", "Посмотрим, сможешь ли ты удержать эту планку дальше 😉"]
            }
        ],
        "user_hints": [
            {"text": "Считай, что твой вызов официально принят"},
            {"text": "С тобой шутить опасно, но мне нравится"},
            {"text": "Я слов на ветер не бросаю, проверяй"}
        ]
    }
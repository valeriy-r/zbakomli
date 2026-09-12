import asyncio
import asyncpg
import config

SQL = """
ALTER TABLE character_sessions 
ADD COLUMN IF NOT EXISTS memory_state JSONB DEFAULT '{
  "current_topic": "знакомство",
  "mood": "нейтральный",
  "intimacy_level": 1,
  "known_facts": []
}'::jsonb;
"""

async def run_migration():
    conn = await asyncpg.connect(config.DATABASE_URL)
    await conn.execute(SQL)
    print(" Память БД готова: колонка memory_state добавлена!")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(run_migration())
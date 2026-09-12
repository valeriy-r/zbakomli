import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_BOT_TOKEN
from db import init_db, close_db, get_pool
from safety import hold_queue_worker
from bot_chapter1 import router as chapter1_router
from bot_chapter2 import router as chapter2_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")


async def main():
    await init_db()
    db_pool = get_pool()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # chapter1_router регистрируется первым для корректной обработки /start и меню
    dp.include_router(chapter1_router)
    dp.include_router(chapter2_router)

    queue_worker_task = asyncio.create_task(hold_queue_worker(db_pool, bot))
    logger.info("Bot and Safety Hold Worker successfully started.")

    try:
        await dp.start_polling(bot)
    finally:
        queue_worker_task.cancel()
        await bot.session.close()
        await close_db()
        logger.info("Сервис остановлен.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
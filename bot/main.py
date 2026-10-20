import asyncio
import logging
import shutil
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.config import load_config
from bot.downloader import YouTubeDownloader
from bot.handlers import setup_router


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Справка и лимиты"),
        BotCommand(command="cancel", description="Остановить загрузку плейлиста"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    if not shutil.which("ffmpeg"):
        logger.warning(
            "Внимание: исполняемый файл 'ffmpeg' не найден в PATH! "
            "Для конвертации в MP3 необходим установленный FFmpeg (в Docker он уже встроен)."
        )

    config = load_config()
    downloader = YouTubeDownloader(config)

    bot = Bot(token=config.bot_token)
    dp = Dispatcher()

    router = setup_router(config, downloader)
    dp.include_router(router)

    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info("Бот @%s (ID: %s) успешно запущен и готов к работе!", me.username, me.id)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

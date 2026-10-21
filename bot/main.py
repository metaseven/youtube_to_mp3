import asyncio
import contextlib
import logging
import shutil
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from aiogram.utils.backoff import BackoffConfig

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


async def periodic_storage_cleanup(downloader: YouTubeDownloader, interval_seconds: int = 600) -> None:
    """Фоновая задача: каждые 10 минут удаляет зависшие временные файлы старше 30 минут."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(downloader.cleanup_stale_files, 1800)
        except Exception as exc:
            logging.getLogger(__name__).warning("Ошибка в фоновой очистке хранилища: %s", exc)


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

    # Очищаем хранилище от возможных старых файлов после прошлых запусков/рестартов
    downloader.cleanup_all_downloads()

    # Увеличенный таймаут сессии (120 сек) для стабильной загрузки больших MP3-файлов
    session = AiohttpSession(timeout=120)
    bot = Bot(token=config.bot_token, session=session)
    dp = Dispatcher()

    router = setup_router(config, downloader)
    dp.include_router(router)

    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info("Бот @%s (ID: %s) успешно запущен и готов к работе!", me.username, me.id)

    # Настройка экспоненциальной задержки при сетевых сбоях и TelegramRetryAfter на GetUpdates
    backoff_config = BackoffConfig(
        min_delay=5.0,
        max_delay=60.0,
        factor=2.0,
        jitter=0.1,
    )

    cleanup_task = asyncio.create_task(periodic_storage_cleanup(downloader))
    try:
        await dp.start_polling(
            bot,
            polling_timeout=30,
            backoff_config=backoff_config,
        )
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        downloader.cleanup_all_downloads()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

import asyncio
import html
import logging
import time
from typing import Any, Callable, Coroutine
import uuid

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot.downloader import DownloadError, MediaSourceInfo, ProgressState, VideoItem, YouTubeDownloader
from bot.utils import (
    extract_youtube_urls,
    format_duration,
    has_playlist_param,
    is_playlist_url,
    render_progress_bar,
)

logger = logging.getLogger(__name__)


async def _retry_telegram_call(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
) -> Any:
    """Выполняет запрос к Telegram API с автоматическим повтором при таймаутах и лимитах."""
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            wait_sec = min(exc.retry_after + 1.0, 30.0)
            logger.warning("Telegram Flood Control: ожидание %.1f сек перед повтором...", wait_sec)
            await asyncio.sleep(wait_sec)
        except (TelegramNetworkError, TelegramServerError) as exc:
            if attempt + 1 >= max_retries:
                raise
            wait_sec = 2.0 * (attempt + 1)
            logger.warning(
                "Сетевая ошибка Telegram API (%s), повтор %d/%d через %.1f сек...",
                exc,
                attempt + 1,
                max_retries,
                wait_sec,
            )
            await asyncio.sleep(wait_sec)
    return None


def setup_router(config: Config, downloader: YouTubeDownloader) -> Router:
    router = Router()

    # Временное in-memory хранилище ссылок для инлайн-кнопок выбора (Видео vs Весь плейлист)
    pending_links: dict[str, str] = {}
    # Флаги отмены активных плейлистов по ID пользователя
    cancelled_users: set[int] = set()

    def is_user_allowed(user_id: int | None) -> bool:
        if not config.allowed_user_ids:
            return True
        return user_id is not None and user_id in config.allowed_user_ids

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if not is_user_allowed(message.from_user.id if message.from_user else None):
            await _retry_telegram_call(lambda: message.answer("⛔ У вас нет доступа к этому боту."))
            return

        text = (
            "👋 <b>Привет! Я бот для конвертации YouTube в MP3.</b>\n\n"
            "Отправь мне:\n"
            "• 🎬 <b>Ссылку на любое видео YouTube / YouTube Shorts / YouTube Music</b>\n"
            "• 📂 <b>Ссылку на целый плейлист YouTube</b>\n"
            "• 📋 <b>Несколько ссылок</b> в одном сообщении\n\n"
            "Я скачаю аудиодорожку в высоком качестве, сконвертирую её в <code>.mp3</code> "
            "и отправлю файл, <b>подписанный точным названием видео</b>.\n\n"
            "Команды:\n"
            "/help — справка и лимиты\n"
            "/cancel — остановить текущую загрузку плейлиста"
        )
        await _retry_telegram_call(lambda: message.answer(text, parse_mode=ParseMode.HTML))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        if not is_user_allowed(message.from_user.id if message.from_user else None):
            return

        max_mins = config.max_duration_seconds // 60
        text = (
            "ℹ️ <b>Справка по использованию бота</b>\n\n"
            "1. Скопируйте ссылку на ролик или плейлист YouTube и отправьте её в чат.\n"
            "2. Если ссылка ведёт на видео внутри плейлиста, бот предложит выбор: "
            "скачать только это видео или весь плейлист целиком.\n"
            "3. Каждый аудиофайл отправляется в формате <b>MP3</b> и подписан "
            "оригинальным названием видеоролика.\n\n"
            "⚙️ <b>Текущие настройки:</b>\n"
            f"• Битрейт MP3: <code>{config.audio_bitrate} kbps</code>\n"
            f"• Максимум треков из плейлиста за раз: <code>{config.max_playlist_items}</code>\n"
            f"• Максимальная длительность одного видео: <code>{max_mins} мин</code>\n"
            f"• Максимальный размер MP3 файла: <code>{config.max_file_size_mb} МБ</code>"
        )
        await _retry_telegram_call(lambda: message.answer(text, parse_mode=ParseMode.HTML))

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message) -> None:
        if not message.from_user or not is_user_allowed(message.from_user.id):
            return
        cancelled_users.add(message.from_user.id)
        await _retry_telegram_call(
            lambda: message.answer("🛑 Запрос на остановку принят. Текущая очередь скачивания прерывается.")
        )

    def _build_progress_text(
        item_title: str,
        state: ProgressState,
        playlist_header: str = "",
    ) -> str:
        safe_title = html.escape(item_title)
        header_block = f"{playlist_header}\n" if playlist_header else ""

        if state.stage == "downloading":
            bar = render_progress_bar(state.percent)
            details: list[str] = []
            if state.total_mb > 0:
                details.append(f"{state.downloaded_mb:.1f} / {state.total_mb:.1f} МБ")
            elif state.downloaded_mb > 0:
                details.append(f"{state.downloaded_mb:.1f} МБ")
            if state.speed_str:
                details.append(state.speed_str)
            if state.eta_str:
                details.append(f"осталось {state.eta_str}")
            details_line = f"\n📊 <code>{' • '.join(details)}</code>" if details else ""

            return (
                f"{header_block}"
                f"🎬 <b>{safe_title}</b>\n\n"
                f"📥 Скачивание аудиодорожки:\n"
                f"<code>{bar}</code>{details_line}"
            )

        bar = render_progress_bar(100.0)
        return (
            f"{header_block}"
            f"🎬 <b>{safe_title}</b>\n\n"
            f"✅ Скачано: <code>{bar}</code>\n"
            f"⚙️ <b>Конвертация видео в MP3 ({config.audio_bitrate} kbps)...</b>"
        )

    async def send_single_track(
        message: Message,
        bot: Bot,
        item: VideoItem,
        status_msg: Message | None = None,
        playlist_header: str = "",
        prefix_label: str = "",
    ) -> bool:
        """Скачивает одно видео в MP3, обновляя прогресс-бар, и отправляет пользователю."""
        track = None
        last_text = ""
        last_edit_ts = 0.0
        edit_lock = asyncio.Lock()

        async def on_progress(state: ProgressState) -> None:
            nonlocal last_text, last_edit_ts
            if status_msg is None:
                return
            if edit_lock.locked():
                return
            async with edit_lock:
                now = time.monotonic()
                if state.stage == "downloading" and (now - last_edit_ts) < 1.5:
                    return
                new_text = _build_progress_text(item.title, state, playlist_header=playlist_header)
                if new_text == last_text:
                    return
                try:
                    await status_msg.edit_text(new_text, parse_mode=ParseMode.HTML)
                    last_text = new_text
                    last_edit_ts = time.monotonic()
                except TelegramRetryAfter as exc:
                    last_edit_ts = time.monotonic() + exc.retry_after
                except (TelegramBadRequest, TelegramNetworkError, TelegramServerError):
                    pass
                except Exception:
                    pass

        try:
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VOICE)
            except Exception:
                pass

            track = await downloader.download_mp3(item.webpage_url, progress_callback=on_progress)

            if status_msg is not None:
                safe_title = html.escape(track.title)
                header_block = f"{playlist_header}\n" if playlist_header else ""
                upload_text = (
                    f"{header_block}"
                    f"🎵 <b>{safe_title}</b>\n\n"
                    f"📤 <b>Отправка MP3 файла в Telegram...</b>"
                )
                if upload_text != last_text:
                    try:
                        await status_msg.edit_text(upload_text, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass

            try:
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VOICE)
            except Exception:
                pass

            audio_file = FSInputFile(
                path=track.file_path,
                filename=track.display_filename,
            )
            thumb_file = (
                FSInputFile(path=track.thumbnail_path)
                if track.thumbnail_path and track.thumbnail_path.exists()
                else None
            )

            safe_title = html.escape(track.title)
            duration_str = format_duration(track.duration)
            caption = (
                f"{prefix_label}🎵 <b>{safe_title}</b>\n"
                f"⏱ <code>{duration_str}</code>"
            )

            await _retry_telegram_call(
                lambda: message.answer_audio(
                    audio=audio_file,
                    title=track.title,
                    performer=track.performer,
                    duration=track.duration,
                    thumbnail=thumb_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            )
            return True

        except DownloadError as exc:
            safe_title = html.escape(item.title)
            await _retry_telegram_call(
                lambda: message.answer(
                    f"⚠️ Не удалось обработать <b>{safe_title}</b>:\n<code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )
            )
            return False
        except Exception as exc:
            logger.exception("Непредвиденная ошибка при отправке трека %s", item.webpage_url)
            safe_title = html.escape(item.title)
            await _retry_telegram_call(
                lambda: message.answer(
                    f"❌ Ошибка при отправке <b>{safe_title}</b>: <code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )
            )
            return False
        finally:
            if track is not None:
                track.cleanup()

    async def process_url(
        message: Message,
        bot: Bot,
        url: str,
        *,
        allow_playlist: bool,
    ) -> None:
        user_id = message.from_user.id if message.from_user else 0
        cancelled_users.discard(user_id)

        status_msg = await _retry_telegram_call(
            lambda: message.answer("🔍 Получаю информацию о ссылке...")
        )
        if status_msg is None:
            return

        try:
            source_info: MediaSourceInfo = await downloader.extract_source_info(
                url,
                allow_playlist=allow_playlist,
            )
        except DownloadError as exc:
            try:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка:</b>\n<code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return
        except Exception as exc:
            logger.exception("Непредвиденная ошибка при анализе ссылки %s", url)
            try:
                await status_msg.edit_text(
                    f"❌ <b>Не удалось обработать ссылку:</b>\n<code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        # Обработка одиночного видео
        if not source_info.is_playlist or len(source_info.items) == 1:
            item = source_info.items[0]
            safe_title = html.escape(item.title)
            initial_bar = render_progress_bar(0.0)
            try:
                await status_msg.edit_text(
                    f"🎬 <b>{safe_title}</b>\n\n"
                    f"📥 Подготовка к скачиванию:\n"
                    f"<code>{initial_bar}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            await send_single_track(message, bot, item, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # Обработка плейлиста
        total_items = len(source_info.items)
        safe_playlist_title = html.escape(source_info.title)
        limit_note = ""
        if source_info.total_playlist_count > total_items:
            limit_note = (
                f"\n<i>(В плейлисте {source_info.total_playlist_count} видео, "
                f"будут скачаны первые {total_items} согласно лимиту)</i>"
            )

        try:
            await status_msg.edit_text(
                f"📂 <b>Плейлист:</b> {safe_playlist_title}\n"
                f"🎶 Треков к загрузке: <b>{total_items}</b>{limit_note}\n\n"
                "⏳ Начинаю поочерёдную конвертацию в MP3...\n"
                "<i>Для остановки отправьте /cancel</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        success_count = 0
        fail_count = 0

        for idx, item in enumerate(source_info.items, start=1):
            if user_id in cancelled_users:
                cancelled_users.discard(user_id)
                await _retry_telegram_call(
                    lambda: message.answer(
                        f"🛑 Загрузка плейлиста <b>{safe_playlist_title}</b> остановлена пользователем.\n"
                        f"✅ Успешно загружено: <b>{success_count}</b> из {total_items}.",
                        parse_mode=ParseMode.HTML,
                    )
                )
                return

            playlist_bar = render_progress_bar(((idx - 1) / total_items) * 100.0, width=10)
            playlist_header = (
                f"📂 <b>Плейлист:</b> {safe_playlist_title}\n"
                f"🗂 Прогресс плейлиста: <b>{idx}/{total_items}</b> <code>{playlist_bar}</code>\n"
                f"<i>Для остановки отправьте /cancel</i>"
            )

            safe_item_title = html.escape(item.title)
            initial_track_bar = render_progress_bar(0.0)
            try:
                await status_msg.edit_text(
                    f"{playlist_header}\n\n"
                    f"🎬 <b>{safe_item_title}</b>\n\n"
                    f"📥 Подготовка к скачиванию:\n"
                    f"<code>{initial_track_bar}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            prefix = f"<code>[{idx}/{total_items}]</code> "
            ok = await send_single_track(
                message,
                bot,
                item,
                status_msg=status_msg,
                playlist_header=playlist_header,
                prefix_label=prefix,
            )
            if ok:
                success_count += 1
            else:
                fail_count += 1

            # Небольшая пауза между треками во избежание флуд-лимитов Telegram и YouTube
            await asyncio.sleep(1.0)

        summary_text = (
            f"✅ <b>Плейлист «{safe_playlist_title}» обработан!</b>\n"
            f"🎵 Успешно отправлено MP3: <b>{success_count}</b>"
        )
        if fail_count > 0:
            summary_text += f"\n⚠️ Пропущено из-за ошибок/ограничений: <b>{fail_count}</b>"

        try:
            await status_msg.edit_text(summary_text, parse_mode=ParseMode.HTML)
        except Exception:
            await _retry_telegram_call(lambda: message.answer(summary_text, parse_mode=ParseMode.HTML))

    @router.callback_query(F.data.startswith("dl:"))
    async def handle_download_choice(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.data or not callback.message:
            return

        parts = callback.data.split(":", maxsplit=2)
        if len(parts) != 3:
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
            return

        mode, token = parts[1], parts[2]
        url = pending_links.pop(token, None)

        if not url:
            await callback.answer("Ссылка устарела. Отправьте её заново.", show_alert=True)
            try:
                await callback.message.delete()
            except Exception:
                pass
            return

        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

        allow_playlist = mode == "playlist"
        await process_url(callback.message, bot, url, allow_playlist=allow_playlist)

    @router.message(F.text)
    async def handle_text_message(message: Message, bot: Bot) -> None:
        if not is_user_allowed(message.from_user.id if message.from_user else None):
            await _retry_telegram_call(lambda: message.answer("⛔ У вас нет доступа к этому боту."))
            return

        urls = extract_youtube_urls(message.text or "")
        if not urls:
            await _retry_telegram_call(
                lambda: message.answer(
                    "🔗 Пожалуйста, отправьте корректную ссылку на видео или плейлист <b>YouTube</b>.",
                    parse_mode=ParseMode.HTML,
                )
            )
            return

        for url in urls:
            # Если ссылка содержит одновременно и видео (v=...), и плейлист (list=...),
            # спрашиваем пользователя, хочет ли он скачать только это видео или весь плейлист
            if has_playlist_param(url) and not is_playlist_url(url):
                token = uuid.uuid4().hex[:12]
                pending_links[token] = url
                # Ограничиваем размер кэша ожидающих ссылок
                if len(pending_links) > 500:
                    oldest_key = next(iter(pending_links))
                    pending_links.pop(oldest_key, None)

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎵 Только это видео",
                                callback_data=f"dl:single:{token}",
                            ),
                            InlineKeyboardButton(
                                text="📂 Весь плейлист",
                                callback_data=f"dl:playlist:{token}",
                            ),
                        ]
                    ]
                )
                await _retry_telegram_call(
                    lambda: message.answer(
                        "🤔 Эта ссылка ведёт на видео внутри плейлиста. Что именно скачать?",
                        reply_markup=keyboard,
                    )
                )
            else:
                await process_url(
                    message,
                    bot,
                    url,
                    allow_playlist=True,
                )

    return router

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import time
import uuid
from typing import Any

import yt_dlp

from bot.config import Config
from bot.utils import sanitize_filename

logger = logging.getLogger(__name__)


@dataclass
class ProgressState:
    """Текущее состояние прогресса скачивания или конвертации."""
    stage: str  # "downloading" | "converting"
    percent: float
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    speed_str: str = ""
    eta_str: str = ""


ProgressCallback = Callable[[ProgressState], Awaitable[None]]


@dataclass
class VideoItem:
    """Метаданные одного видеоролика (из одиночной ссылки или элемента плейлиста)."""
    video_id: str
    title: str
    webpage_url: str
    duration: int | None = None
    uploader: str | None = None


@dataclass
class MediaSourceInfo:
    """Результат предварительного анализа ссылки (одиночное видео или плейлист)."""
    is_playlist: bool
    title: str
    items: list[VideoItem]
    total_playlist_count: int = 1


@dataclass
class DownloadedTrack:
    """Результат скачивания и конвертации одного трека в MP3."""
    file_path: Path
    display_filename: str
    title: str
    performer: str | None
    duration: int | None
    thumbnail_path: Path | None
    temp_dir: Path

    def cleanup(self) -> None:
        """Удаляет временную папку со всеми файлами этого трека."""
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Не удалось удалить временную директорию %s: %s", self.temp_dir, exc)


class DownloadError(Exception):
    """Пользовательская ошибка скачивания или конвертации."""


class YouTubeDownloader:
    """Сервис для извлечения информации и скачивания MP3 с YouTube через yt-dlp."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_downloads)

    def cleanup_all_downloads(self) -> None:
        """Полная очистка папки загрузок при старте бота."""
        if not self.config.download_dir.exists():
            return
        removed = 0
        for item in self.config.download_dir.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed += 1
            except Exception as exc:
                logger.warning("Не удалось удалить старый временный файл %s: %s", item, exc)
        if removed > 0:
            logger.info("Очищено старых временных папок/файлов при запуске: %d", removed)

    def cleanup_stale_files(self, max_age_seconds: int = 1800) -> int:
        """Удаляет зависшие временные директории и файлы старше max_age_seconds (по умолчанию 30 минут)."""
        if not self.config.download_dir.exists():
            return 0
        now = time.time()
        removed = 0
        for item in self.config.download_dir.iterdir():
            try:
                mtime = item.stat().st_mtime
                if now - mtime > max_age_seconds:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning("Ошибка при проверке/удалении %s: %s", item, exc)
        if removed > 0:
            logger.info("Периодическая очистка: удалено %d устаревших временных папок/файлов.", removed)
        return removed

    def _base_ydl_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "cachedir": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"],
                }
            },
        }
        if self.config.cookies_file and Path(self.config.cookies_file).is_file():
            opts["cookiefile"] = self.config.cookies_file
        if self.config.proxy_url:
            opts["proxy"] = self.config.proxy_url
        return opts

    async def extract_source_info(
        self,
        url: str,
        *,
        allow_playlist: bool = True,
    ) -> MediaSourceInfo:
        """
        Получает список видео (для плейлиста или одиночной ссылки) без скачивания самих файлов.
        Использует extract_flat='in_playlist' для мгновенного получения элементов плейлиста.
        """
        return await asyncio.to_thread(
            self._extract_source_info_sync,
            url,
            allow_playlist=allow_playlist,
        )

    def _extract_source_info_sync(
        self,
        url: str,
        *,
        allow_playlist: bool = True,
    ) -> MediaSourceInfo:
        opts = self._base_ydl_opts()
        opts["extract_flat"] = "in_playlist"
        opts["noplaylist"] = not allow_playlist
        if allow_playlist:
            opts["playlistend"] = self.config.max_playlist_items

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            logger.error("Ошибка при получении метаданных для %s: %s", url, exc)
            raise DownloadError(f"Не удалось получить информацию по ссылке: {exc}") from exc

        if not info:
            raise DownloadError("Видео или плейлист не найдены либо недоступны.")

        # Проверяем, вернул ли yt-dlp плейлист
        if info.get("_type") == "playlist" or "entries" in info:
            raw_entries = list(info.get("entries") or [])
            items: list[VideoItem] = []

            for entry in raw_entries:
                if not entry:
                    continue
                vid_id = entry.get("id") or ""
                vid_title = entry.get("title") or "Без названия"
                # Пропускаем удалённые или приватные видео в плейлисте
                if vid_title in {"[Deleted video]", "[Private video]"}:
                    continue

                entry_url = (
                    entry.get("url")
                    or entry.get("webpage_url")
                    or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "")
                )
                if not entry_url:
                    continue
                if not entry_url.startswith("http"):
                    entry_url = f"https://www.youtube.com/watch?v={vid_id}"

                items.append(
                    VideoItem(
                        video_id=vid_id,
                        title=vid_title,
                        webpage_url=entry_url,
                        duration=entry.get("duration"),
                        uploader=entry.get("uploader") or entry.get("channel"),
                    )
                )

            if not items:
                raise DownloadError("В плейлисте нет доступных для скачивания видео.")

            playlist_title = info.get("title") or "Плейлист YouTube"
            total_count = info.get("playlist_count") or len(items)
            return MediaSourceInfo(
                is_playlist=True,
                title=playlist_title,
                items=items[: self.config.max_playlist_items],
                total_playlist_count=total_count,
            )

        # Одиночное видео
        video_id = info.get("id") or ""
        title = info.get("title") or "YouTube Audio"
        webpage_url = info.get("webpage_url") or url
        duration = info.get("duration")
        uploader = info.get("uploader") or info.get("channel")

        return MediaSourceInfo(
            is_playlist=False,
            title=title,
            items=[
                VideoItem(
                    video_id=video_id,
                    title=title,
                    webpage_url=webpage_url,
                    duration=duration,
                    uploader=uploader,
                )
            ],
            total_playlist_count=1,
        )

    async def download_mp3(
        self,
        video_url: str,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadedTrack:
        """
        Скачивает аудиодорожку видео, конвертирует в MP3 через FFmpeg и переименовывает
        файл строго по названию видеоролика.
        """
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            return await asyncio.to_thread(
                self._download_mp3_sync,
                video_url,
                loop,
                progress_callback,
            )

    def _download_mp3_sync(
        self,
        video_url: str,
        loop: asyncio.AbstractEventLoop | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadedTrack:
        task_dir = self.config.download_dir / uuid.uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=True)

        last_update_time = 0.0
        last_reported_stage = ""

        def _emit_progress(state: ProgressState, *, force: bool = False) -> None:
            nonlocal last_update_time, last_reported_stage
            if not progress_callback or not loop:
                return
            now = time.monotonic()
            if not force and state.stage == last_reported_stage and (now - last_update_time) < 2.0:
                return
            last_update_time = now
            last_reported_stage = state.stage
            try:
                asyncio.run_coroutine_threadsafe(progress_callback(state), loop)
            except Exception:
                pass

        def _progress_hook(d: dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total > 0:
                    percent = min(99.0, (downloaded / total) * 100.0)
                else:
                    percent = 0.0

                speed = d.get("speed")
                if speed and speed > 0:
                    if speed >= 1024 * 1024:
                        speed_str = f"{speed / (1024 * 1024):.1f} МБ/с"
                    else:
                        speed_str = f"{speed / 1024:.0f} КБ/с"
                else:
                    speed_str = ""

                eta = d.get("eta")
                if isinstance(eta, (int, float)) and eta >= 0:
                    mins, secs = divmod(int(eta), 60)
                    eta_str = f"{mins:02d}:{secs:02d}"
                else:
                    eta_str = ""

                _emit_progress(
                    ProgressState(
                        stage="downloading",
                        percent=percent,
                        downloaded_mb=downloaded / (1024 * 1024),
                        total_mb=total / (1024 * 1024) if total else 0.0,
                        speed_str=speed_str,
                        eta_str=eta_str,
                    )
                )
            elif status == "finished":
                total = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                total_mb = total / (1024 * 1024) if total else 0.0
                _emit_progress(
                    ProgressState(
                        stage="converting",
                        percent=100.0,
                        downloaded_mb=total_mb,
                        total_mb=total_mb,
                    ),
                    force=True,
                )

        def _postprocessor_hook(d: dict[str, Any]) -> None:
            status = d.get("status")
            if status == "started":
                _emit_progress(
                    ProgressState(
                        stage="converting",
                        percent=100.0,
                    ),
                    force=True,
                )

        try:
            # Шаг 1: Проверяем длительность до начала скачивания
            probe_opts = self._base_ydl_opts()
            probe_opts["noplaylist"] = True

            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)

            if not info:
                raise DownloadError("Не удалось получить данные о видео.")

            if info.get("is_live"):
                raise DownloadError("Прямые трансляции (live) не поддерживаются для конвертации в MP3.")

            duration = info.get("duration")
            if duration and duration > self.config.max_duration_seconds:
                max_mins = self.config.max_duration_seconds // 60
                actual_mins = int(duration) // 60
                raise DownloadError(
                    f"Видео слишком длинное ({actual_mins} мин). "
                    f"Максимально допустимая длительность: {max_mins} мин."
                )

            # Шаг 2: Скачиваем лучшую аудиодорожку и конвертируем в MP3 + метаданные
            outtmpl = str(task_dir / "%(id)s.%(ext)s")
            dl_opts = self._base_ydl_opts()
            dl_opts.update(
                {
                    "format": "bestaudio/best",
                    "noplaylist": True,
                    "outtmpl": outtmpl,
                    "writethumbnail": True,
                    "keepvideo": False,
                    "progress_hooks": [_progress_hook],
                    "postprocessor_hooks": [_postprocessor_hook],
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": self.config.audio_bitrate,
                        },
                        {
                            "key": "FFmpegThumbnailsConvertor",
                            "format": "jpg",
                        },
                        {
                            "key": "FFmpegMetadata",
                            "add_metadata": True,
                        },
                    ],
                }
            )

            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                result_info = ydl.extract_info(video_url, download=True)

            raw_title = (result_info or info).get("title") or "YouTube Audio"
            performer = (
                (result_info or info).get("artist")
                or (result_info or info).get("uploader")
                or (result_info or info).get("channel")
            )
            final_duration = (result_info or info).get("duration")

            # Находим сконвертированный .mp3 файл
            mp3_files = list(task_dir.glob("*.mp3"))
            if not mp3_files:
                raise DownloadError("Не удалось создать MP3-файл после конвертации.")

            raw_mp3_path = mp3_files[0]

            # Переименовываем файл по названию видео, чтобы в Telegram он пришёл подписанным по имени видео
            clean_title = sanitize_filename(raw_title)
            final_filename = f"{clean_title}.mp3"
            final_mp3_path = task_dir / final_filename

            if raw_mp3_path != final_mp3_path:
                raw_mp3_path.rename(final_mp3_path)

            # Проверяем размер файла под лимит стандартного Telegram Bot API (50 МБ)
            file_size_mb = final_mp3_path.stat().st_size / (1024 * 1024)
            if file_size_mb > self.config.max_file_size_mb:
                raise DownloadError(
                    f"Размер MP3 файла ({file_size_mb:.1f} МБ) превышает лимит Telegram Bot API "
                    f"({self.config.max_file_size_mb} МБ)."
                )

            # Ищем сконвертированную обложку .jpg, если она скачалась
            jpg_files = list(task_dir.glob("*.jpg"))
            thumbnail_path = jpg_files[0] if jpg_files else None

            # Удаляем все прочие промежуточные файлы (.webm, .m4a, .webp, .part), если они остались
            keep_files = {final_mp3_path}
            if thumbnail_path:
                keep_files.add(thumbnail_path)
            for f in task_dir.iterdir():
                if f not in keep_files and f.is_file():
                    f.unlink(missing_ok=True)

            return DownloadedTrack(
                file_path=final_mp3_path,
                display_filename=final_filename,
                title=raw_title,
                performer=performer,
                duration=int(final_duration) if final_duration else None,
                thumbnail_path=thumbnail_path,
                temp_dir=task_dir,
            )

        except DownloadError:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            logger.error("Ошибка скачивания %s: %s", video_url, exc)
            raise DownloadError(f"Ошибка при скачивании или конвертации видео: {exc}") from exc

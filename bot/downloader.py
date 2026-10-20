import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import uuid
from typing import Any

import yt_dlp

from bot.config import Config
from bot.utils import sanitize_filename

logger = logging.getLogger(__name__)


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

    def _base_ydl_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
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

    async def download_mp3(self, video_url: str) -> DownloadedTrack:
        """
        Скачивает аудиодорожку видео, конвертирует в MP3 через FFmpeg и переименовывает
        файл строго по названию видеоролика.
        """
        async with self._semaphore:
            return await asyncio.to_thread(self._download_mp3_sync, video_url)

    def _download_mp3_sync(self, video_url: str) -> DownloadedTrack:
        task_dir = self.config.download_dir / uuid.uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=True)

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

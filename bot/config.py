from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _parse_allowed_users(raw_value: str) -> set[int]:
    if not raw_value or not raw_value.strip():
        return set()
    user_ids: set[int] = set()
    for part in raw_value.split(","):
        cleaned = part.strip()
        if cleaned.isdigit() or (cleaned.startswith("-") and cleaned[1:].isdigit()):
            user_ids.add(int(cleaned))
    return user_ids


@dataclass(frozen=True)
class Config:
    bot_token: str
    audio_bitrate: str = "192"
    max_playlist_items: int = 50
    max_duration_seconds: int = 7200  # 2 часа
    max_file_size_mb: int = 49        # Лимит стандартного Telegram Bot API (50 МБ)
    max_concurrent_downloads: int = 2
    download_dir: Path = Path("/app/downloads")
    allowed_user_ids: set[int] = field(default_factory=set)
    cookies_file: str | None = None
    proxy_url: str | None = None


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError(
            "Переменная окружения BOT_TOKEN не задана! "
            "Укажите её в .env файле или в настройках Environment Variables в Coolify."
        )

    default_download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).resolve()
    default_download_dir.mkdir(parents=True, exist_ok=True)

    cookies_file = os.getenv("COOKIES_FILE", "").strip() or None
    proxy_url = os.getenv("PROXY_URL", "").strip() or None

    return Config(
        bot_token=bot_token,
        audio_bitrate=os.getenv("AUDIO_BITRATE", "192").strip(),
        max_playlist_items=int(os.getenv("MAX_PLAYLIST_ITEMS", "50")),
        max_duration_seconds=int(os.getenv("MAX_DURATION_SECONDS", "7200")),
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "49")),
        max_concurrent_downloads=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")),
        download_dir=default_download_dir,
        allowed_user_ids=_parse_allowed_users(os.getenv("ALLOWED_USER_IDS", "")),
        cookies_file=cookies_file,
        proxy_url=proxy_url,
    )

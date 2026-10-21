import re
from urllib.parse import parse_qs, urlparse

# Регулярное выражение для поиска ссылок в тексте сообщения
URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Поддерживаемые домены YouTube (включая YouTube Music и мобильные ссылки)
YOUTUBE_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

# Запрещённые символы в именах файлов для Windows/Linux/macOS/Telegram
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTIPLE_SPACES = re.compile(r"\s+")


def extract_youtube_urls(text: str) -> list[str]:
    """Извлекает все уникальные валидные ссылки на YouTube из текста сообщения."""
    if not text:
        return []

    found_urls = URL_REGEX.findall(text)
    valid_urls: list[str] = []
    seen: set[str] = set()

    for url in found_urls:
        # Убираем случайные знаки препинания в конце ссылки
        cleaned_url = url.rstrip(".,;!)>]}'\"")
        if is_youtube_url(cleaned_url) and cleaned_url not in seen:
            seen.add(cleaned_url)
            valid_urls.append(cleaned_url)

    return valid_urls


def is_youtube_url(url: str) -> bool:
    """Проверяет, относится ли ссылка к YouTube."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host in YOUTUBE_DOMAINS or host.endswith(".youtube.com")
    except Exception:
        return False


def is_playlist_url(url: str) -> bool:
    """Определяет, ведёт ли ссылка на плейлист YouTube."""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        # Чистая ссылка на плейлист (/playlist?list=...) или ссылка с параметром list=
        if parsed.path.startswith("/playlist") and "list" in query:
            return True
        if "list" in query and "v" not in query:
            return True
        return False
    except Exception:
        return False


def has_playlist_param(url: str) -> bool:
    """Проверяет, есть ли в ссылке на конкретное видео также параметр плейлиста (list=...)."""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        return "list" in query and ("v" in query or parsed.hostname in {"youtu.be", "www.youtu.be"})
    except Exception:
        return False


def sanitize_filename(title: str, max_length: int = 180) -> str:
    """
    Очищает название видео от недопустимых для файловой системы символов,
    сохраняя при этом читаемое название видео (кириллицу, латиницу, цифры, скобки, дефисы).
    """
    if not title:
        return "YouTube_Audio"

    # Заменяем слеши и двоеточия на дефис, а прочие спецсимволы удаляем
    cleaned = title.replace("/", " - ").replace("\\", " - ").replace(":", " -")
    cleaned = INVALID_FILENAME_CHARS.sub("", cleaned)
    cleaned = MULTIPLE_SPACES.sub(" ", cleaned).strip(" .-_")

    if not cleaned:
        return "YouTube_Audio"

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .-_")

    return cleaned


def format_duration(seconds: int | float | None) -> str:
    """Форматирует длительность из секунд в ЧЧ:ММ:СС или ММ:СС."""
    if not seconds or seconds <= 0:
        return "00:00"
    total_sec = int(seconds)
    hours, remainder = divmod(total_sec, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_progress_bar(percent: float, width: int = 12) -> str:
    """Формирует текстовый прогресс-бар вида [██████░░░░░░] 50%."""
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100.0) * width))
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {clamped:.0f}%"


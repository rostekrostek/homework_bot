import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Токен бота, полученный от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")


def _parse_teacher_ids(raw: str) -> list[int]:
    ids = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            pass
    return ids


# Список Telegram chat_id преподавателей — им всем будут приходить
# уведомления о новых сданных работах. Можно указать несколько через запятую:
# TEACHER_CHAT_IDS=111111111,222222222
# Старая переменная TEACHER_CHAT_ID (одно значение) тоже поддерживается
# для обратной совместимости и будет добавлена в список, если задана.
_teacher_ids_raw = os.getenv("TEACHER_CHAT_IDS", "")
TEACHER_CHAT_IDS = _parse_teacher_ids(_teacher_ids_raw)

_legacy_single_id = os.getenv("TEACHER_CHAT_ID", "").strip()
if _legacy_single_id:
    try:
        legacy_id = int(_legacy_single_id)
        if legacy_id and legacy_id not in TEACHER_CHAT_IDS:
            TEACHER_CHAT_IDS.append(legacy_id)
    except ValueError:
        pass

# Пароль для входа в веб-панель (просмотр работ и отзывы)
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "changeme")

# Секретный ключ для подписи сессионных cookie веб-панели.
SECRET_KEY = os.getenv("SECRET_KEY", "please-change-this-secret-key")

# На каком хосте/порту слушает веб-панель
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Публичный адрес панели — для ссылок в уведомлениях преподавателям.
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", f"http://YOUR_VDS_IP:{WEB_PORT}")

# Часовой пояс преподавателя и студентов. Дедлайны вводятся в панели и
# показываются в этом поясе, а в базе хранятся в UTC.
# Список названий: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "homework.db"

# Изображения (можно присылать и как «файл», без сжатия Telegram).
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# Разрешённые расширения файлов, которые студент может прислать как документ.
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".ppt", ".pptx", ".xls", ".xlsx",
} | IMAGE_EXTENSIONS

# Максимальный размер файла (ограничение Telegram Bot API — 20 МБ).
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024

# Максимум материалов (файлы, фото, голосовые) в одной сдаче.
MAX_SUBMISSION_FILES = 15

# Сколько минут после отправки студент может изменить работу
# (если её ещё не проверили).
EDIT_WINDOW_MINUTES = 30

# Пауза после последнего файла альбома, прежде чем бот ответит «Принято».
ALBUM_DEBOUNCE_SECONDS = 1.5

# Сколько работ показывать на одной странице истории в боте.
HISTORY_PAGE_SIZE = 8

# Путь к ffmpeg — для конвертации голосовых отзывов преподавателя в .ogg/OPUS.
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

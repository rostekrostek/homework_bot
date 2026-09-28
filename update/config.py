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
# Сгенерировать можно командой: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.getenv("SECRET_KEY", "please-change-this-secret-key")

# На каком хосте/порту слушает веб-панель
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Публичный адрес панели — используется в уведомлениях, которые бот
# присылает вам в Telegram, чтобы вставить туда кликабельную ссылку.
# Пока без домена — укажите http://ВАШ_IP:8080
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", f"http://YOUR_VDS_IP:{WEB_PORT}")

MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "homework.db"

# Разрешённые расширения файлов-документов, которые студент может прислать
# боту (помимо текста и голосовых). Задание могут прислать как файл Word,
# PDF, PowerPoint, Excel и т.д.
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt",
    ".ppt", ".pptx", ".xls", ".xlsx",
}

# Максимальный размер файла в байтах, который бот согласится принять
# (ограничение самого Telegram Bot API на скачивание файлов — 20 МБ).
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024

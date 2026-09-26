import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Токен бота, полученный от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Ваш личный Telegram chat_id — сюда бот будет присылать уведомления
# о новых сданных работах. Как узнать свой chat_id — см. README.md.
TEACHER_CHAT_ID = int(os.getenv("TEACHER_CHAT_ID", "0"))

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

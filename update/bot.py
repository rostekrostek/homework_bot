import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from config import (
    ALLOWED_DOCUMENT_EXTENSIONS,
    BOT_TOKEN,
    MAX_DOCUMENT_SIZE,
    MEDIA_DIR,
    PANEL_BASE_URL,
    TEACHER_CHAT_IDS,
)
from database import Student, Submission, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class Registration(StatesGroup):
    waiting_name = State()
    waiting_group = State()


async def get_student(telegram_id: int):
    session = SessionLocal()
    try:
        return session.query(Student).filter_by(telegram_id=telegram_id).first()
    finally:
        session.close()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)

    if student:
        await message.answer(
            f"С возвращением, {student.full_name}!\n\n"
            "Отправьте текст, голосовое сообщение или файл (Word, PDF и т.д.) "
            "с выполненным заданием и коротко подпишите, что это за задание."
        )
        return

    await message.answer(
        "Привет! Это бот для сдачи домашних заданий по испанскому языку.\n\n"
        "Как вас зовут? Напишите, пожалуйста, имя и фамилию (например: Анна Иванова)."
    )
    await state.set_state(Registration.waiting_name)


@dp.message(Registration.waiting_name)
async def process_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, напишите имя текстом.")
        return
    await state.update_data(full_name=name)
    await message.answer("Спасибо! Теперь укажите вашу учебную группу (например: ИЯ-21).")
    await state.set_state(Registration.waiting_group)


@dp.message(Registration.waiting_group)
async def process_group(message: Message, state: FSMContext):
    group_name = (message.text or "").strip()
    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")

    session = SessionLocal()
    try:
        student = Student(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=full_name,
            group_name=group_name,
        )
        session.add(student)
        session.commit()
    finally:
        session.close()

    await state.clear()
    await message.answer(
        f"Готово, {full_name} ({group_name})!\n\n"
        "Теперь вы можете присылать сюда домашние задания:\n"
        "— текстом\n"
        "— голосовым сообщением\n"
        "— файлом (Word, PDF, PowerPoint, Excel и т.д.)\n\n"
        "Пожалуйста, коротко указывайте, какое это задание — так преподавателю "
        "будет проще ориентироваться."
    )


async def notify_teachers(submission_id: int, student: Student, kind: str, preview: str | None):
    if not TEACHER_CHAT_IDS:
        log.warning("TEACHER_CHAT_IDS не задан — уведомление не отправлено")
        return

    link = f"{PANEL_BASE_URL}/submission/{submission_id}"
    kind_labels = {"voice": "голосовое сообщение", "document": "файл"}
    kind_label = kind_labels.get(kind, "текст")
    text = f"📥 Новая работа от {student.full_name} ({student.group_name or '—'})\nТип: {kind_label}\n"
    if preview:
        text += f"Описание: {preview}\n"
    text += f"\nОткрыть и оставить отзыв: {link}"

    for chat_id in TEACHER_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не удалось отправить уведомление преподавателю %s", chat_id)


def _pending_caption_target(student_id: int):
    """Находит последнее voice/document-сообщение студента без подписи."""
    session = SessionLocal()
    try:
        return (
            session.query(Submission)
            .filter(
                Submission.student_id == student_id,
                Submission.kind.in_(["voice", "document"]),
                Submission.caption.is_(None),
            )
            .order_by(Submission.created_at.desc())
            .first()
        )
    finally:
        session.close()


@dp.message(F.voice)
async def handle_voice(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    file = await bot.get_file(message.voice.file_id)
    filename = f"voice_{student.id}_{int(datetime.utcnow().timestamp())}.ogg"
    dest_path = MEDIA_DIR / filename
    await bot.download_file(file.file_path, destination=dest_path)

    caption = (message.caption or "").strip() or None

    session = SessionLocal()
    try:
        submission = Submission(
            student_id=student.id,
            kind="voice",
            file_path=filename,
            caption=caption,
            status="new",
        )
        session.add(submission)
        session.commit()
        session.refresh(submission)
        submission_id = submission.id
    finally:
        session.close()

    if caption:
        await message.answer("Голосовое сообщение получено, передал преподавателю. Спасибо!")
    else:
        await message.answer(
            "Голосовое сообщение получено! Напишите, пожалуйста, следующим сообщением, "
            "какое это задание (например: «Задание 3, диалог о путешествиях»)."
        )

    await notify_teachers(submission_id, student, "voice", caption)


@dp.message(F.document)
async def handle_document(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    doc = message.document
    original_name = doc.file_name or "file"
    ext = Path(original_name).suffix.lower()

    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
        await message.answer(
            f"Такой тип файла пока не поддерживается ({ext or 'без расширения'}).\n"
            f"Поддерживаются: {allowed}"
        )
        return

    if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE:
        await message.answer(
            "Файл слишком большой (максимум 20 МБ). Пришлите файл поменьше."
        )
        return

    file = await bot.get_file(doc.file_id)
    filename = f"doc_{student.id}_{int(datetime.utcnow().timestamp())}{ext}"
    dest_path = MEDIA_DIR / filename
    await bot.download_file(file.file_path, destination=dest_path)

    caption = (message.caption or "").strip() or None

    session = SessionLocal()
    try:
        submission = Submission(
            student_id=student.id,
            kind="document",
            file_path=filename,
            original_filename=original_name,
            caption=caption,
            status="new",
        )
        session.add(submission)
        session.commit()
        session.refresh(submission)
        submission_id = submission.id
    finally:
        session.close()

    if caption:
        await message.answer("Файл получен, передал преподавателю. Спасибо!")
    else:
        await message.answer(
            "Файл получен! Напишите, пожалуйста, следующим сообщением, какое это "
            "задание (например: «Задание 3, диалог о путешествиях»)."
        )

    await notify_teachers(submission_id, student, "document", caption or original_name)


@dp.message(F.text)
async def handle_text(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    text = message.text.strip()

    # Если недавнее голосовое сообщение или файл остались без описания —
    # считаем этот текст их подписью, а не новым заданием.
    pending = _pending_caption_target(student.id)
    if pending:
        session = SessionLocal()
        try:
            session.query(Submission).filter_by(id=pending.id).update({"caption": text})
            session.commit()
        finally:
            session.close()
        await message.answer("Спасибо, описание добавлено!")
        return

    session = SessionLocal()
    try:
        submission = Submission(
            student_id=student.id,
            kind="text",
            text_content=text,
            status="new",
        )
        session.add(submission)
        session.commit()
        session.refresh(submission)
        submission_id = submission.id
    finally:
        session.close()

    await message.answer("Текстовое задание получено, передал преподавателю. Спасибо!")

    preview = text if len(text) <= 100 else text[:97] + "..."
    await notify_teachers(submission_id, student, "text", preview)


@dp.message()
async def handle_other(message: Message):
    await message.answer(
        "Пока что я умею принимать текст, голосовые сообщения и файлы "
        "(Word, PDF, PowerPoint, Excel и т.д.). Отправьте задание одним из этих способов."
    )


async def main():
    init_db()
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

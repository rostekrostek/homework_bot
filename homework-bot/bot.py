import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from config import BOT_TOKEN, MEDIA_DIR, PANEL_BASE_URL, TEACHER_CHAT_ID
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
            "Отправьте текст или голосовое сообщение с выполненным заданием "
            "и коротко подпишите, что это за задание."
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
        "— голосовым сообщением\n\n"
        "Пожалуйста, коротко указывайте, какое это задание — так преподавателю "
        "будет проще ориентироваться."
    )


async def notify_teacher(submission_id: int, student: Student, kind: str, preview: str | None):
    if not TEACHER_CHAT_ID:
        log.warning("TEACHER_CHAT_ID не задан — уведомление не отправлено")
        return

    link = f"{PANEL_BASE_URL}/submission/{submission_id}"
    kind_label = "голосовое сообщение" if kind == "voice" else "текст"
    text = f"📥 Новая работа от {student.full_name} ({student.group_name or '—'})\nТип: {kind_label}\n"
    if preview:
        text += f"Описание: {preview}\n"
    text += f"\nОткрыть и оставить отзыв: {link}"

    try:
        await bot.send_message(TEACHER_CHAT_ID, text)
    except Exception:
        log.exception("Не удалось отправить уведомление преподавателю")


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

    await notify_teacher(submission_id, student, "voice", caption)


@dp.message(F.text)
async def handle_text(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    text = message.text.strip()

    # Если недавнее голосовое сообщение осталось без описания —
    # считаем этот текст его подписью, а не новым заданием.
    session = SessionLocal()
    try:
        pending_voice = (
            session.query(Submission)
            .filter_by(student_id=student.id, kind="voice", caption=None)
            .order_by(Submission.created_at.desc())
            .first()
        )
        if pending_voice:
            pending_voice.caption = text
            session.commit()
            await message.answer("Спасибо, описание добавлено к голосовому сообщению!")
            return
    finally:
        session.close()

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
    await notify_teacher(submission_id, student, "text", preview)


@dp.message()
async def handle_other(message: Message):
    await message.answer(
        "Пока что я умею принимать только текстовые и голосовые сообщения. "
        "Отправьте задание одним из этих способов."
    )


async def main():
    init_db()
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

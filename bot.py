import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import (
    ALBUM_DEBOUNCE_SECONDS,
    ALLOWED_DOCUMENT_EXTENSIONS,
    BOT_TOKEN,
    HISTORY_PAGE_SIZE,
    MAX_DOCUMENT_SIZE,
    MEDIA_DIR,
    PANEL_BASE_URL,
    TEACHER_CHAT_IDS,
)
from database import Group, Student, Submission, SubmissionFile, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

HISTORY_BUTTON_TEXT = "📜 Моя история"
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=HISTORY_BUTTON_TEXT)]],
    resize_keyboard=True,
)

# Буфер для сборки альбомов (несколько файлов, присланных студентом одним
# действием — Telegram доставляет их отдельными сообщениями с общим
# media_group_id). Ключ — media_group_id.
_pending_albums: dict[str, list[Message]] = {}
_album_timers: dict[str, asyncio.Task] = {}


class Registration(StatesGroup):
    waiting_name = State()
    waiting_group = State()
    waiting_group_manual = State()


async def get_student(telegram_id: int):
    session = SessionLocal()
    try:
        return session.query(Student).filter_by(telegram_id=telegram_id).first()
    finally:
        session.close()


def get_groups() -> list[Group]:
    session = SessionLocal()
    try:
        return session.query(Group).order_by(Group.name).all()
    finally:
        session.close()


def _group_picker_keyboard(groups: list[Group]) -> InlineKeyboardMarkup:
    rows = []
    row: list[InlineKeyboardButton] = []
    for g in groups:
        row.append(InlineKeyboardButton(text=g.name, callback_data=f"reg_group:{g.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Не нашёл(-ла) группу — ввести вручную", callback_data="reg_group:manual")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _finish_registration(telegram_id: int, username: str | None, full_name: str, group_id: int | None, group_name: str | None):
    session = SessionLocal()
    try:
        student = Student(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            group_id=group_id,
            group_name=group_name,
        )
        session.add(student)
        session.commit()
    finally:
        session.close()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)

    if student:
        await message.answer(
            f"С возвращением, {student.full_name}!\n\n"
            "Отправьте текст, голосовое сообщение или файл(ы) (Word, PDF и т.д.) "
            "с выполненным заданием и коротко подпишите, что это за задание.",
            reply_markup=MAIN_MENU,
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

    groups = get_groups()
    if groups:
        await message.answer(
            "Спасибо! Теперь выберите вашу учебную группу:",
            reply_markup=_group_picker_keyboard(groups),
        )
        await state.set_state(Registration.waiting_group)
    else:
        await message.answer(
            "Спасибо! Теперь укажите вашу учебную группу текстом (например: ИЯ-21)."
        )
        await state.set_state(Registration.waiting_group_manual)


@dp.callback_query(Registration.waiting_group, F.data.startswith("reg_group:"))
async def process_group_button(callback: CallbackQuery, state: FSMContext):
    payload = callback.data.split(":", 1)[1]
    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")

    if payload == "manual":
        await callback.message.edit_text("Хорошо, напишите название своей группы текстом (например: ИЯ-21).")
        await state.set_state(Registration.waiting_group_manual)
        await callback.answer()
        return

    session = SessionLocal()
    try:
        group = session.query(Group).filter_by(id=int(payload)).first()
        group_name = group.name if group else None
        group_id = group.id if group else None
    finally:
        session.close()

    await _finish_registration(
        callback.from_user.id, callback.from_user.username, full_name, group_id, group_name
    )
    await state.clear()

    await callback.message.edit_text(f"Готово, {full_name} ({group_name})!")
    await callback.message.answer(
        "Теперь вы можете присылать сюда домашние задания:\n"
        "— текстом\n"
        "— голосовым сообщением\n"
        "— файлом или несколькими файлами сразу (Word, PDF, PowerPoint, Excel и т.д.)\n\n"
        "Пожалуйста, коротко указывайте, какое это задание — так преподавателю "
        "будет проще ориентироваться.",
        reply_markup=MAIN_MENU,
    )
    await callback.answer()


@dp.message(Registration.waiting_group_manual)
async def process_group_manual(message: Message, state: FSMContext):
    group_name = (message.text or "").strip()
    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")

    await _finish_registration(message.from_user.id, message.from_user.username, full_name, None, group_name)
    await state.clear()

    await message.answer(
        f"Готово, {full_name} ({group_name})!\n\n"
        "Теперь вы можете присылать сюда домашние задания:\n"
        "— текстом\n"
        "— голосовым сообщением\n"
        "— файлом или несколькими файлами сразу (Word, PDF, PowerPoint, Excel и т.д.)\n\n"
        "Пожалуйста, коротко указывайте, какое это задание — так преподавателю "
        "будет проще ориентироваться.",
        reply_markup=MAIN_MENU,
    )


async def notify_teachers(submission_id: int, student: Student, kind: str, preview: str | None, file_count: int = 1):
    if not TEACHER_CHAT_IDS:
        log.warning("TEACHER_CHAT_IDS не задан — уведомление не отправлено")
        return

    link = f"{PANEL_BASE_URL}/submission/{submission_id}"
    if kind == "voice":
        kind_label = "голосовое сообщение"
    elif kind == "document":
        kind_label = f"файл ({file_count} шт.)" if file_count > 1 else "файл"
    else:
        kind_label = "текст"
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


def _validate_document(doc) -> str | None:
    """Возвращает текст ошибки, если файл не подходит, иначе None."""
    original_name = doc.file_name or "file"
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
        return f"«{original_name}» — неподдерживаемый тип файла ({ext or 'без расширения'}). Поддерживаются: {allowed}"
    if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE:
        return f"«{original_name}» слишком большой (максимум 20 МБ)."
    return None


async def _save_documents(student: Student, chat_id: int, messages: list[Message]):
    """Скачивает и сохраняет один или несколько файлов как одно задание."""
    valid_messages = []
    errors = []
    for m in messages:
        err = _validate_document(m.document)
        if err:
            errors.append(err)
        else:
            valid_messages.append(m)

    if errors:
        await bot.send_message(chat_id, "\n".join(errors))

    if not valid_messages:
        return

    saved_files = []  # (dest_filename, original_name)
    for m in valid_messages:
        doc = m.document
        original_name = doc.file_name or "file"
        ext = Path(original_name).suffix.lower()
        file = await bot.get_file(doc.file_id)
        filename = f"doc_{student.id}_{int(datetime.utcnow().timestamp() * 1000)}_{len(saved_files)}{ext}"
        dest_path = MEDIA_DIR / filename
        await bot.download_file(file.file_path, destination=dest_path)
        saved_files.append((filename, original_name))

    # Подпись — берём первую непустую подпись среди сообщений альбома.
    caption = None
    for m in valid_messages:
        if m.caption and m.caption.strip():
            caption = m.caption.strip()
            break

    session = SessionLocal()
    try:
        submission = Submission(
            student_id=student.id,
            kind="document",
            caption=caption,
            status="new",
        )
        session.add(submission)
        session.flush()
        for idx, (filename, original_name) in enumerate(saved_files):
            session.add(
                SubmissionFile(
                    submission_id=submission.id,
                    file_path=filename,
                    original_filename=original_name,
                    order_index=idx,
                )
            )
        session.commit()
        session.refresh(submission)
        submission_id = submission.id
    finally:
        session.close()

    count = len(saved_files)
    file_word = "файл" if count == 1 else "файла" if count < 5 else "файлов"
    if caption:
        await bot.send_message(chat_id, f"Получил {count} {file_word}, передал преподавателю. Спасибо!")
    else:
        await bot.send_message(
            chat_id,
            f"Получил {count} {file_word}! Напишите, пожалуйста, следующим сообщением, какое это "
            "задание (например: «Задание 3, диалог о путешествиях»).",
        )

    await notify_teachers(submission_id, student, "document", caption, file_count=count)


async def _process_album(media_group_id: str):
    await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    messages = _pending_albums.pop(media_group_id, [])
    _album_timers.pop(media_group_id, None)
    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    first = messages[0]
    student = await get_student(first.from_user.id)
    if not student:
        await bot.send_message(first.chat.id, "Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    await _save_documents(student, first.chat.id, messages)


@dp.message(F.document)
async def handle_document(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    if message.media_group_id:
        group_id = message.media_group_id
        _pending_albums.setdefault(group_id, []).append(message)
        old_task = _album_timers.get(group_id)
        if old_task:
            old_task.cancel()
        _album_timers[group_id] = asyncio.create_task(_process_album(group_id))
        return

    await _save_documents(student, message.chat.id, [message])


@dp.message(F.text == HISTORY_BUTTON_TEXT)
@dp.message(Command("history"))
async def cmd_history(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return

    text, keyboard = _build_history_page(student.id, offset=0)
    await message.answer(text, reply_markup=keyboard)


def _build_history_page(student_id: int, offset: int) -> tuple[str, InlineKeyboardMarkup | None]:
    session = SessionLocal()
    try:
        total = session.query(Submission).filter_by(student_id=student_id).count()
        if total == 0:
            return "Пока нет сданных работ.", None

        items = (
            session.query(Submission)
            .filter_by(student_id=student_id)
            .order_by(Submission.created_at.desc())
            .offset(offset)
            .limit(HISTORY_PAGE_SIZE)
            .all()
        )

        kind_icons = {"voice": "🎤", "document": "📎", "text": "✍️"}
        rows = []
        for s in items:
            icon = kind_icons.get(s.kind, "•")
            status_icon = "✅" if s.status == "reviewed" else "⏳"
            label = f"{status_icon} {icon} {s.created_at.strftime('%d.%m.%Y')}"
            if s.caption:
                short_caption = s.caption if len(s.caption) <= 28 else s.caption[:25] + "..."
                label += f" — {short_caption}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"hist_show:{s.id}:{offset}")])

        nav_row = []
        if offset + HISTORY_PAGE_SIZE < total:
            nav_row.append(InlineKeyboardButton(text="◀️ Раньше", callback_data=f"hist_page:{offset + HISTORY_PAGE_SIZE}"))
        if offset > 0:
            nav_row.append(InlineKeyboardButton(text="Позже ▶️", callback_data=f"hist_page:{max(offset - HISTORY_PAGE_SIZE, 0)}"))
        if nav_row:
            rows.append(nav_row)

        shown_from = offset + 1
        shown_to = offset + len(items)
        text = f"Ваши работы ({shown_from}–{shown_to} из {total}). Нажмите на работу, чтобы посмотреть отзыв:"
        return text, InlineKeyboardMarkup(inline_keyboard=rows)
    finally:
        session.close()


@dp.callback_query(F.data.startswith("hist_page:"))
async def cb_history_page(callback: CallbackQuery):
    offset = int(callback.data.split(":", 1)[1])
    student = await get_student(callback.from_user.id)
    if not student:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return
    text, keyboard = _build_history_page(student.id, offset)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("hist_show:"))
async def cb_history_show(callback: CallbackQuery):
    _, sub_id_raw, offset_raw = callback.data.split(":")
    sub_id = int(sub_id_raw)
    offset = int(offset_raw)

    student = await get_student(callback.from_user.id)
    if not student:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=sub_id, student_id=student.id).first()
        if not s:
            await callback.answer("Не найдено.", show_alert=True)
            return
        data = {
            "kind": s.kind,
            "caption": s.caption,
            "text_content": s.text_content,
            "created_at": s.created_at,
            "status": s.status,
            "feedback_text": s.feedback_text,
            "feedback_voice_path": s.feedback_voice_path,
        }
    finally:
        session.close()

    kind_labels = {"voice": "голосовое сообщение", "document": "файл(ы)", "text": "текст"}
    lines = [f"📅 {data['created_at'].strftime('%d.%m.%Y %H:%M')}"]
    lines.append(f"Тип: {kind_labels.get(data['kind'], data['kind'])}")
    if data["caption"]:
        lines.append(f"Задание: {data['caption']}")
    if data["kind"] == "text" and data["text_content"]:
        preview = data["text_content"] if len(data["text_content"]) <= 300 else data["text_content"][:297] + "..."
        lines.append(f"\nВаш ответ:\n{preview}")

    if data["status"] == "reviewed":
        lines.append("\n✅ Отзыв получен:")
        if data["feedback_text"]:
            lines.append(data["feedback_text"])
        if data["feedback_voice_path"]:
            lines.append("(и голосовой отзыв — отправляю следующим сообщением)")
    else:
        lines.append("\n⏳ Пока без отзыва.")

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"hist_page:{offset}")]]
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=back_keyboard)

    if data["status"] == "reviewed" and data["feedback_voice_path"]:
        voice_path = MEDIA_DIR / data["feedback_voice_path"]
        if voice_path.exists():
            from aiogram.types import FSInputFile

            await bot.send_voice(callback.message.chat.id, FSInputFile(voice_path))

    await callback.answer()


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
        "(Word, PDF, PowerPoint, Excel и т.д. — можно несколько сразу). "
        "Отправьте задание одним из этих способов."
    )


async def main():
    init_db()
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import logging
import math
import time
import uuid
from datetime import timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.orm import joinedload

from config import (
    ALBUM_DEBOUNCE_SECONDS,
    ALLOWED_DOCUMENT_EXTENSIONS,
    BOT_TOKEN,
    HISTORY_PAGE_SIZE,
    MAX_DOCUMENT_SIZE,
    MAX_SUBMISSION_FILES,
    MEDIA_DIR,
    PANEL_BASE_URL,
    TEACHER_CHAT_IDS,
    TIMEZONE,
)
from database import Group, Student, Submission, SubmissionFile, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(TIMEZONE)
except Exception:  # нет tzdata — показываем время как есть (UTC)
    LOCAL_TZ = None

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".aac"}

BTN_SUBMIT = "📝 Сдать задание"
BTN_HISTORY = "📜 Моя история"
BTN_SEND = "✅ Отправить"
BTN_CANCEL = "❌ Отмена"


# ───────────────────────── клавиатуры и вспомогательное ─────────────────────────


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SUBMIT), KeyboardButton(text=BTN_HISTORY)]],
        resize_keyboard=True,
    )


def draft_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SEND), KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def describe_content(files_count: int, texts_count: int) -> str:
    parts = []
    if files_count:
        parts.append(f"{files_count} {plural(files_count, 'файл', 'файла', 'файлов')}")
    if texts_count:
        parts.append("текст")
    return " + ".join(parts) if parts else "пусто"


def fmt_time(dt) -> str:
    if LOCAL_TZ is not None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    return dt.strftime("%d.%m %H:%M")


async def get_student(telegram_id: int):
    session = SessionLocal()
    try:
        return (
            session.query(Student)
            .options(joinedload(Student.group))
            .filter_by(telegram_id=telegram_id)
            .first()
        )
    finally:
        session.close()


async def require_student(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
    return student


# ───────────────────────────── регистрация ─────────────────────────────


class Registration(StatesGroup):
    waiting_name = State()
    waiting_group = State()


def _list_groups() -> list[tuple[int, str]]:
    session = SessionLocal()
    try:
        return [(g.id, g.name) for g in session.query(Group).order_by(Group.name).all()]
    finally:
        session.close()


def _create_student(tg_user, full_name: str, group_id=None, group_name=None) -> Student | None:
    session = SessionLocal()
    try:
        if session.query(Student).filter_by(telegram_id=tg_user.id).first():
            return None
        student = Student(
            telegram_id=tg_user.id,
            username=tg_user.username,
            full_name=full_name,
            group_id=group_id,
            group_name=group_name,
        )
        session.add(student)
        session.commit()
        return student
    finally:
        session.close()


WELCOME_AFTER_REGISTRATION = (
    "Теперь вы можете сдавать домашние задания.\n\n"
    "Нажмите «📝 Сдать задание», загрузите всё нужное (текст, файлы, фото, голосовые) "
    "и подтвердите отправку кнопкой «✅ Отправить»."
)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    student = await get_student(message.from_user.id)

    if student:
        await message.answer(
            f"С возвращением, {student.full_name}!\n\n"
            "Нажмите «📝 Сдать задание», чтобы отправить работу.",
            reply_markup=main_menu_kb(),
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

    groups = _list_groups()
    if groups:
        builder = InlineKeyboardBuilder()
        for gid, gname in groups:
            builder.button(text=gname, callback_data=f"grp:{gid}")
        builder.adjust(2)
        await message.answer("Спасибо! Выберите вашу группу:", reply_markup=builder.as_markup())
    else:
        await message.answer("Спасибо! Теперь укажите вашу учебную группу (например: ИЯ-21).")
    await state.set_state(Registration.waiting_group)


@dp.callback_query(Registration.waiting_group, F.data.startswith("grp:"))
async def process_group_button(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split(":", 1)[1])
    group = next(((gid, n) for gid, n in _list_groups() if gid == group_id), None)
    if not group:
        await callback.answer("Эта группа больше не существует", show_alert=True)
        return

    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")
    student = _create_student(callback.from_user, full_name, group_id=group_id)
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(f"Группа: {group[1]}")
    except Exception:
        pass
    if student is None:
        return
    await callback.message.answer(
        f"Готово, {full_name} ({group[1]})!\n\n{WELCOME_AFTER_REGISTRATION}",
        reply_markup=main_menu_kb(),
    )


@dp.message(Registration.waiting_group)
async def process_group_text(message: Message, state: FSMContext):
    if _list_groups():
        await message.answer("Пожалуйста, выберите группу кнопкой в сообщении выше.")
        return
    group_name = (message.text or "").strip()
    if not group_name:
        await message.answer("Пожалуйста, напишите группу текстом.")
        return
    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")
    _create_student(message.from_user, full_name, group_name=group_name)
    await state.clear()
    await message.answer(
        f"Готово, {full_name} ({group_name})!\n\n{WELCOME_AFTER_REGISTRATION}",
        reply_markup=main_menu_kb(),
    )


# ───────────────────────────── сдача задания ─────────────────────────────


class Draft:
    """Черновик задания: всё, что студент прислал до нажатия «Отправить»."""

    def __init__(self):
        self.files: list[dict] = []
        self.texts: list[str] = []
        self.notes: list[str] = []
        self.summary_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def is_empty(self) -> bool:
        return not self.files and not self.texts

    def cancel_summary(self):
        if self.summary_task and not self.summary_task.done():
            self.summary_task.cancel()

    def discard_files(self):
        for f in self.files:
            try:
                (MEDIA_DIR / f["path"]).unlink(missing_ok=True)
            except OSError:
                pass


drafts: dict[int, Draft] = {}


def get_draft(user_id: int) -> Draft:
    draft = drafts.get(user_id)
    if draft is None:
        draft = Draft()
        drafts[user_id] = draft
    return draft


def _add_note(draft: Draft, note: str):
    if note not in draft.notes:
        draft.notes.append(note)


async def _send_summary(message: Message, draft: Draft):
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    notes, draft.notes = draft.notes, []
    lines = list(notes)
    if not draft.is_empty():
        lines.append(
            f"📎 Добавлено. Сейчас в задании: {describe_content(len(draft.files), len(draft.texts))}."
        )
    lines.append("")
    lines.append("Можно прислать ещё или нажать «✅ Отправить», когда всё готово.")
    try:
        await message.answer("\n".join(lines), reply_markup=draft_kb())
    except Exception:
        log.exception("Не удалось отправить сводку по черновику")


def schedule_summary(message: Message, draft: Draft):
    """Откладываем ответ на пару секунд, чтобы альбом из N файлов
    получил один ответ, а не N."""
    draft.cancel_summary()
    draft.summary_task = asyncio.create_task(_send_summary(message, draft))


def _add_caption(draft: Draft, message: Message):
    caption = (message.caption or "").strip()
    if caption:
        draft.texts.append(caption)


async def _add_attachment(
    draft: Draft,
    student: Student,
    file_id: str,
    size: int | None,
    original_name: str,
    ext: str,
    kind: str,
    prefix: str,
):
    if len(draft.files) >= MAX_SUBMISSION_FILES:
        _add_note(draft, f"⚠️ В одном задании не больше {MAX_SUBMISSION_FILES} файлов — лишние не приняты.")
        return
    if size and size > MAX_DOCUMENT_SIZE:
        _add_note(draft, f"⚠️ «{original_name}» больше 20 МБ — не принят.")
        return

    filename = f"{prefix}_{student.id}_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=MEDIA_DIR / filename)
    except Exception:
        log.exception("Не удалось скачать файл %s", original_name)
        _add_note(draft, f"⚠️ Не удалось получить «{original_name}» — пришлите его ещё раз.")
        return

    draft.files.append({"path": filename, "original": original_name, "kind": kind})


@dp.message(F.text == BTN_SUBMIT)
@dp.message(Command("submit"))
async def start_submission(message: Message):
    student = await require_student(message)
    if not student:
        return

    draft = drafts.get(message.from_user.id)
    if draft and not draft.is_empty():
        await message.answer(
            f"У вас уже есть начатое задание ({describe_content(len(draft.files), len(draft.texts))}).\n"
            "Продолжайте добавлять или нажмите «✅ Отправить». Чтобы начать заново — «❌ Отмена».",
            reply_markup=draft_kb(),
        )
        return

    drafts[message.from_user.id] = Draft()
    await message.answer(
        "📝 Начинаем сдачу задания.\n\n"
        "Присылайте всё, что относится к этому заданию: текст, файлы "
        "(Word, PDF, PowerPoint, Excel…), фото, голосовые сообщения. "
        "Можно по одному или сразу несколько.\n\n"
        "Когда всё загрузите — нажмите «✅ Отправить». "
        f"(Максимум файлов: {MAX_SUBMISSION_FILES}.)",
        reply_markup=draft_kb(),
    )


@dp.message(F.text == BTN_CANCEL)
async def cancel_submission(message: Message):
    student = await require_student(message)
    if not student:
        return
    draft = drafts.pop(message.from_user.id, None)
    if draft is None or draft.is_empty():
        if draft:
            draft.cancel_summary()
        await message.answer("Отменять нечего — задание не начато.", reply_markup=main_menu_kb())
        return
    draft.cancel_summary()
    async with draft.lock:
        draft.discard_files()
    await message.answer("Задание отменено, ничего не отправлено.", reply_markup=main_menu_kb())


async def notify_teachers(submission_id: int, student: Student, files_count: int, texts: list[str]):
    if not TEACHER_CHAT_IDS:
        log.warning("TEACHER_CHAT_IDS не задан — уведомление не отправлено")
        return

    link = f"{PANEL_BASE_URL}/submission/{submission_id}"
    text = (
        f"📥 Новая работа от {student.full_name} ({student.display_group or '—'})\n"
        f"Состав: {describe_content(files_count, len(texts))}\n"
    )
    if texts:
        preview = texts[0].replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:97] + "..."
        text += f"Описание: {preview}\n"
    text += f"\nОткрыть и оставить отзыв: {link}"

    for chat_id in TEACHER_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не удалось отправить уведомление преподавателю %s", chat_id)


@dp.message(F.text == BTN_SEND)
async def confirm_submission(message: Message):
    student = await require_student(message)
    if not student:
        return

    draft = drafts.get(message.from_user.id)
    if draft is None or draft.is_empty():
        await message.answer(
            "Вы пока ничего не добавили. Пришлите текст, файлы или голосовое — "
            "а потом снова нажмите «✅ Отправить».",
            reply_markup=draft_kb() if draft is not None else main_menu_kb(),
        )
        return

    draft.cancel_summary()
    async with draft.lock:
        files = list(draft.files)
        texts = list(draft.texts)
        joined = "\n\n".join(texts) or None

        kinds = {f["kind"] for f in files}
        if not files:
            kind = "text"
        elif kinds == {"voice"}:
            kind = "voice"
        elif kinds == {"document"}:
            kind = "document"
        else:
            kind = "mixed"

        session = SessionLocal()
        try:
            submission = Submission(
                student_id=student.id,
                kind=kind,
                text_content=None if files else joined,
                caption=joined if files else None,
                status="new",
            )
            session.add(submission)
            session.flush()
            for i, f in enumerate(files):
                session.add(
                    SubmissionFile(
                        submission_id=submission.id,
                        file_path=f["path"],
                        original_filename=f["original"],
                        kind=f["kind"],
                        order_index=i,
                    )
                )
            session.commit()
            submission_id = submission.id
        except Exception:
            session.rollback()
            log.exception("Не удалось сохранить задание")
            await message.answer(
                "⚠️ Не удалось сохранить задание. Ваши материалы не потеряны — "
                "попробуйте нажать «✅ Отправить» ещё раз.",
                reply_markup=draft_kb(),
            )
            return
        finally:
            session.close()

        drafts.pop(message.from_user.id, None)

    await message.answer(
        "✅ Задание отправлено!\n\n"
        f"Передано преподавателю: {describe_content(len(files), len(texts))}.\n"
        "Когда работа будет проверена, я пришлю отзыв сюда.",
        reply_markup=main_menu_kb(),
    )
    await notify_teachers(submission_id, student, len(files), texts)


# ── приём материалов в черновик ──


@dp.message(F.voice)
async def on_voice(message: Message):
    student = await require_student(message)
    if not student:
        return
    draft = get_draft(message.from_user.id)
    async with draft.lock:
        await _add_attachment(
            draft, student, message.voice.file_id, message.voice.file_size,
            "Голосовое сообщение.ogg", ".ogg", "voice", "voice",
        )
        _add_caption(draft, message)
    schedule_summary(message, draft)


@dp.message(F.audio)
async def on_audio(message: Message):
    student = await require_student(message)
    if not student:
        return
    audio = message.audio
    name = audio.file_name or "audio.mp3"
    ext = Path(name).suffix.lower() or ".mp3"
    draft = get_draft(message.from_user.id)
    async with draft.lock:
        await _add_attachment(draft, student, audio.file_id, audio.file_size, name, ext, "audio", "audio")
        _add_caption(draft, message)
    schedule_summary(message, draft)


@dp.message(F.photo)
async def on_photo(message: Message):
    student = await require_student(message)
    if not student:
        return
    photo = message.photo[-1]
    draft = get_draft(message.from_user.id)
    async with draft.lock:
        name = f"Фото {len(draft.files) + 1}.jpg"
        await _add_attachment(draft, student, photo.file_id, photo.file_size, name, ".jpg", "document", "photo")
        _add_caption(draft, message)
    schedule_summary(message, draft)


@dp.message(F.document)
async def on_document(message: Message):
    student = await require_student(message)
    if not student:
        return
    doc = message.document
    name = doc.file_name or "file"
    ext = Path(name).suffix.lower()
    draft = get_draft(message.from_user.id)

    async with draft.lock:
        if ext in ALLOWED_DOCUMENT_EXTENSIONS:
            await _add_attachment(draft, student, doc.file_id, doc.file_size, name, ext, "document", "doc")
        elif ext in AUDIO_EXTENSIONS:
            await _add_attachment(draft, student, doc.file_id, doc.file_size, name, ext, "audio", "audio")
        else:
            allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
            _add_note(
                draft,
                f"⚠️ Файл «{name}» не принят: тип {ext or 'без расширения'} не поддерживается.\n"
                f"Поддерживаются: {allowed}",
            )
        _add_caption(draft, message)
    schedule_summary(message, draft)


# ───────────────────────────── история ─────────────────────────────


def _label(kind: str, caption: str | None, text: str | None) -> str:
    base = (caption or text or "").strip().replace("\n", " ")
    if not base:
        base = {"voice": "Голосовое", "document": "Файлы", "mixed": "Файлы", "text": "Текст"}.get(kind, "Задание")
    return base if len(base) <= 28 else base[:27] + "…"


def _build_history(student_id: int, page: int):
    session = SessionLocal()
    try:
        query = session.query(Submission).filter_by(student_id=student_id)
        total = query.count()
        pages = max(1, math.ceil(total / HISTORY_PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        items = (
            query.order_by(Submission.created_at.desc())
            .offset(page * HISTORY_PAGE_SIZE)
            .limit(HISTORY_PAGE_SIZE)
            .all()
        )
        rows = [
            {
                "id": s.id,
                "icon": "✅" if s.status == "reviewed" else "⏳",
                "when": fmt_time(s.created_at),
                "label": _label(s.kind, s.caption, s.text_content),
            }
            for s in items
        ]
    finally:
        session.close()

    if total == 0:
        return "Вы пока ничего не сдавали.", None

    builder = InlineKeyboardBuilder()
    for r in rows:
        builder.row(
            InlineKeyboardButton(
                text=f"{r['icon']} {r['when']} · {r['label']}",
                callback_data=f"h:v:{r['id']}:{page}",
            )
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"h:p:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="h:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"h:p:{page + 1}"))
        builder.row(*nav)

    text = f"📜 Ваши работы (всего {total}).\n✅ — проверено, ⏳ — ждёт проверки."
    return text, builder.as_markup()


@dp.message(F.text == BTN_HISTORY)
@dp.message(Command("history"))
async def cmd_history(message: Message):
    student = await require_student(message)
    if not student:
        return
    text, markup = _build_history(student.id, 0)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "h:noop")
async def history_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("h:p:"))
async def history_page(callback: CallbackQuery):
    student = await get_student(callback.from_user.id)
    if not student:
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    text, markup = _build_history(student.id, page)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("h:v:"))
async def history_view(callback: CallbackQuery):
    student = await get_student(callback.from_user.id)
    if not student:
        await callback.answer()
        return
    _, _, sid, page = callback.data.split(":")

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=int(sid), student_id=student.id).first()
        if not s:
            await callback.answer("Работа не найдена", show_alert=True)
            return
        data = {
            "when": fmt_time(s.created_at),
            "reviewed": s.status == "reviewed",
            "files": len(s.files) or (1 if s.file_path else 0),
            "text": s.text_content,
            "caption": s.caption,
            "feedback": s.feedback_text,
            "voice": s.feedback_voice_path,
            "kind": s.kind,
        }
    finally:
        session.close()

    lines = [
        f"📄 Работа от {data['when']}",
        "Статус: " + ("✅ проверено" if data["reviewed"] else "⏳ ждёт проверки"),
    ]
    own_text = data["text"] or data["caption"]
    contents = describe_content(data["files"], 1 if own_text else 0)
    if contents != "пусто":
        lines.append(f"Состав: {contents}")
    if own_text:
        shown = own_text if len(own_text) <= 700 else own_text[:697] + "..."
        lines += ["", "Ваш текст:", shown]
    if data["feedback"]:
        fb = data["feedback"] if len(data["feedback"]) <= 2500 else data["feedback"][:2497] + "..."
        lines += ["", "📝 Отзыв преподавателя:", fb]
    elif data["reviewed"] and data["voice"]:
        lines += ["", "📝 Преподаватель оставил голосовой отзыв (он ниже)."]

    back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку", callback_data=f"h:p:{page}")]]
    )
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=back)
    except Exception:
        pass
    await callback.answer()

    if data["voice"]:
        path = MEDIA_DIR / data["voice"]
        if path.exists():
            try:
                if path.suffix.lower() == ".ogg":
                    await callback.message.answer_voice(FSInputFile(path), caption="📝 Голосовой отзыв")
                else:
                    await callback.message.answer_audio(FSInputFile(path), caption="📝 Голосовой отзыв")
            except Exception:
                log.exception("Не удалось отправить голосовой отзыв студенту")


# ───────────────────────────── текст и всё остальное ─────────────────────────────


@dp.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message):
    student = await require_student(message)
    if not student:
        return
    draft = get_draft(message.from_user.id)
    async with draft.lock:
        draft.texts.append(message.text.strip())
    schedule_summary(message, draft)


@dp.message()
async def handle_other(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer("Похоже, вы ещё не зарегистрированы. Отправьте /start.")
        return
    await message.answer(
        "Я принимаю текст, файлы (Word, PDF, PowerPoint, Excel…), фото и голосовые сообщения. "
        "Нажмите «📝 Сдать задание», чтобы отправить работу.",
        reply_markup=main_menu_kb(),
    )


async def main():
    init_db()
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

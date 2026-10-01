import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
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

from config import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_DOCUMENT_EXTENSIONS,
    BOT_TOKEN,
    DRAFT_PANEL_DEBOUNCE_SECONDS,
    HISTORY_PAGE_SIZE,
    MAX_DOCUMENT_SIZE,
    MAX_DRAFT_ITEMS,
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


class Registration(StatesGroup):
    waiting_name = State()
    waiting_group = State()
    waiting_group_manual = State()


class Drafting(StatesGroup):
    waiting_caption = State()


# ---------------------------------------------------------------------------
# Черновик работы
#
# Всё, что студент присылает (файлы, аудио, голосовые, текст), копится в
# «черновике» и уходит преподавателю ОДНИМ заданием только после нажатия
# кнопки «Отправить». Черновики хранятся в памяти процесса: если бота
# перезапустить, неотправленные черновики пропадут (студент пришлёт заново).
# ---------------------------------------------------------------------------

@dataclass
class DraftItem:
    kind: str  # "text" | "voice" | "audio" | "document"
    order: int  # message_id — чтобы сохранить порядок отправки
    text: str | None = None
    file_path: str | None = None
    original_filename: str | None = None


@dataclass
class Draft:
    chat_id: int
    items: list[DraftItem] = field(default_factory=list)
    caption: str | None = None
    panel_msg_id: int | None = None
    pending_downloads: int = 0
    version: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_drafts: dict[int, Draft] = {}  # ключ — telegram_id студента


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _summary(items: list[DraftItem]) -> str:
    counts = Counter(i.kind for i in items)
    parts = []
    if counts["document"]:
        n = counts["document"]
        parts.append(f"{n} {_plural(n, 'файл', 'файла', 'файлов')}")
    if counts["audio"]:
        n = counts["audio"]
        parts.append(f"{n} {_plural(n, 'аудиофайл', 'аудиофайла', 'аудиофайлов')}")
    if counts["voice"]:
        n = counts["voice"]
        parts.append(f"{n} {_plural(n, 'голосовое', 'голосовых', 'голосовых')}")
    if counts["text"]:
        n = counts["text"]
        parts.append(f"{n} {_plural(n, 'текст', 'текста', 'текстов')}")
    return ", ".join(parts)


def _short(s: str, limit: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _item_line(it: DraftItem) -> str:
    if it.kind == "text":
        return f"✍️ {_short(it.text or '', 40)}"
    icon = {"document": "📎", "audio": "🎵", "voice": "🎤"}.get(it.kind, "•")
    name = it.original_filename or "голосовое сообщение"
    return f"{icon} {_short(name, 40)}"


def _panel_text(draft: Draft) -> str:
    items = sorted(draft.items, key=lambda i: i.order)
    lines = ["📦 Ваша работа собрана, но ещё НЕ отправлена", ""]
    lines.append(f"В ней: {_summary(items)}")
    shown = items[:10]
    for it in shown:
        lines.append(f"  {_item_line(it)}")
    if len(items) > len(shown):
        lines.append(f"  …и ещё {len(items) - len(shown)}")
    lines.append("")
    if draft.caption:
        lines.append(f"📝 Описание: «{_short(draft.caption, 120)}»")
    else:
        lines.append("📝 Описание: не добавлено")
    lines.append("")
    lines.append(
        "Хотите добавить что-то ещё? Просто пришлите сюда файлы, аудио, голосовое или текст — "
        "всё уйдёт преподавателю одним заданием.\n"
        "Когда всё готово — нажмите «Отправить»."
    )
    return "\n".join(lines)


def _panel_keyboard(draft: Draft) -> InlineKeyboardMarkup:
    cap_label = "✏️ Изменить описание" if draft.caption else "📝 Добавить описание"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить преподавателю", callback_data="draft:send")],
            [InlineKeyboardButton(text=cap_label, callback_data="draft:caption")],
            [
                InlineKeyboardButton(text="↩️ Убрать последнее", callback_data="draft:undo"),
                InlineKeyboardButton(text="🗑 Отменить", callback_data="draft:cancel"),
            ],
        ]
    )


def _get_draft(message: Message) -> Draft:
    tg_id = message.from_user.id
    draft = _drafts.get(tg_id)
    if draft is None:
        draft = Draft(chat_id=message.chat.id)
        _drafts[tg_id] = draft
    return draft


def _delete_media_file(name: str | None):
    if not name:
        return
    try:
        (MEDIA_DIR / name).unlink(missing_ok=True)
    except Exception:
        log.exception("Не удалось удалить файл %s", name)


def _discard_draft(tg_id: int):
    draft = _drafts.pop(tg_id, None)
    if draft:
        for it in draft.items:
            _delete_media_file(it.file_path)


async def _show_panel(tg_id: int):
    """Показывает панель черновика внизу чата (старую панель удаляет,
    чтобы актуальная всегда была последним сообщением)."""
    draft = _drafts.get(tg_id)
    if not draft or not draft.items:
        return
    async with draft.lock:
        if draft.panel_msg_id:
            try:
                await bot.delete_message(draft.chat_id, draft.panel_msg_id)
            except Exception:
                pass
            draft.panel_msg_id = None
        msg = await bot.send_message(
            draft.chat_id, _panel_text(draft), reply_markup=_panel_keyboard(draft)
        )
        draft.panel_msg_id = msg.message_id


async def _debounced_panel(tg_id: int, version: int):
    # Если студент шлёт несколько файлов подряд, показываем панель один раз —
    # после последнего.
    await asyncio.sleep(DRAFT_PANEL_DEBOUNCE_SECONDS)
    draft = _drafts.get(tg_id)
    if not draft or draft.version != version:
        return
    try:
        await _show_panel(tg_id)
    except Exception:
        log.exception("Не удалось показать панель черновика")


async def _after_add(message: Message, state: FSMContext, draft: Draft):
    """Вызывается после добавления вложения в черновик."""
    cap = (message.caption or "").strip()
    if cap and not draft.caption:
        draft.caption = cap
    if await state.get_state() == Drafting.waiting_caption.state:
        await state.clear()
    draft.version += 1
    asyncio.create_task(_debounced_panel(message.from_user.id, draft.version))


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------

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


WELCOME_AFTER_REG = (
    "Теперь вы можете присылать сюда домашние задания:\n"
    "— файлы (Word, PDF, PowerPoint, Excel и т.д.), можно несколько\n"
    "— аудио и голосовые сообщения\n"
    "— текст\n\n"
    "Всё, что вы пришлёте, соберётся в одну работу. Когда закончите — "
    "нажмите «✅ Отправить преподавателю». Перед отправкой можно добавить описание задания."
)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)

    if student:
        await message.answer(
            f"С возвращением, {student.full_name}!\n\n"
            "Присылайте файлы, аудио, голосовые или текст с выполненным заданием — "
            "всё соберётся в одну работу, а в конце я спрошу, отправлять ли.",
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
    await callback.message.answer(WELCOME_AFTER_REG, reply_markup=MAIN_MENU)
    await callback.answer()


@dp.message(Registration.waiting_group_manual)
async def process_group_manual(message: Message, state: FSMContext):
    group_name = (message.text or "").strip()
    data = await state.get_data()
    full_name = data.get("full_name", "Без имени")

    await _finish_registration(message.from_user.id, message.from_user.username, full_name, None, group_name)
    await state.clear()

    await message.answer(f"Готово, {full_name} ({group_name})!\n\n{WELCOME_AFTER_REG}", reply_markup=MAIN_MENU)


# ---------------------------------------------------------------------------
# Уведомление преподавателям
# ---------------------------------------------------------------------------

async def notify_teachers(submission_id: int, student: Student, summary: str, preview: str | None):
    if not TEACHER_CHAT_IDS:
        log.warning("TEACHER_CHAT_IDS не задан — уведомление не отправлено")
        return

    link = f"{PANEL_BASE_URL}/submission/{submission_id}"
    text = (
        f"📥 Новая работа от {student.full_name} ({student.group_name or '—'})\n"
        f"Состав: {summary}\n"
    )
    if preview:
        text += f"Описание: {preview}\n"
    text += f"\nОткрыть и оставить отзыв: {link}"

    for chat_id in TEACHER_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не удалось отправить уведомление преподавателю %s", chat_id)


# ---------------------------------------------------------------------------
# Приём вложений в черновик
# ---------------------------------------------------------------------------

async def _receive_file(
    message: Message,
    state: FSMContext,
    student: Student,
    *,
    kind: str,
    file_id: str,
    ext: str,
    original_name: str | None,
    size: int | None,
):
    if size and size > MAX_DOCUMENT_SIZE:
        await message.answer(f"«{original_name or 'файл'}» слишком большой (максимум 20 МБ).")
        return

    draft = _get_draft(message)
    if len(draft.items) + draft.pending_downloads >= MAX_DRAFT_ITEMS:
        await message.answer(
            f"В одной работе может быть не больше {MAX_DRAFT_ITEMS} вложений. "
            "Отправьте текущую работу, а остальное — следующей."
        )
        return

    filename = f"{kind}_{student.id}_{int(time.time() * 1000)}_{message.message_id}{ext}"
    draft.pending_downloads += 1
    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=MEDIA_DIR / filename)
    except Exception:
        log.exception("Не удалось скачать файл от студента %s", student.id)
        await message.answer("Не получилось загрузить файл, попробуйте прислать его ещё раз.")
        return
    finally:
        draft.pending_downloads -= 1

    draft.items.append(
        DraftItem(kind=kind, order=message.message_id, file_path=filename, original_filename=original_name)
    )
    await _after_add(message, state, draft)


NOT_REGISTERED = "Похоже, вы ещё не зарегистрированы. Отправьте /start."


@dp.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer(NOT_REGISTERED)
        return
    await _receive_file(
        message, state, student,
        kind="voice", file_id=message.voice.file_id, ext=".ogg",
        original_name=None, size=message.voice.file_size,
    )


@dp.message(F.audio)
async def handle_audio(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer(NOT_REGISTERED)
        return
    audio = message.audio
    original_name = audio.file_name or audio.title or "аудио"
    ext = Path(audio.file_name).suffix.lower() if audio.file_name else ""
    if not ext or len(ext) > 6:
        ext = ".mp3"
    if not Path(original_name).suffix:
        original_name += ext
    await _receive_file(
        message, state, student,
        kind="audio", file_id=audio.file_id, ext=ext,
        original_name=original_name, size=audio.file_size,
    )


def _validate_document(doc) -> str | None:
    """Возвращает текст ошибки, если файл не подходит, иначе None."""
    original_name = doc.file_name or "file"
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS and ext not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS))
        return f"«{original_name}» — неподдерживаемый тип файла ({ext or 'без расширения'}). Поддерживаются: {allowed}"
    if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE:
        return f"«{original_name}» слишком большой (максимум 20 МБ)."
    return None


@dp.message(F.document)
async def handle_document(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer(NOT_REGISTERED)
        return

    doc = message.document
    err = _validate_document(doc)
    if err:
        await message.answer(err)
        return

    original_name = doc.file_name or "file"
    ext = Path(original_name).suffix.lower()
    kind = "audio" if ext in ALLOWED_AUDIO_EXTENSIONS else "document"
    await _receive_file(
        message, state, student,
        kind=kind, file_id=doc.file_id, ext=ext,
        original_name=original_name, size=doc.file_size,
    )


# ---------------------------------------------------------------------------
# Кнопки под панелью черновика
# ---------------------------------------------------------------------------

def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="draft:back")]]
    )


async def _edit_to_main(callback: CallbackQuery, draft: Draft):
    try:
        await callback.message.edit_text(_panel_text(draft), reply_markup=_panel_keyboard(draft))
    except Exception:
        pass  # например, "message is not modified"


def _build_submission(student: Student, draft: Draft) -> tuple[int, str, str | None]:
    items = sorted(draft.items, key=lambda i: i.order)
    texts = [i.text for i in items if i.kind == "text" and i.text]
    file_items = [i for i in items if i.kind != "text"]

    file_kinds = {i.kind for i in file_items}
    if not file_items:
        kind = "text"
    elif texts:
        kind = "mixed"
    elif file_kinds == {"voice"}:
        kind = "voice"
    elif file_kinds <= {"document", "audio"}:
        kind = "document"
    else:
        kind = "mixed"

    session = SessionLocal()
    try:
        submission = Submission(
            student_id=student.id,
            kind=kind,
            text_content="\n\n".join(texts) or None,
            caption=draft.caption,
            status="new",
        )
        session.add(submission)
        session.flush()
        for idx, it in enumerate(file_items):
            session.add(
                SubmissionFile(
                    submission_id=submission.id,
                    file_path=it.file_path,
                    original_filename=it.original_filename,
                    kind=it.kind,
                    order_index=idx,
                )
            )
        session.commit()
        submission_id = submission.id
    finally:
        session.close()

    preview = draft.caption or (_short(texts[0], 100) if texts else None)
    return submission_id, _summary(items), preview


async def _do_send(callback: CallbackQuery, state: FSMContext):
    tg_id = callback.from_user.id
    draft = _drafts.pop(tg_id, None)  # сразу убираем — защита от двойного нажатия
    if not draft or not draft.items:
        await callback.answer("Эта работа уже отправлена.", show_alert=True)
        return

    student = await get_student(tg_id)
    if not student:
        _drafts[tg_id] = draft
        await callback.answer("Вы не зарегистрированы. Отправьте /start.", show_alert=True)
        return

    try:
        submission_id, summary, preview = _build_submission(student, draft)
    except Exception:
        log.exception("Не удалось сохранить работу студента %s", student.id)
        _drafts[tg_id] = draft
        await callback.answer("Не получилось отправить, попробуйте ещё раз.", show_alert=True)
        return

    await state.clear()

    lines = [
        "✅ Работа отправлена!",
        "",
        f"📦 В ней: {summary}",
    ]
    if draft.caption:
        lines.append(f"📝 Описание: «{_short(draft.caption, 120)}»")
    lines += [
        "",
        "Преподаватель уже получил уведомление. Как только появится отзыв — "
        "я пришлю его сюда. Статус работы всегда можно посмотреть в «📜 Моя история».",
    ]
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=None)
    except Exception:
        await callback.message.answer("\n".join(lines))
    await callback.answer("✅ Отправлено!")

    await notify_teachers(submission_id, student, summary, preview)


@dp.callback_query(F.data.startswith("draft:"))
async def cb_draft(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    tg_id = callback.from_user.id
    draft = _drafts.get(tg_id)

    if not draft or not draft.items:
        await callback.answer("Эта работа уже отправлена или отменена.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action in ("send", "send_yes", "undo", "cancel", "cancel_yes") and draft.pending_downloads:
        await callback.answer("Подождите секунду — файлы ещё загружаются ⏳", show_alert=True)
        return

    if action == "caption":
        await state.set_state(Drafting.waiting_caption)
        await callback.message.edit_text(
            "📝 Напишите описание задания одним сообщением.\n"
            "Например: «Задание 3, диалог о путешествиях».",
            reply_markup=_back_keyboard(),
        )
        await callback.answer()

    elif action == "back":
        await state.clear()
        await _edit_to_main(callback, draft)
        await callback.answer()

    elif action == "undo":
        draft.items.sort(key=lambda i: i.order)
        it = draft.items.pop()
        _delete_media_file(it.file_path)
        if not draft.items:
            _drafts.pop(tg_id, None)
            await callback.message.edit_text(
                "↩️ Всё убрано. Пришлите задание заново, когда будете готовы."
            )
        else:
            await _edit_to_main(callback, draft)
        await callback.answer("Убрано")

    elif action == "cancel":
        await callback.message.edit_text(
            "Отменить работу? Всё, что вы прислали, будет удалено, ничего не отправится.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🗑 Да, отменить", callback_data="draft:cancel_yes")],
                    [InlineKeyboardButton(text="⬅️ Нет, вернуться", callback_data="draft:back")],
                ]
            ),
        )
        await callback.answer()

    elif action == "cancel_yes":
        _discard_draft(tg_id)
        await state.clear()
        await callback.message.edit_text("🗑 Работа отменена, ничего не отправлено.")
        await callback.answer()

    elif action == "send":
        if draft.caption:
            await _do_send(callback, state)
        else:
            await callback.message.edit_text(
                "У работы нет описания. Преподавателю будет проще, если вы коротко напишете, "
                "что это за задание.\n\nОтправить без описания?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Да, отправить так", callback_data="draft:send_yes")],
                        [InlineKeyboardButton(text="📝 Добавить описание", callback_data="draft:caption")],
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="draft:back")],
                    ]
                ),
            )
            await callback.answer()

    elif action == "send_yes":
        await _do_send(callback, state)

    else:
        await callback.answer()


# ---------------------------------------------------------------------------
# История
# ---------------------------------------------------------------------------

@dp.message(F.text == HISTORY_BUTTON_TEXT)
@dp.message(Command("history"))
async def cmd_history(message: Message):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer(NOT_REGISTERED)
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

        kind_icons = {"voice": "🎤", "document": "📎", "text": "✍️", "mixed": "📦"}
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
            await callback.answer("Эта работа не найдена (возможно, удалена).", show_alert=True)
            return
        data = {
            "kind": s.kind,
            "caption": s.caption,
            "text_content": s.text_content,
            "created_at": s.created_at,
            "status": s.status,
            "feedback_text": s.feedback_text,
            "feedback_voice_path": s.feedback_voice_path,
            "file_count": len(s.files) or (1 if s.file_path else 0),
        }
    finally:
        session.close()

    kind_labels = {
        "voice": "голосовое сообщение",
        "document": "файл(ы)",
        "text": "текст",
        "mixed": "несколько вложений",
    }
    lines = [f"📅 {data['created_at'].strftime('%d.%m.%Y %H:%M')}"]
    lines.append(f"Тип: {kind_labels.get(data['kind'], data['kind'])}")
    if data["caption"]:
        lines.append(f"Задание: {data['caption']}")
    if data["file_count"]:
        lines.append(f"Вложений: {data['file_count']}")
    if data["text_content"]:
        preview = data["text_content"] if len(data["text_content"]) <= 300 else data["text_content"][:297] + "..."
        lines.append(f"\nВаш текст:\n{preview}")

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
            await bot.send_voice(callback.message.chat.id, FSInputFile(voice_path))

    await callback.answer()


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

@dp.message(Drafting.waiting_caption, F.text)
async def handle_caption(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    draft = _drafts.get(tg_id)
    await state.clear()
    if not draft or not draft.items:
        await message.answer("Сначала пришлите работу, а потом добавьте описание.")
        return
    draft.caption = message.text.strip()
    draft.version += 1  # отменяем отложенное обновление панели, если оно было
    await _show_panel(tg_id)


@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    student = await get_student(message.from_user.id)
    if not student:
        await message.answer(NOT_REGISTERED)
        return

    text = message.text.strip()
    if not text:
        return
    if text.startswith("/"):
        await message.answer("Не знаю такой команды. Пришлите задание или нажмите «📜 Моя история».")
        return

    draft = _get_draft(message)
    if len(draft.items) + draft.pending_downloads >= MAX_DRAFT_ITEMS:
        await message.answer(
            f"В одной работе может быть не больше {MAX_DRAFT_ITEMS} вложений. "
            "Отправьте текущую работу, а остальное — следующей."
        )
        return

    draft.items.append(DraftItem(kind="text", order=message.message_id, text=text))
    await _after_add(message, state, draft)


@dp.message()
async def handle_other(message: Message):
    await message.answer(
        "Пока что я умею принимать текст, аудио, голосовые сообщения и файлы "
        "(Word, PDF, PowerPoint, Excel и т.д. — можно несколько сразу). "
        "Отправьте задание одним из этих способов."
    )


async def main():
    init_db()
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from starlette.middleware.sessions import SessionMiddleware

from config import (
    BOT_TOKEN,
    FFMPEG_PATH,
    MEDIA_DIR,
    PANEL_PASSWORD,
    SECRET_KEY,
    WEB_HOST,
    WEB_PORT,
)
from database import Group, Student, Submission, SubmissionFile, SessionLocal, init_db

init_db()

app = FastAPI(title="Домашние задания — панель преподавателя")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

KIND_LABELS = {
    "voice": "голосовое сообщение",
    "document": "файл(ы)",
    "mixed": "файл(ы)",
    "text": "текстовое задание",
}


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("logged_in"))


def _short(text: str | None, limit: int = 60) -> str | None:
    if not text:
        return text
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _students_url(group: str | None, sort: str | None) -> str:
    params = {}
    if group:
        params["group"] = group
    if sort:
        params["sort"] = sort
    qs = urlencode(params)
    return "/students" + (("?" + qs) if qs else "")


def _transcode_to_ogg_sync(input_path: Path, output_path: Path) -> bool:
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-y", "-i", str(input_path), "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(output_path)],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


async def _transcode_to_ogg(input_path: Path, output_path: Path) -> bool:
    return await asyncio.to_thread(_transcode_to_ogg_sync, input_path, output_path)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "authed": False})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PANEL_PASSWORD):
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный пароль", "authed": False}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status: str = "new", q: str = ""):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    q = q.strip()
    session = SessionLocal()
    try:
        query = session.query(Submission).order_by(Submission.created_at.desc())
        if q:
            like = f"%{q}%"
            query = query.join(Student).filter(
                or_(
                    Student.full_name.ilike(like),
                    Submission.text_content.ilike(like),
                    Submission.caption.ilike(like),
                )
            )
        elif status in ("new", "reviewed"):
            query = query.filter_by(status=status)

        rows = [
            {
                "id": s.id,
                "student_name": s.student.full_name,
                "group": s.student.display_group,
                "kind": s.kind,
                "caption": s.caption,
                "created_at": s.created_at,
                "status": s.status,
            }
            for s in query.all()
        ]
    finally:
        session.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "submissions": rows,
            "status": status,
            "q": q,
            "deleted": request.query_params.get("deleted") == "1",
            "authed": True,
            "active_page": "dashboard",
        },
    )


@app.get("/submission/{submission_id}", response_class=HTMLResponse)
async def submission_detail(request: Request, submission_id: int):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=submission_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        data = {
            "id": s.id,
            "student_name": s.student.full_name,
            "group": s.student.display_group,
            "kind": s.kind,
            "text_content": s.text_content,
            "file_path": s.file_path,
            "original_filename": s.original_filename,
            "files": [
                {"file_path": f.file_path, "original_filename": f.original_filename, "kind": f.kind}
                for f in s.files
            ],
            "caption": s.caption,
            "created_at": s.created_at,
            "status": s.status,
            "feedback_text": s.feedback_text,
            "feedback_voice_path": s.feedback_voice_path,
        }
    finally:
        session.close()

    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "submission.html",
        {"s": data, "error": error, "authed": True, "active_page": None},
    )


@app.get("/media/{filename}")
async def get_media(request: Request, filename: str):
    if not is_logged_in(request):
        raise HTTPException(status_code=403)
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(file_path)


@app.get("/submission/{submission_id}/delete", response_class=HTMLResponse)
async def delete_submission_confirm(request: Request, submission_id: int):
    """Шаг 1 из 2: страница «Вы уверены?». Само удаление — POST ниже."""
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=submission_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        files_count = len(s.files) or (1 if s.file_path else 0)
        data = {
            "id": s.id,
            "student_name": s.student.full_name,
            "group": s.student.display_group,
            "caption": _short(s.caption or s.text_content, 200),
            "created_at": s.created_at,
            "files_count": files_count,
            "reviewed": s.status == "reviewed",
        }
    finally:
        session.close()

    return templates.TemplateResponse(
        request,
        "delete_confirm.html",
        {"s": data, "authed": True, "active_page": None},
    )


@app.post("/submission/{submission_id}/delete")
async def delete_submission(request: Request, submission_id: int, confirm: str = Form("")):
    """Шаг 2 из 2: фактическое удаление. Студенту ничего не отправляется."""
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    if confirm != "yes":
        return RedirectResponse(f"/submission/{submission_id}", status_code=303)

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=submission_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        names = [f.file_path for f in s.files]
        if s.file_path:
            names.append(s.file_path)
        if s.feedback_voice_path:
            names.append(s.feedback_voice_path)
        session.delete(s)
        session.commit()
    finally:
        session.close()

    for name in names:
        try:
            (MEDIA_DIR / Path(name).name).unlink(missing_ok=True)
        except OSError:
            pass

    return RedirectResponse("/?status=all&deleted=1", status_code=303)


@app.post("/submission/{submission_id}/feedback")
async def send_feedback(
    request: Request,
    submission_id: int,
    feedback_text: str = Form(""),
    voice: UploadFile | None = File(None),
):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    feedback_text = feedback_text.strip()
    has_voice = voice is not None and bool(voice.filename)

    if not feedback_text and not has_voice:
        return RedirectResponse(f"/submission/{submission_id}?error=empty", status_code=303)

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=submission_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        was_reviewed = s.status == "reviewed"

        voice_rel_path = None
        if has_voice:
            raw_bytes = await voice.read()
            suffix = Path(voice.filename).suffix or ".webm"
            ts = int(datetime.utcnow().timestamp() * 1000)
            tmp_input = MEDIA_DIR / f"_tmp_feedback_{submission_id}_{ts}{suffix}"
            tmp_input.write_bytes(raw_bytes)

            out_filename = f"feedback_voice_{submission_id}_{ts}.ogg"
            out_path = MEDIA_DIR / out_filename
            converted = await _transcode_to_ogg(tmp_input, out_path)
            tmp_input.unlink(missing_ok=True)

            if converted:
                voice_rel_path = out_filename
            else:
                # ffmpeg недоступен или конвертация не удалась — сохраняем как есть
                fallback_name = f"feedback_voice_{submission_id}_{ts}{suffix}"
                (MEDIA_DIR / fallback_name).write_bytes(raw_bytes)
                voice_rel_path = fallback_name

        if feedback_text:
            s.feedback_text = feedback_text
        if voice_rel_path:
            s.feedback_voice_path = voice_rel_path
        s.status = "reviewed"
        s.feedback_at = datetime.utcnow()
        session.commit()

        telegram_id = s.student.telegram_id
        task_label = _short(s.caption or s.text_content) or KIND_LABELS.get(s.kind, "задание")
        is_edit = was_reviewed
        voice_is_ogg = voice_rel_path is not None and voice_rel_path.endswith(".ogg")
    finally:
        session.close()

    async with httpx.AsyncClient(timeout=20) as client:
        if feedback_text:
            prefix = "✏️ Отзыв обновлён" if is_edit else "📝 Отзыв"
            message = f"{prefix} на задание «{task_label}»:\n\n{feedback_text}"
            try:
                await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": telegram_id, "text": message})
            except Exception:
                pass
        if voice_rel_path:
            try:
                file_bytes = (MEDIA_DIR / voice_rel_path).read_bytes()
                endpoint = "sendVoice" if voice_is_ogg else "sendAudio"
                field_name = "voice" if voice_is_ogg else "audio"
                caption = "✏️ Голосовой отзыв обновлён" if is_edit else "📝 Голосовой отзыв"
                await client.post(
                    f"{TELEGRAM_API}/{endpoint}",
                    data={"chat_id": telegram_id, "caption": caption},
                    files={field_name: (voice_rel_path, file_bytes)},
                )
            except Exception:
                pass

    return RedirectResponse(f"/submission/{submission_id}", status_code=303)


@app.get("/students", response_class=HTMLResponse)
async def students_list(request: Request, group: str = "", sort: str = "recent"):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        all_students = session.query(Student).all()

        group_counts: dict[str, int] = {}
        no_group_count = 0
        for st in all_students:
            name = (st.display_group or "").strip()
            if name:
                group_counts[name] = group_counts.get(name, 0) + 1
            else:
                no_group_count += 1

        groups = [
            {"name": name, "count": count}
            for name, count in sorted(group_counts.items(), key=lambda kv: kv[0].lower())
        ]

        if group == "__none__":
            filtered = [st for st in all_students if not (st.display_group or "").strip()]
        elif group:
            filtered = [st for st in all_students if (st.display_group or "").strip() == group]
        else:
            filtered = list(all_students)

        rows = []
        for st in filtered:
            submissions = st.submissions
            new_count = sum(1 for s in submissions if s.status == "new")
            last_activity = max(
                (s.created_at for s in submissions), default=st.created_at
            )
            rows.append(
                {
                    "id": st.id,
                    "full_name": st.full_name,
                    "group_name": st.display_group,
                    "username": st.username,
                    "notes": st.notes,
                    "submission_count": len(submissions),
                    "new_count": new_count,
                    "last_activity": last_activity,
                }
            )

        if sort == "name":
            rows.sort(key=lambda r: r["full_name"].lower())
        elif sort == "group":
            rows.sort(key=lambda r: ((r["group_name"] or "").lower(), r["full_name"].lower()))
        else:
            sort = "recent"
            rows.sort(key=lambda r: r["last_activity"], reverse=True)

        context = {
            "students": rows,
            "groups": groups,
            "selected_group": group,
            "has_no_group": no_group_count > 0,
            "no_group_count": no_group_count,
            "total_count": len(all_students),
            "sort": sort,
            "sort_urls": {
                "name": _students_url(group, "name"),
                "group": _students_url(group, "group"),
                "recent": _students_url(group, "recent"),
            },
            "authed": True,
            "active_page": "students",
        }
    finally:
        session.close()

    return templates.TemplateResponse(request, "students.html", context)


@app.get("/students/{student_id}", response_class=HTMLResponse)
async def student_detail(request: Request, student_id: int, saved: bool = False):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        st = session.query(Student).filter_by(id=student_id).first()
        if not st:
            raise HTTPException(status_code=404, detail="Студент не найден")
        student_data = {
            "id": st.id,
            "full_name": st.full_name,
            "group_id": st.group_id,
            "legacy_group_name": st.group_name if not st.group_id else None,
            "username": st.username,
            "notes": st.notes,
            "created_at": st.created_at,
        }
        submissions = [
            {
                "id": s.id,
                "kind": s.kind,
                "caption": s.caption,
                "created_at": s.created_at,
                "status": s.status,
            }
            for s in st.submissions
        ]
        groups = [{"id": g.id, "name": g.name} for g in session.query(Group).order_by(Group.name).all()]
    finally:
        session.close()

    return templates.TemplateResponse(
        request,
        "student_detail.html",
        {
            "st": student_data,
            "submissions": submissions,
            "groups": groups,
            "saved": saved,
            "authed": True,
            "active_page": "students",
        },
    )


@app.post("/students/{student_id}")
async def student_update(
    request: Request,
    student_id: int,
    full_name: str = Form(...),
    group_choice: str = Form(""),
    notes: str = Form(""),
):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    full_name = full_name.strip()
    notes = notes.strip() or None

    session = SessionLocal()
    try:
        st = session.query(Student).filter_by(id=student_id).first()
        if not st:
            raise HTTPException(status_code=404, detail="Студент не найден")
        st.full_name = full_name or st.full_name

        if group_choice == "__legacy__":
            pass  # оставить как есть (старое текстовое значение группы)
        elif group_choice == "":
            st.group_id = None
            st.group_name = None
        else:
            try:
                st.group_id = int(group_choice)
                st.group_name = None
            except ValueError:
                pass

        st.notes = notes
        session.commit()
    finally:
        session.close()

    return RedirectResponse(f"/students/{student_id}?saved=1", status_code=303)


@app.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        groups = session.query(Group).order_by(Group.name).all()
        rows = [{"id": g.id, "name": g.name, "student_count": len(g.students)} for g in groups]
    finally:
        session.close()

    return templates.TemplateResponse(
        request, "groups.html", {"groups": rows, "authed": True, "active_page": "groups"}
    )


@app.post("/groups")
async def create_group(request: Request, name: str = Form(...)):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    name = name.strip()
    if name:
        session = SessionLocal()
        try:
            exists = session.query(Group).filter_by(name=name).first()
            if not exists:
                session.add(Group(name=name))
                session.commit()
        finally:
            session.close()
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}")
async def rename_group(request: Request, group_id: int, name: str = Form(...)):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    name = name.strip()
    session = SessionLocal()
    try:
        g = session.query(Group).filter_by(id=group_id).first()
        if g and name:
            g.name = name
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}/delete")
async def delete_group(request: Request, group_id: int):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        g = session.query(Group).filter_by(id=group_id).first()
        if g:
            for st in g.students:
                st.group_name = g.name
                st.group_id = None
            session.delete(g)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/groups", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)

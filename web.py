import secrets
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import BOT_TOKEN, MEDIA_DIR, PANEL_PASSWORD, SECRET_KEY, WEB_HOST, WEB_PORT
from database import Student, Submission, SessionLocal, init_db

init_db()

app = FastAPI(title="Домашние задания — панель преподавателя")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("logged_in"))


def _students_url(group: str | None, sort: str | None) -> str:
    params = {}
    if group:
        params["group"] = group
    if sort:
        params["sort"] = sort
    qs = urlencode(params)
    return "/students" + (("?" + qs) if qs else "")


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
async def dashboard(request: Request, status: str = "new"):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        query = session.query(Submission).order_by(Submission.created_at.desc())
        if status in ("new", "reviewed"):
            query = query.filter_by(status=status)
        rows = [
            {
                "id": s.id,
                "student_name": s.student.full_name,
                "group": s.student.group_name,
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
        {"submissions": rows, "status": status, "authed": True, "active_page": "dashboard"},
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
            "group": s.student.group_name,
            "kind": s.kind,
            "text_content": s.text_content,
            "file_path": s.file_path,
            "original_filename": s.original_filename,
            "caption": s.caption,
            "created_at": s.created_at,
            "status": s.status,
            "feedback_text": s.feedback_text,
        }
    finally:
        session.close()

    return templates.TemplateResponse(
        request, "submission.html", {"s": data, "authed": True, "active_page": None}
    )


@app.get("/media/{filename}")
async def get_media(request: Request, filename: str):
    if not is_logged_in(request):
        raise HTTPException(status_code=403)
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(file_path)


@app.post("/submission/{submission_id}/feedback")
async def send_feedback(
    request: Request, submission_id: int, feedback_text: str = Form(...)
):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    session = SessionLocal()
    try:
        s = session.query(Submission).filter_by(id=submission_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        s.feedback_text = feedback_text
        s.status = "reviewed"
        s.feedback_at = datetime.utcnow()
        session.commit()
        telegram_id = s.student.telegram_id
        task_label = s.caption or ("голосовое сообщение" if s.kind == "voice" else "текстовое задание")
    finally:
        session.close()

    message = f"📝 Отзыв на задание «{task_label}»:\n\n{feedback_text}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": telegram_id, "text": message},
            )
        except Exception:
            pass  # отзыв уже сохранён в базе, повторно попытаться можно вручную

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
            name = (st.group_name or "").strip()
            if name:
                group_counts[name] = group_counts.get(name, 0) + 1
            else:
                no_group_count += 1

        groups = [
            {"name": name, "count": count}
            for name, count in sorted(group_counts.items(), key=lambda kv: kv[0].lower())
        ]

        if group == "__none__":
            filtered = [st for st in all_students if not (st.group_name or "").strip()]
        elif group:
            filtered = [st for st in all_students if (st.group_name or "").strip() == group]
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
                    "group_name": st.group_name,
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
            "group_name": st.group_name,
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
    finally:
        session.close()

    return templates.TemplateResponse(
        request,
        "student_detail.html",
        {
            "st": student_data,
            "submissions": submissions,
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
    group_name: str = Form(""),
    notes: str = Form(""),
):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    full_name = full_name.strip()
    group_name = group_name.strip() or None
    notes = notes.strip() or None

    session = SessionLocal()
    try:
        st = session.query(Student).filter_by(id=student_id).first()
        if not st:
            raise HTTPException(status_code=404, detail="Студент не найден")
        st.full_name = full_name or st.full_name
        st.group_name = group_name
        st.notes = notes
        session.commit()
    finally:
        session.close()

    return RedirectResponse(f"/students/{student_id}?saved=1", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)

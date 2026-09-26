import secrets
from datetime import datetime

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import BOT_TOKEN, MEDIA_DIR, PANEL_PASSWORD, SECRET_KEY, WEB_HOST, WEB_PORT
from database import Submission, SessionLocal, init_db

init_db()

app = FastAPI(title="Домашние задания — панель преподавателя")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("logged_in"))


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PANEL_PASSWORD):
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный пароль"}
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
        request, "dashboard.html", {"submissions": rows, "status": status}
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
            "caption": s.caption,
            "created_at": s.created_at,
            "status": s.status,
            "feedback_text": s.feedback_text,
        }
    finally:
        session.close()

    return templates.TemplateResponse(request, "submission.html", {"s": data})


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)

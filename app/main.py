import json
import shutil
import uuid
from pathlib import Path
from fastapi import FastAPI, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR
from app.models import init_db, get_db, Article, User, SessionLocal
from app.auth import (
    hash_password, verify_password,
    create_session_cookie, get_current_user,
    COOKIE_NAME, login_required_middleware, ensure_admin_exists,
)
from app.services.batch_runner import (
    parse_csv, start_worker, run_queued,
)

app = FastAPI(title="Health Content Writer")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Auth middleware — must be added before routes
login_required_middleware(app)


@app.on_event("startup")
async def startup():
    init_db()
    db = SessionLocal()
    ensure_admin_exists(db)
    db.close()
    await start_worker(SessionLocal)


# ─── Helpers ──────────────────────────────────────────────

def save_upload(file: UploadFile) -> str:
    ext = Path(file.filename or "file.jpg").suffix
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return str(dest)


def _get_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if not user:
        raise Exception("Not logged in")
    return user


# ─── Login ────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Sai tên đăng nhập hoặc mật khẩu"
        })
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(COOKIE_NAME, create_session_cookie(user.id), httponly=True, max_age=86400 * 30)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ─── Dashboard ────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    base = db.query(Article).filter(Article.user_id == user.id)

    published_today = base.filter(
        Article.status == "published", Article.published_at >= today_start
    ).count()
    failed_today = base.filter(
        Article.status == "failed", Article.created_at >= today_start
    ).count()
    processing = base.filter(
        Article.status.in_(["pending", "generating", "images", "uploading", "publishing"])
    ).count()
    total_all = base.filter(Article.status == "published").count()

    recent = base.order_by(Article.created_at.desc()).limit(30).all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "published_today": published_today,
        "failed_today": failed_today,
        "processing_count": processing,
        "total_all": total_all,
        "recent": recent,
    })


# ─── Queue ────────────────────────────────────────────────

@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    queued_articles = db.query(Article).filter(
        Article.user_id == user.id, Article.status == "queued"
    ).order_by(Article.id.asc()).all()

    return templates.TemplateResponse("queue.html", {
        "request": request,
        "user": user,
        "queued_articles": queued_articles,
    })


# ─── Create Single Article ───────────────────────────────

@app.get("/create", response_class=HTMLResponse)
async def create_form(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    return templates.TemplateResponse("create.html", {"request": request, "user": user})


@app.post("/create")
async def create_article(
    request: Request,
    competitor_content: str = Form(""),
    notes: str = Form(""),
    screenshot: UploadFile = File(None),
    featured_image: UploadFile = File(None),
    action: str = Form("now"),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)

    screenshot_path = ""
    if screenshot and screenshot.filename:
        screenshot_path = save_upload(screenshot)

    featured_path = ""
    if featured_image and featured_image.filename:
        featured_path = save_upload(featured_image)

    status = "pending" if action == "now" else "queued"

    article = Article(
        user_id=user.id,
        competitor_content=competitor_content,
        notes=notes,
        screenshot_path=screenshot_path,
        featured_image_path=featured_path,
        wp_category="Health",
        status=status,
    )
    db.add(article)
    db.commit()

    return RedirectResponse(url="/", status_code=303)


# ─── Preview ──────────────────────────────────────────────

@app.get("/preview/{article_id}", response_class=HTMLResponse)
async def preview(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if not article:
        return RedirectResponse(url="/")

    stock_urls = []
    try:
        stock_urls = json.loads(article.stock_image_urls) if article.stock_image_urls else []
    except Exception:
        pass

    return templates.TemplateResponse("preview.html", {
        "request": request,
        "user": user,
        "article": article,
        "stock_urls": stock_urls,
    })


@app.post("/preview/{article_id}/publish")
async def publish_from_preview(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if article and article.status in ("preview", "ready", "failed"):
        article.status = "pending"
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/preview/{article_id}/regenerate")
async def regenerate(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if article:
        article.status = "pending"
        db.commit()
    return RedirectResponse(url=f"/preview/{article_id}", status_code=303)


@app.post("/article/{article_id}/delete")
async def delete_article(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if article:
        db.delete(article)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ─── API: Realtime Article Status ────────────────────────

@app.get("/api/articles-status")
async def articles_status(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    base = db.query(Article).filter(Article.user_id == user.id)

    published_today = base.filter(
        Article.status == "published", Article.published_at >= today_start
    ).count()
    failed_today = base.filter(
        Article.status == "failed", Article.created_at >= today_start
    ).count()
    total_all = base.filter(Article.status == "published").count()

    processing = base.filter(
        Article.status.in_(["pending", "generating", "images", "uploading", "ready", "publishing"])
    ).order_by(Article.id.asc()).all()

    queued = base.filter(Article.status == "queued").order_by(Article.id.asc()).all()

    done = base.filter(
        Article.status.in_(["published", "failed"])
    ).order_by(Article.created_at.desc()).limit(30).all()

    def serialize(a):
        return {
            "id": a.id,
            "title": a.title or "(Chưa tạo)",
            "status": a.status,
            "word_count": a.word_count,
            "wp_post_url": a.wp_post_url or "",
            "error_message": a.error_message or "",
            "created_at": a.created_at.strftime("%H:%M %d/%m") if a.created_at else "",
        }

    return JSONResponse({
        "stats": {
            "published_today": published_today,
            "failed_today": failed_today,
            "total_all": total_all,
            "processing_count": len(processing),
        },
        "processing": [serialize(a) for a in processing],
        "queued": [serialize(a) for a in queued],
        "done": [serialize(a) for a in done],
    })


@app.post("/api/run-queued")
async def api_run_queued(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    data = await request.json()
    ids = data.get("ids", [])
    # Only allow running own queued articles
    if ids:
        own_ids = [a.id for a in db.query(Article).filter(
            Article.id.in_(ids), Article.user_id == user.id, Article.status == "queued"
        ).all()]
        if own_ids:
            await run_queued(own_ids, SessionLocal)
    return JSONResponse({"ok": True})


# ─── Settings ─────────────────────────────────────────────

def _read_env() -> dict:
    from app.config import BASE_DIR
    env_path = BASE_DIR / ".env"
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def _write_env(values: dict):
    from app.config import BASE_DIR
    env_path = BASE_DIR / ".env"
    lines = [f"{k}={v}" for k, v in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    from app.config import PROMPT_FILE
    default_prompt = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""
    env = _read_env() if user.role == "admin" else {}

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "env": env,
        "default_prompt": default_prompt,
    })


@app.post("/api/save-settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    data = await request.json()

    # User settings: WP credentials + custom prompt
    if "wp_url" in data:
        user.wp_url = (data["wp_url"] or "").strip().rstrip("/")
    if "wp_username" in data:
        user.wp_username = (data["wp_username"] or "").strip()
    if "wp_password" in data:
        user.wp_app_password = (data["wp_password"] or "").strip()
    if "custom_prompt" in data:
        user.custom_prompt = (data["custom_prompt"] or "").strip() or None
    db.commit()

    # Admin-only: global API keys
    if user.role == "admin":
        import app.config as cfg
        env = _read_env()
        admin_fields = {
            "openai_key": "OPENAI_API_KEY",
            "gpt_model": "GPT_MODEL",
            "pexels_key": "PEXELS_API_KEY",
        }
        changed = False
        for field, env_key in admin_fields.items():
            if field in data and data[field].strip():
                env[env_key] = data[field].strip()
                changed = True

        if changed:
            if "SECRET_KEY" not in env:
                env["SECRET_KEY"] = cfg.SECRET_KEY
            if "UPLOAD_DIR" not in env:
                env["UPLOAD_DIR"] = "uploads"
            _write_env(env)

            cfg.OPENAI_API_KEY = env.get("OPENAI_API_KEY", "")
            cfg.GPT_MODEL = env.get("GPT_MODEL", "gpt-4o-mini")
            cfg.PEXELS_API_KEY = env.get("PEXELS_API_KEY", "")

            from app.services import gpt_generator, image_search
            gpt_generator.client = gpt_generator.AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
            image_search.PEXELS_API_KEY = cfg.PEXELS_API_KEY

    return JSONResponse({"ok": True, "message": "Đã lưu cài đặt"})


@app.get("/api/test-wp")
async def test_wp(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user.wp_url or not user.wp_username or not user.wp_app_password:
        return JSONResponse({"ok": False, "error": "Chưa cấu hình WordPress"})
    # Test with user's own WP credentials
    import base64, httpx
    token = base64.b64encode(f"{user.wp_username}:{user.wp_app_password}".encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{user.wp_url}/wp-json/wp/v2/users/me",
                headers={"Authorization": f"Basic {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            wp_user = resp.json()
        return JSONResponse({"ok": True, "user": wp_user.get("name", ""), "url": user.wp_url})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ─── Admin: User Management ─────────────────────────────

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    users = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "user": user,
        "users": users,
    })


@app.post("/admin/users/add")
async def admin_add_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("user"),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    if not username.strip() or not password.strip():
        return RedirectResponse(url="/admin/users", status_code=303)
    existing = db.query(User).filter(User.username == username.strip()).first()
    if existing:
        return RedirectResponse(url="/admin/users", status_code=303)
    new_user = User(
        username=username.strip(),
        password_hash=hash_password(password.strip()),
        role=role if role in ("admin", "user") else "user",
    )
    db.add(new_user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/delete")
async def admin_delete_user(request: Request, uid: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin" or uid == user.id:
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.query(User).get(uid)
    if target:
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/role")
async def admin_change_role(request: Request, uid: int, role: str = Form("user"), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin" or uid == user.id:
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.query(User).get(uid)
    if target and role in ("admin", "user"):
        target.role = role
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/reset-password")
async def admin_reset_password(request: Request, uid: int, password: str = Form(""), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.query(User).get(uid)
    if target and password.strip():
        target.password_hash = hash_password(password.strip())
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)

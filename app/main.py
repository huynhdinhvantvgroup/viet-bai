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
from app.models import init_db, get_db, Article, User, AdsenseToken, DailyStat, SystemConfig, SessionLocal
from app.auth import (
    hash_password, verify_password,
    create_session_cookie, get_current_user,
    COOKIE_NAME, login_required_middleware, ensure_admin_exists,
)
from app.services.batch_runner import (
    parse_csv, enqueue_article, run_queued, start_worker,
)

app = FastAPI(title="Health Content Writer")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ─── Timezone & Helpers ──────────────────────────────────
import logging
from datetime import datetime, timedelta, timezone

TZ_VN = timezone(timedelta(hours=7))
TZ_UTC = timezone.utc

logger = logging.getLogger("health-writer")

def _to_vn(dt, fmt="%H:%M %d/%m"):
    if not dt:
        return ""
    return dt.replace(tzinfo=TZ_UTC).astimezone(TZ_VN).strftime(fmt) if dt.tzinfo is None else dt.astimezone(TZ_VN).strftime(fmt)

def _today_start():
    """Return start of today (00:00) in UTC."""
    now = datetime.now(TZ_VN)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(TZ_UTC)

def _month_start():
    """Return start of this month (day 1, 00:00) in UTC."""
    now = datetime.now(TZ_VN)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(TZ_UTC)

templates.env.filters["vn_time"] = _to_vn

# Auth middleware — must be added before routes
login_required_middleware(app)


@app.on_event("startup")
async def startup():
    init_db()
    db = SessionLocal()
    ensure_admin_exists(db)
    db.close()
    await start_worker(SessionLocal)  # Only starts if Redis is NOT available


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
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {
            "error": "Sai tên đăng nhập hoặc mật khẩu"
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
    today_s = _today_start()

    base = db.query(Article).filter(Article.user_id == user.id)

    published_today = base.filter(
        Article.status == "published", Article.published_at >= today_s
    ).count()
    failed_today = base.filter(
        Article.status == "failed", Article.created_at >= today_s
    ).count()
    processing = base.filter(
        Article.status.in_(["pending", "generating", "images", "uploading", "publishing", "deleting"])
    ).count()
    total_all = base.filter(Article.status == "published").count()

    per_page = 20
    page = int(request.query_params.get("page", 1))
    if page < 1:
        page = 1
    total_articles = base.count()
    total_pages = max(1, (total_articles + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    recent = base.order_by(Article.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    now = datetime.now(TZ_VN)
    days_vi = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    now_vn = f"{days_vi[now.weekday()]}, {now.strftime('%d/%m/%Y')}"

    from app.config import SLOGAN_FILE
    slogan = SLOGAN_FILE.read_text(encoding="utf-8").strip() if SLOGAN_FILE.exists() else ""

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "published_today": published_today,
        "failed_today": failed_today,
        "processing_count": processing,
        "total_all": total_all,
        "recent": recent,
        "page": page,
        "total_pages": total_pages,
        "total_articles": total_articles,
        "now_vn": now_vn,
        "slogan": slogan,
    })


# ─── Queue ────────────────────────────────────────────────

@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    queued_articles = db.query(Article).filter(
        Article.user_id == user.id, Article.status == "queued"
    ).order_by(Article.id.asc()).all()

    return templates.TemplateResponse(request, "queue.html", {
        "user": user,
        "queued_articles": queued_articles,
    })


# ─── Create Single Article ───────────────────────────────

@app.get("/create", response_class=HTMLResponse)
async def create_form(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    return templates.TemplateResponse(request, "create.html", {"user": user})


@app.post("/create")
async def create_article(
    request: Request,
    competitor_content: str = Form(""),
    caption: str = Form(""),
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
        caption=caption,
        screenshot_path=screenshot_path,
        featured_image_path=featured_path,
        wp_category="Health",
        status=status,
    )
    db.add(article)
    db.commit()

    if status == "pending":
        await enqueue_article(article.id)

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

    return templates.TemplateResponse(request, "preview.html", {
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
        await enqueue_article(article.id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/preview/{article_id}/regenerate")
async def regenerate(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if article:
        article.status = "pending"
        db.commit()
        await enqueue_article(article.id)
    return RedirectResponse(url=f"/preview/{article_id}", status_code=303)


@app.post("/article/{article_id}/delete")
async def delete_article(request: Request, article_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if article:
        if article.status in ("generating", "images", "uploading", "publishing"):
            article.status = "deleting"
            db.commit()
        else:
            db.delete(article)
            db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/article/{article_id}/download-image")
async def download_featured_image(request: Request, article_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    user = _get_user(request, db)
    article = db.query(Article).filter(Article.id == article_id, Article.user_id == user.id).first()
    if not article or not article.featured_image_path:
        return RedirectResponse(url="/")
    file_path = Path(article.featured_image_path)
    if not file_path.exists():
        return JSONResponse({"error": "File không tồn tại"}, status_code=404)
    return FileResponse(file_path, filename=f"featured-{article_id}{file_path.suffix}", media_type="application/octet-stream")


# ─── API: Realtime Article Status ────────────────────────

@app.get("/api/articles-status")
async def articles_status(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    today_s = _today_start()

    base = db.query(Article).filter(Article.user_id == user.id)

    published_today = base.filter(
        Article.status == "published", Article.published_at >= today_s
    ).count()
    failed_today = base.filter(
        Article.status == "failed", Article.created_at >= today_s
    ).count()
    total_all = base.filter(Article.status == "published").count()

    processing = base.filter(
        Article.status.in_(["pending", "generating", "images", "uploading", "ready", "publishing", "deleting"])
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
            "wp_post_id": a.wp_post_id,
            "error_message": a.error_message or "",
            "caption": a.caption or "",
            "has_featured_image": bool(a.featured_image_path),
            "image_count": (a.content or "").count("<img") if a.status in ("published", "failed") else None,
            "created_at": _to_vn(a.created_at),
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
    from app.config import PROMPT_FILE, SLOGAN_FILE
    default_prompt = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""
    slogan = SLOGAN_FILE.read_text(encoding="utf-8").strip() if SLOGAN_FILE.exists() else ""
    env = _read_env() if user.role == "admin" else {}

    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "env": env,
        "default_prompt": default_prompt,
        "slogan": slogan,
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
            "gemini_key": "GEMINI_API_KEY",
            "gpt_model": "GPT_MODEL",
            "pexels_key": "PEXELS_API_KEY",
            "google_client_id": "GOOGLE_CLIENT_ID",
            "google_client_secret": "GOOGLE_CLIENT_SECRET",
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
            cfg.GEMINI_API_KEY = env.get("GEMINI_API_KEY", "")
            cfg.GPT_MODEL = env.get("GPT_MODEL", "gpt-5.4-mini")
            cfg.PEXELS_API_KEY = env.get("PEXELS_API_KEY", "")
            cfg.GOOGLE_CLIENT_ID = env.get("GOOGLE_CLIENT_ID", "")
            cfg.GOOGLE_CLIENT_SECRET = env.get("GOOGLE_CLIENT_SECRET", "")

            from app.services import gpt_generator, image_search
            gpt_generator.client = gpt_generator.AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)
            gpt_generator.GEMINI_API_KEY = cfg.GEMINI_API_KEY
            image_search.PEXELS_API_KEY = cfg.PEXELS_API_KEY

        # Save slogan
        if "slogan" in data:
            from app.config import SLOGAN_FILE
            SLOGAN_FILE.write_text(data["slogan"].strip(), encoding="utf-8")

        # Save default prompt to prompt.txt
        if "default_prompt" in data and data["default_prompt"].strip():
            from app.config import PROMPT_FILE
            PROMPT_FILE.write_text(data["default_prompt"], encoding="utf-8")

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

    today_s = _today_start()
    month_s = _month_start()

    user_stats = {}
    for u in users:
        posts_day = db.query(Article).filter(
            Article.user_id == u.id, Article.status == "published",
            Article.published_at >= today_s,
        ).count()
        posts_month = db.query(Article).filter(
            Article.user_id == u.id, Article.status == "published",
            Article.published_at >= month_s,
        ).count()
        user_stats[u.id] = {"posts_day": posts_day, "posts_month": posts_month}

    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user,
        "users": users,
        "user_stats": user_stats,
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
    target = db.get(User, uid)
    if target:
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/role")
async def admin_change_role(request: Request, uid: int, role: str = Form("user"), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin" or uid == user.id:
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.get(User, uid)
    if target and role in ("admin", "user"):
        target.role = role
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/reset-password")
async def admin_reset_password(request: Request, uid: int, password: str = Form(""), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.get(User, uid)
    if target and password.strip():
        target.password_hash = hash_password(password.strip())
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{uid}/update-info")
async def admin_update_user_info(
    request: Request, uid: int,
    username: str = Form(""),
    role: str = Form(""),
    subdomain: str = Form(""),
    monthly_kpi: float = Form(0),
    employee_name: str = Form(""),
    position: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/admin/users", status_code=303)
    target = db.get(User, uid)
    if target:
        new_name = username.strip()
        if new_name and new_name != target.username:
            dup = db.query(User).filter(User.username == new_name, User.id != uid).first()
            if not dup:
                target.username = new_name
        if role in ("user", "admin") and target.id != user.id:
            target.role = role
        target.subdomain = subdomain.strip()
        target.monthly_kpi = monthly_kpi
        target.employee_name = employee_name.strip()
        target.position = position.strip()
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


# ─── Admin: Bonus Config ────────────────────────────────

@app.post("/admin/bonus/config")
async def admin_save_config(request: Request, conversion_rate: float = Form(0.35), usd_vnd_rate: float = Form(26000), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    for key, val in [("conversion_rate", str(conversion_rate)), ("usd_vnd_rate", str(usd_vnd_rate))]:
        row = db.get(SystemConfig, key)
        if row:
            row.value = val
        else:
            db.add(SystemConfig(key=key, value=val))
    db.commit()
    return RedirectResponse(url="/boss", status_code=303)


@app.post("/admin/bonus/tiers")
async def admin_save_tiers(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    form = await request.form()
    tiers = []
    i = 0
    while f"min_{i}" in form:
        tiers.append({
            "min_usd": float(form.get(f"min_{i}", 0)),
            "max_usd": float(form.get(f"max_{i}", 0)),
            "bonus_rate": float(form.get(f"rate_{i}", 0)),
        })
        i += 1
    row = db.get(SystemConfig, "bonus_tiers")
    if row:
        row.value = json.dumps(tiers)
    else:
        db.add(SystemConfig(key="bonus_tiers", value=json.dumps(tiers)))
    db.commit()
    return RedirectResponse(url="/boss", status_code=303)


@app.post("/api/admin/sync-adsense")
async def admin_sync_adsense(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        from app.services.adsense_sync import sync_daily_stats
        await sync_daily_stats()
        return JSONResponse({"message": "Đã cập nhật dữ liệu!"})
    except Exception as e:
        return JSONResponse({"error": str(e), "message": f"Loi: {e}"})


# ─── AdSense OAuth + Report (Admin only) ────────────────

@app.get("/adsense", response_class=HTMLResponse)
async def adsense_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    import app.config as cfg
    tokens = db.query(AdsenseToken).all()
    has_credentials = bool(cfg.GOOGLE_CLIENT_ID and cfg.GOOGLE_CLIENT_SECRET)
    return templates.TemplateResponse(request, "adsense.html", {
        "user": user,
        "tokens": tokens,
        "has_credentials": has_credentials,
    })


@app.get("/adsense/connect")
async def adsense_connect(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    import app.config as cfg
    if not cfg.GOOGLE_CLIENT_ID or not cfg.GOOGLE_CLIENT_SECRET:
        return RedirectResponse(url="/adsense")
    from app.services.adsense_service import GOOGLE_AUTH_URL, SCOPES
    redirect_uri = str(request.url_for("adsense_callback"))
    params = (
        f"client_id={cfg.GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(url=f"{GOOGLE_AUTH_URL}?{params}")


@app.get("/adsense/callback")
async def adsense_callback(request: Request, code: str = "", error: str = "", db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/")
    if error or not code:
        return RedirectResponse(url="/adsense")
    import app.config as cfg
    from app.services.adsense_service import exchange_code, get_adsense_account
    redirect_uri = str(request.url_for("adsense_callback"))
    try:
        token_data = await exchange_code(code, cfg.GOOGLE_CLIENT_ID, cfg.GOOGLE_CLIENT_SECRET, redirect_uri)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 3600)
        account = await get_adsense_account(access_token)
        account_id = account.get("name", "")
        account_name = account.get("displayName", account_id)
        existing = db.query(AdsenseToken).filter(AdsenseToken.account_id == account_id).first()
        if existing:
            existing.access_token = access_token
            existing.refresh_token = refresh_token or existing.refresh_token
            existing.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            existing.account_name = account_name
        else:
            new_token = AdsenseToken(
                account_name=account_name,
                account_id=account_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            )
            db.add(new_token)
        db.commit()
    except Exception:
        pass
    return RedirectResponse(url="/adsense")


@app.post("/adsense/disconnect/{token_id}")
async def adsense_disconnect(request: Request, token_id: int, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return RedirectResponse(url="/adsense", status_code=303)
    token = db.get(AdsenseToken, token_id)
    if token:
        db.delete(token)
        db.commit()
    return RedirectResponse(url="/adsense", status_code=303)


@app.post("/api/adsense-nickname")
async def adsense_nickname(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user.role != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    token = db.get(AdsenseToken, data.get("token_id"))
    if token:
        token.nickname = data.get("nickname", "")
        db.commit()
    return JSONResponse({"ok": True})


@app.get("/api/adsense-report")
async def adsense_report_api(request: Request, period: str = "7d", db: Session = Depends(get_db)):
    user = _get_user(request, db)
    # Non-admin can only see their own subdomain
    if user.role != "admin" and not user.subdomain:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    import app.config as cfg
    from app.services.adsense_service import refresh_access_token, get_report
    from datetime import timezone
    tz7 = timezone(timedelta(hours=7))
    today = datetime.now(tz7).date()
    if period == "today":
        start = today
    elif period == "30d":
        start = today - timedelta(days=30)
    elif period == "month":
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=7)
    end = today

    tokens = db.query(AdsenseToken).all()
    if not tokens:
        return JSONResponse({"rows": [], "total": 0, "users": {}})

    all_rows = []
    for tk in tokens:
        try:
            # Refresh token if expired (compare as naive UTC)
            exp = tk.expires_at.replace(tzinfo=None) if tk.expires_at and tk.expires_at.tzinfo else tk.expires_at
            now_utc = datetime.utcnow()
            if not exp or exp < now_utc:
                new_data = await refresh_access_token(tk.refresh_token, cfg.GOOGLE_CLIENT_ID, cfg.GOOGLE_CLIENT_SECRET)
                tk.access_token = new_data["access_token"]
                tk.expires_at = now_utc + timedelta(seconds=new_data.get("expires_in", 3600))
                db.commit()
            rows, _ = await get_report(tk.access_token, tk.account_id, str(start), str(end))
            all_rows.extend(rows)
        except Exception as e:
            logger.exception("AdSense report error for token %s", tk.id)
            continue

    # Merge duplicates by domain
    merged = {}
    for r in all_rows:
        d = r.get("domain", "")
        if d in merged:
            merged[d]["earnings"] += r.get("earnings", 0)
            merged[d]["clicks"] += r.get("clicks", 0)
            merged[d]["impressions"] += r.get("impressions", 0)
            merged[d]["pageviews"] += r.get("pageviews", 0)
        else:
            merged[d] = {
                "domain": d,
                "earnings": r.get("earnings", 0),
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "pageviews": r.get("pageviews", 0),
            }

    rows_list = sorted(merged.values(), key=lambda x: x["earnings"], reverse=True)
    # Non-admin: filter to only their subdomain
    if user.role != "admin":
        rows_list = [r for r in rows_list if r["domain"] == user.subdomain]
    total = sum(r["earnings"] for r in rows_list)

    # Map subdomain → user
    users = db.query(User).filter(User.subdomain != "").all()
    user_map = {}
    for u in users:
        user_map[u.subdomain] = {"username": u.username, "kpi": u.monthly_kpi}

    return JSONResponse({
        "rows": rows_list,
        "total": round(total, 2),
        "users": user_map,
        "period": period,
    })


# ─── API: Top Earner ──────────────────────────────────

@app.get("/api/top-earner")
async def api_top_earner(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    try:
        from sqlalchemy import func, extract

        tz7 = timezone(timedelta(hours=7))
        now = datetime.now(tz7)
        year, mon = now.year, now.month

        # Query daily_stats grouped by account_id
        stats = db.query(
            DailyStat.account_id,
            func.sum(DailyStat.revenue_usd).label("total_rev"),
        ).filter(
            extract("year", DailyStat.date) == year,
            extract("month", DailyStat.date) == mon,
        ).group_by(DailyStat.account_id).all()

        # Map account_id → user, group by employee_name
        user_map = {u.id: u for u in db.query(User).all()}
        emp_earnings = {}
        for s in stats:
            u = user_map.get(s.account_id)
            if not u:
                continue
            name = u.employee_name or u.username
            emp_earnings[name] = emp_earnings.get(name, 0) + float(s.total_rev or 0)

        ranking = [{"name": k, "earnings": v} for k, v in emp_earnings.items()]
        ranking.sort(key=lambda x: x["earnings"], reverse=True)

        top = ranking[0] if ranking and ranking[0]["earnings"] > 0 else None
        my_name = user.employee_name or user.username
        my_rank = None
        for i, r in enumerate(ranking):
            if r["name"] == my_name:
                my_rank = i + 1
                break

        return JSONResponse({
            "top": top["name"] if top else None,
            "rank": my_rank,
            "total_users": len(ranking),
        })
    except Exception:
        return JSONResponse({"top": None, "rank": None})


# ─── API: Employee Bonus Dashboard ───────────────────────

def _get_bonus_config(db):
    """Load bonus tiers + system config from DB."""
    tiers_row = db.get(SystemConfig, "bonus_tiers")
    conv_row = db.get(SystemConfig, "conversion_rate")
    rate_row = db.get(SystemConfig, "usd_vnd_rate")
    tiers = json.loads(tiers_row.value) if tiers_row else []
    conversion_rate = float(conv_row.value) if conv_row else 0.35
    usd_vnd_rate = float(rate_row.value) if rate_row else 26000
    return tiers, conversion_rate, usd_vnd_rate


def _lookup_bonus_tier(tiers, total_usd):
    """Find bonus rate for given USD amount."""
    if not tiers:
        return 0
    for t in tiers:
        if t["min_usd"] <= total_usd <= t["max_usd"]:
            return t["bonus_rate"]
    if total_usd > tiers[-1]["max_usd"]:
        return tiers[-1]["bonus_rate"]
    return 0


def _get_employee_accounts(user, db):
    """Get all accounts for this user's employee group (same employee_name)."""
    if user.employee_name:
        return db.query(User).filter(User.employee_name == user.employee_name).all()
    return [user]


@app.get("/api/employee-bonus")
async def api_employee_bonus(request: Request, month: str = "", db: Session = Depends(get_db)):
    """Return bonus dashboard data for the logged-in employee."""
    user = _get_user(request, db)

    tz7 = timezone(timedelta(hours=7))
    now = datetime.now(tz7)

    # Parse month param (YYYY-MM) or default to current month
    if month:
        try:
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
        except (ValueError, IndexError):
            year, mon = now.year, now.month
    else:
        year, mon = now.year, now.month

    is_current_month = (year == now.year and mon == now.month)

    # Get all accounts for this employee
    accounts = _get_employee_accounts(user, db)
    account_ids = [a.id for a in accounts]

    # Get employee name
    employee_name = user.employee_name or user.username

    # Query daily_stats for the month
    from sqlalchemy import func, extract
    stats = db.query(
        func.sum(DailyStat.page_views).label("total_views"),
        func.sum(DailyStat.revenue_usd).label("total_revenue"),
    ).filter(
        DailyStat.account_id.in_(account_ids),
        extract("year", DailyStat.date) == year,
        extract("month", DailyStat.date) == mon,
    ).first()

    total_views = int(stats.total_views or 0)
    total_revenue = float(stats.total_revenue or 0)

    # RPM average
    rpm_stats = db.query(
        func.sum(DailyStat.rpm * DailyStat.page_views).label("sum_rpm_views"),
        func.sum(DailyStat.page_views).label("sum_views"),
    ).filter(
        DailyStat.account_id.in_(account_ids),
        extract("year", DailyStat.date) == year,
        extract("month", DailyStat.date) == mon,
    ).first()

    sum_rpm_views = float(rpm_stats.sum_rpm_views or 0)
    sum_views = int(rpm_stats.sum_views or 0)
    avg_rpm = sum_rpm_views / sum_views if sum_views > 0 else 0

    # Load config
    tiers, conversion_rate, usd_vnd_rate = _get_bonus_config(db)

    # Calculate he_so (market coefficient) - visible to employee
    he_so = round(avg_rpm * conversion_rate, 2)

    # Calculate bonus
    rate_vnd = usd_vnd_rate * conversion_rate  # 26000 * 0.35 = 9100
    gia_tri = total_revenue * rate_vnd
    bonus_rate = _lookup_bonus_tier(tiers, total_revenue)
    thuong_vnd = int(gia_tri * bonus_rate)

    # Basic requirement status
    basic_met = total_revenue >= 1000
    basic_progress = min(total_revenue / 1000, 1.0) if not basic_met else 1.0

    # Milestones (views-based, for employee display)
    moc_views = []
    for t in tiers:
        if t["min_usd"] > 0:
            views_needed = int(t["min_usd"] * 1000 / avg_rpm) if avg_rpm > 0 else 0
            thuong_min = int(t["min_usd"] * rate_vnd * t["bonus_rate"])
            moc_views.append({
                "views": views_needed,
                "thuong": thuong_min,
                "dat": total_views >= views_needed,
            })

    return JSONResponse({
        "employee_name": employee_name,
        "he_so": he_so,
        "total_views": total_views,
        "total_revenue_usd": round(total_revenue, 2),
        "thuong_vnd": thuong_vnd,
        "basic_met": basic_met,
        "basic_progress": round(basic_progress * 100, 1),
        "moc_views": moc_views,
        "month": f"{year}-{mon:02d}",
        "is_current_month": is_current_month,
    })


@app.get("/api/employee-daily")
async def api_employee_daily(request: Request, month: str = "", db: Session = Depends(get_db)):
    """Return daily views breakdown for chart."""
    user = _get_user(request, db)

    tz7 = timezone(timedelta(hours=7))
    now = datetime.now(tz7)

    if month:
        try:
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
        except (ValueError, IndexError):
            year, mon = now.year, now.month
    else:
        year, mon = now.year, now.month

    accounts = _get_employee_accounts(user, db)
    account_ids = [a.id for a in accounts]

    from sqlalchemy import func, extract
    rows = db.query(
        DailyStat.date,
        func.sum(DailyStat.page_views).label("views"),
    ).filter(
        DailyStat.account_id.in_(account_ids),
        extract("year", DailyStat.date) == year,
        extract("month", DailyStat.date) == mon,
    ).group_by(DailyStat.date).order_by(DailyStat.date).all()

    daily = [{"date": str(r.date), "views": int(r.views)} for r in rows]

    total_views = sum(d["views"] for d in daily)
    avg_daily = int(total_views / len(daily)) if daily else 0

    best = max(daily, key=lambda x: x["views"]) if daily else None
    worst = min(daily, key=lambda x: x["views"]) if daily else None

    # Thưởng dự kiến (same calc as bonus endpoint)
    tiers, conversion_rate, usd_vnd_rate = _get_bonus_config(db)

    stats = db.query(
        func.sum(DailyStat.revenue_usd).label("total_revenue"),
    ).filter(
        DailyStat.account_id.in_(account_ids),
        extract("year", DailyStat.date) == year,
        extract("month", DailyStat.date) == mon,
    ).first()
    total_revenue = float(stats.total_revenue or 0)
    rate_vnd = usd_vnd_rate * conversion_rate
    bonus_rate = _lookup_bonus_tier(tiers, total_revenue)
    thuong_vnd = int(total_revenue * rate_vnd * bonus_rate)

    return JSONResponse({
        "daily": daily,
        "total_views": total_views,
        "avg_daily": avg_daily,
        "thuong_vnd": thuong_vnd,
        "best": best,
        "worst": worst,
        "month": f"{year}-{mon:02d}",
    })


# ─── Boss Dashboard ──────────────────────────────────────

@app.get("/boss", response_class=HTMLResponse)
async def boss_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = _get_user(request, db)
        if user.role != "admin":
            return RedirectResponse(url="/")
        tiers, conversion_rate, usd_vnd_rate = _get_bonus_config(db)
        return templates.TemplateResponse(request, "boss.html", {
            "user": user,
            "tiers": tiers,
            "config": {"conversion_rate": conversion_rate, "usd_vnd_rate": usd_vnd_rate},
        })
    except Exception as e:
        import traceback
        logger.error("Boss page error: %s", traceback.format_exc())
        return HTMLResponse(f"<pre>{traceback.format_exc()}</pre>", status_code=500)


@app.get("/api/boss-overview")
async def api_boss_overview(request: Request, month: str = "", db: Session = Depends(get_db)):
    """Return full overview for boss: all employees, revenue, bonus."""
    try:
        user = _get_user(request, db)
    except Exception:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    if user.role != "admin":
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    tz7 = timezone(timedelta(hours=7))
    now = datetime.now(tz7)
    if month:
        try:
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
        except (ValueError, IndexError):
            year, mon = now.year, now.month
    else:
        year, mon = now.year, now.month

    tiers, conversion_rate, usd_vnd_rate = _get_bonus_config(db)
    rate_vnd = usd_vnd_rate * conversion_rate

    from sqlalchemy import func, extract

    # Group users by employee_name
    all_users = db.query(User).filter(User.employee_name != "", User.employee_name != None).all()
    groups = {}
    for u in all_users:
        groups.setdefault(u.employee_name, []).append(u)

    result = []
    for emp_name, accounts in groups.items():
        account_ids = [a.id for a in accounts]

        stats = db.query(
            func.sum(DailyStat.page_views).label("views"),
            func.sum(DailyStat.revenue_usd).label("revenue"),
            func.sum(DailyStat.rpm * DailyStat.page_views).label("rpm_views"),
            func.sum(DailyStat.page_views).label("sum_views"),
        ).filter(
            DailyStat.account_id.in_(account_ids),
            extract("year", DailyStat.date) == year,
            extract("month", DailyStat.date) == mon,
        ).first()

        views = int(stats.views or 0)
        revenue = float(stats.revenue or 0)
        sum_views = int(stats.sum_views or 0)
        rpm_views = float(stats.rpm_views or 0)
        avg_rpm = rpm_views / sum_views if sum_views > 0 else 0
        he_so = round(avg_rpm * conversion_rate, 2)
        bonus_rate = _lookup_bonus_tier(tiers, revenue)
        thuong_vnd = int(revenue * rate_vnd * bonus_rate)

        # Find tier index (bậc)
        tier_idx = 0
        for i, t in enumerate(tiers):
            if t["min_usd"] <= revenue <= t["max_usd"]:
                tier_idx = i
                break
            if revenue > t["max_usd"]:
                tier_idx = i

        # Progress toward next tier
        basic_met = revenue >= 1000
        if tier_idx < len(tiers) - 1:
            next_min = tiers[tier_idx + 1]["min_usd"] if not basic_met else tiers[tier_idx + 1]["min_usd"]
            progress = min(revenue / next_min * 100, 100) if next_min > 0 else 100
        else:
            progress = 100

        result.append({
            "name": emp_name,
            "accounts": [{"username": a.username} for a in accounts],
            "views": views,
            "avg_rpm": round(avg_rpm, 2),
            "revenue_usd": round(revenue, 2),
            "tier_idx": tier_idx,
            "bonus_rate": bonus_rate,
            "thuong_vnd": thuong_vnd,
            "basic_met": basic_met,
            "progress": round(progress, 1),
        })

    total_revenue = sum(r["revenue_usd"] for r in result)
    total_thuong = sum(r["thuong_vnd"] for r in result)
    doanh_thu_vnd = int(total_revenue * usd_vnd_rate)

    return JSONResponse({
        "employees": sorted(result, key=lambda x: x["revenue_usd"], reverse=True),
        "total_revenue": round(total_revenue, 2),
        "total_thuong": total_thuong,
        "doanh_thu_vnd": doanh_thu_vnd,
        "tiers": tiers,
        "month": f"{year}-{mon:02d}",
    })


# ─── Employee Profile ──────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    from datetime import datetime, timezone, timedelta
    tz7 = timezone(timedelta(hours=7))
    now = datetime.now(tz7)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    base = db.query(Article).filter(Article.user_id == user.id, Article.status == "published")
    posts_today = base.filter(Article.published_at >= today_start).count()
    posts_month = base.filter(Article.published_at >= month_start).count()
    posts_total = base.count()

    accounts = _get_employee_accounts(user, db)

    return templates.TemplateResponse(request, "profile.html", {
        "user": user,
        "accounts": accounts,
        "posts_today": posts_today,
        "posts_month": posts_month,
        "posts_total": posts_total,
    })

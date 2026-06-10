import bcrypt
from itsdangerous import URLSafeSerializer
from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import SECRET_KEY
from app.models import get_db, User

_signer = URLSafeSerializer(SECRET_KEY, salt="session")

COOKIE_NAME = "session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_session_cookie(user_id: int) -> str:
    return _signer.dumps({"uid": user_id})


def get_user_id_from_cookie(cookie: str) -> int | None:
    try:
        data = _signer.loads(cookie)
        return data.get("uid")
    except Exception:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    uid = get_user_id_from_cookie(cookie)
    if not uid:
        return None
    return db.get(User, uid)


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise _LoginRequired()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_login(request, db)
    if user.role != "admin":
        raise _LoginRequired()
    return user


class _LoginRequired(Exception):
    pass


def login_required_middleware(app):
    """Middleware that redirects to /login if not authenticated."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Public paths
            public = ["/login", "/static/", "/favicon.ico"]
            if any(request.url.path.startswith(p) for p in public):
                return await call_next(request)

            cookie = request.cookies.get(COOKIE_NAME)
            if not cookie or not get_user_id_from_cookie(cookie):
                return RedirectResponse(url="/login", status_code=303)

            try:
                response = await call_next(request)
            except _LoginRequired:
                return RedirectResponse(url="/login", status_code=303)
            return response

    app.add_middleware(AuthMiddleware)


def ensure_admin_exists(db: Session):
    """Create default admin if no users exist."""
    if db.query(User).count() == 0:
        admin = User(
            username="admin",
            password_hash=hash_password("admin123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        print("[AUTH] Created default admin: admin / admin123")

from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, Float, String, Text, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from app.config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
_pool_kwargs = {} if _is_sqlite else {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, **_pool_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(20), default="user")  # admin | user
    # Per-user WordPress
    wp_url = Column(String(500), default="")
    wp_username = Column(String(200), default="")
    wp_app_password = Column(String(200), default="")
    # Per-user prompt (NULL = use default prompt.txt)
    custom_prompt = Column(Text, nullable=True)
    # AdSense / KPI
    subdomain = Column(String(500), default="")
    monthly_kpi = Column(Float, default=0)
    # Employee grouping
    employee_name = Column(String(100), default="")
    position = Column(String(100), default="")
    # Meta
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    articles = relationship("Article", back_populates="owner")


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(String(20), default="pending", index=True)
    # Input
    competitor_content = Column(Text, default="")
    notes = Column(Text, default="")
    caption = Column(Text, default="")
    screenshot_path = Column(String(500), default="")
    featured_image_path = Column(String(500), default="")
    # Generated
    title = Column(String(500), default="")
    content = Column(Text, default="")
    stock_image_urls = Column(Text, default="")
    stock_image_keywords = Column(String(500), default="")
    # WordPress
    wp_post_id = Column(Integer, nullable=True)
    wp_post_url = Column(String(500), default="")
    wp_featured_media_id = Column(Integer, nullable=True)
    wp_category = Column(String(100), default="Health")
    wp_tags = Column(String(500), default="")
    scheduled_at = Column(DateTime, nullable=True)
    # Meta
    word_count = Column(Integer, default=0)
    error_message = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    published_at = Column(DateTime, nullable=True)

    owner = relationship("User", back_populates="articles")


class AdsenseToken(Base):
    __tablename__ = "adsense_tokens"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(200), default="")
    nickname = Column(String(200), default="")
    account_id = Column(String(200), default="")
    access_token = Column(Text, default="")
    refresh_token = Column(Text, default="")
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DailyStat(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    page_views = Column(Integer, default=0)
    rpm = Column(Float, default=0)
    revenue_usd = Column(Float, default=0)

    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_account_date"),
    )

    account = relationship("User")


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(50), primary_key=True)
    value = Column(Text, default="")


def init_db():
    Base.metadata.create_all(bind=engine)
    # Auto-add missing columns (works with both SQLite and PostgreSQL)
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    with engine.begin() as conn:
        for table_name, model in [("articles", Article), ("users", User), ("adsense_tokens", AdsenseToken), ("daily_stats", DailyStat)]:
            try:
                existing = {c["name"] for c in inspector.get_columns(table_name)}
                for col in model.__table__.columns:
                    if col.name not in existing:
                        col_type = col.type.compile(dialect=engine.dialect)
                        conn.execute(text(
                            f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type} DEFAULT NULL"
                        ))
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

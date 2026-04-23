from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

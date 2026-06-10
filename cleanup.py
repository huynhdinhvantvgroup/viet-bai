"""Cron job: xóa ảnh uploads và bài viết cũ hơn 14 ngày.
Chạy: python /opt/health-writer/cleanup.py
Cron: 0 3 * * * /opt/health-writer/venv/bin/python /opt/health-writer/cleanup.py
"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from app.models import SessionLocal, Article

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DAYS = 14

cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)

# 1. Xóa bài viết cũ hơn 14 ngày (đã published hoặc failed)
db = SessionLocal()
old_articles = db.query(Article).filter(
    Article.created_at < cutoff,
    Article.status.in_(["published", "failed"]),
).all()

deleted_files = 0
for article in old_articles:
    for fpath in [article.screenshot_path, article.featured_image_path]:
        if fpath:
            p = Path(fpath)
            if p.exists():
                p.unlink()
                deleted_files += 1
    db.delete(article)

deleted_articles = len(old_articles)
db.commit()
db.close()

# 2. Xóa ảnh mồ côi trong uploads/ (không thuộc bài nào, cũ hơn 14 ngày)
orphan_deleted = 0
if UPLOAD_DIR.exists():
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
            f.unlink()
            orphan_deleted += 1

print(f"[CLEANUP] Deleted {deleted_articles} articles, {deleted_files} linked files, {orphan_deleted} orphan files")

"""Cron job: xóa ảnh uploads và bài viết cũ hơn 14 ngày.
Chạy: python /opt/health-writer/cleanup.py
Cron: 0 3 * * * /opt/health-writer/venv/bin/python /opt/health-writer/cleanup.py
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.sqlite"
UPLOAD_DIR = BASE_DIR / "uploads"
DAYS = 14

cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

# 1. Xóa bài viết cũ hơn 14 ngày (đã published hoặc failed)
conn = sqlite3.connect(str(DB_PATH))
cursor = conn.cursor()

# Lấy ảnh paths của bài sắp xóa để xóa file
cursor.execute("""
    SELECT screenshot_path, featured_image_path FROM articles
    WHERE created_at < ? AND status IN ('published', 'failed')
""", (cutoff_str,))
rows = cursor.fetchall()

deleted_files = 0
for screenshot, featured in rows:
    for fpath in [screenshot, featured]:
        if fpath:
            p = Path(fpath)
            if p.exists():
                p.unlink()
                deleted_files += 1

# Xóa records
cursor.execute("""
    DELETE FROM articles
    WHERE created_at < ? AND status IN ('published', 'failed')
""", (cutoff_str,))
deleted_articles = cursor.rowcount
conn.commit()
conn.close()

# 2. Xóa ảnh mồ côi trong uploads/ (không thuộc bài nào, cũ hơn 14 ngày)
orphan_deleted = 0
if UPLOAD_DIR.exists():
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
            f.unlink()
            orphan_deleted += 1

print(f"[CLEANUP] Deleted {deleted_articles} articles, {deleted_files} linked files, {orphan_deleted} orphan files")

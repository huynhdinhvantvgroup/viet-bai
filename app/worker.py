import logging
from urllib.parse import urlparse
from arq import create_pool, cron
from arq.connections import RedisSettings
from app.config import REDIS_URL
from app.models import SessionLocal, Article, User
from app.services.batch_runner import process_single_article, publish_article
from sqlalchemy import update

logger = logging.getLogger("arq.worker")


def parse_redis_url(url: str) -> RedisSettings:
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password,
    )


async def process_article_task(ctx, article_id: int):
    """arq task: process a single article end-to-end."""
    db = SessionLocal()
    try:
        article = db.get(Article, article_id)
        if not article or article.status != "pending":
            return

        # Optimistic lock: claim article atomically
        result = db.execute(
            update(Article)
            .where(Article.id == article_id, Article.status == "pending")
            .values(status="generating")
        )
        db.commit()
        if result.rowcount == 0:
            return  # Another worker claimed it

        db.refresh(article)

        # Load owner config
        owner = db.get(User, article.user_id) if article.user_id else None
        wp_cfg = None
        custom_prompt = None
        if owner:
            if owner.wp_url and owner.wp_username and owner.wp_app_password:
                wp_cfg = {
                    "url": owner.wp_url,
                    "username": owner.wp_username,
                    "password": owner.wp_app_password,
                }
            custom_prompt = owner.custom_prompt

        # Process article (generate → images → upload)
        await process_single_article(article, db, wp_cfg=wp_cfg, custom_prompt=custom_prompt)

        # Check if user requested deletion during processing
        db.refresh(article)
        if article.status == "deleting":
            db.delete(article)
            db.commit()
            return

        # Auto publish
        if article.status not in ("failed",) and wp_cfg:
            await publish_article(article, db, wp_cfg=wp_cfg)
        elif not wp_cfg:
            article.status = "failed"
            article.error_message = "Chưa cấu hình WordPress. Vào Cài đặt để thiết lập."
            db.commit()

        # Check deletion again after publish
        db.refresh(article)
        if article.status == "deleting":
            db.delete(article)
            db.commit()

    except Exception as e:
        logger.error(f"[WORKER ERR] article {article_id}: {e}")
        try:
            article = db.get(Article, article_id)
            if article and article.status != "deleting":
                article.status = "failed"
                article.error_message = str(e)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def sync_adsense_cron(ctx):
    """Cron job: sync AdSense data daily at 23:00 UTC (6:00 AM VN)."""
    from app.services.adsense_sync import sync_daily_stats
    logger.info("[CRON] Starting daily AdSense sync")
    await sync_daily_stats()
    logger.info("[CRON] AdSense sync complete")


class WorkerSettings:
    functions = [process_article_task]
    cron_jobs = [cron(sync_adsense_cron, hour=23, minute=0)]
    redis_settings = parse_redis_url(REDIS_URL)
    max_jobs = 10
    job_timeout = 600
    max_tries = 1
    health_check_interval = 30

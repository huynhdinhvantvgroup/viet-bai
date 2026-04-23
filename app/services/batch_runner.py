import asyncio
import csv
import json
import io
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import Article, User
from app.services.gpt_generator import generate_article, generate_image_keywords
from app.services.image_search import get_article_images
from app.services.wp_publisher import (
    upload_media_from_url_wp,
    upload_media_from_path_wp,
    insert_featured_image_into_content,
    insert_images_into_content,
    create_post_wp,
)


# ─── Background Worker State ─────────────────────────────

_worker_running = False
_worker_task = None


def is_worker_running() -> bool:
    return _worker_running


async def start_worker(db_factory):
    global _worker_running, _worker_task
    if _worker_running:
        return
    _worker_running = True
    _worker_task = asyncio.create_task(_worker_loop(db_factory))


async def _worker_loop(db_factory):
    global _worker_running
    while True:
        db = db_factory()
        try:
            article = db.query(Article).filter(
                Article.status == "pending"
            ).order_by(Article.id.asc()).first()

            if not article:
                db.close()
                await asyncio.sleep(2)
                continue

            # Load the article owner's config
            owner = db.query(User).get(article.user_id) if article.user_id else None
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

            # Process: generate → images → publish
            await process_single_article(article, db, wp_cfg=wp_cfg, custom_prompt=custom_prompt)

            # Auto publish
            if article.status not in ("failed",) and wp_cfg:
                await publish_article(article, db, wp_cfg=wp_cfg)
            elif not wp_cfg:
                article.status = "failed"
                article.error_message = "Chưa cấu hình WordPress. Vào Cài đặt để thiết lập."
                db.commit()

            await asyncio.sleep(1)
        except Exception as e:
            print(f"[WORKER ERR] {e}")
            await asyncio.sleep(3)
        finally:
            db.close()


async def run_queued(article_ids: list[int], db_factory):
    db = db_factory()
    try:
        for aid in article_ids:
            article = db.query(Article).get(aid)
            if article and article.status == "queued":
                article.status = "pending"
        db.commit()
    finally:
        db.close()


# ─── CSV Parser ──────────────────────────────────────────

def parse_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        rows.append({
            "competitor_content": row.get("competitor_content", "").strip(),
            "notes": row.get("notes", "").strip(),
            "screenshot": row.get("screenshot", "").strip(),
            "featured_image": row.get("featured_image", "").strip(),
            "category": row.get("category", "Health").strip(),
            "tags": row.get("tags", "").strip(),
        })
    return rows


# ─── Article Processing Pipeline ─────────────────────────

async def process_single_article(
    article: Article,
    db: Session,
    wp_cfg: dict | None = None,
    custom_prompt: str | None = None,
) -> Article:
    try:
        # Step 1: Generate article text
        article.status = "generating"
        article.error_message = ""
        db.commit()

        result = await generate_article(
            screenshot_path=article.screenshot_path,
            competitor_content=article.competitor_content,
            custom_prompt=custom_prompt,
        )
        article.title = result["title"]
        article.content = result["content"]
        article.word_count = result["word_count"]
        db.commit()

        # Step 2: Get stock image keywords
        article.status = "images"
        db.commit()

        keywords = await generate_image_keywords(article.title, article.content)
        article.stock_image_keywords = json.dumps(keywords)
        stock_images = await get_article_images(keywords)

        # Step 3: Upload featured image to WP
        featured_media_id = None
        featured_wp_url = ""
        if article.featured_image_path and wp_cfg:
            try:
                article.status = "uploading"
                db.commit()
                feat = await upload_media_from_path_wp(
                    article.featured_image_path,
                    f"featured-{article.id}.jpg",
                    wp_cfg,
                )
                featured_media_id = feat["id"]
                featured_wp_url = feat["url"]
            except Exception as e:
                print(f"[ERR] Featured upload: {e}")

        article.wp_featured_media_id = featured_media_id

        # Step 4: Upload stock images to WP
        wp_image_urls = []
        if wp_cfg:
            for i, img in enumerate(stock_images):
                try:
                    uploaded = await upload_media_from_url_wp(
                        img["url"],
                        f"article-{article.id}-img-{i+1}.jpg",
                        wp_cfg,
                    )
                    wp_image_urls.append(uploaded["url"])
                except Exception as e:
                    print(f"[ERR] Stock img {i+1}: {e}")

        article.stock_image_urls = json.dumps(wp_image_urls)

        # Step 5: Insert images into content
        if featured_wp_url:
            article.content = insert_featured_image_into_content(article.content, featured_wp_url)
        if wp_image_urls:
            article.content = insert_images_into_content(article.content, wp_image_urls)

        article.status = "ready"
        db.commit()
        return article

    except Exception as e:
        article.status = "failed"
        article.error_message = str(e)
        db.commit()
        return article


async def publish_article(article: Article, db: Session, wp_cfg: dict | None = None) -> Article:
    try:
        article.status = "publishing"
        db.commit()

        wp_result = await create_post_wp(
            title=article.title,
            content=article.content,
            featured_image_id=article.wp_featured_media_id,
            category=article.wp_category or "Health",
            tags=article.wp_tags or "",
            status="publish",
            scheduled_at=article.scheduled_at.isoformat() if article.scheduled_at else None,
            wp_cfg=wp_cfg,
        )

        article.wp_post_id = wp_result["id"]
        article.wp_post_url = wp_result["url"]
        article.status = "published"
        article.published_at = datetime.now(timezone.utc)
        db.commit()
        return article

    except Exception as e:
        article.status = "failed"
        article.error_message = str(e)
        db.commit()
        return article

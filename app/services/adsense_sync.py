"""AdSense sync: fetch current month data (day 1 → today) and upsert into daily_stats."""
import logging
from datetime import datetime, timedelta, timezone, date

from app.models import SessionLocal, AdsenseToken, DailyStat, User
from app.services.adsense_service import refresh_access_token, get_report

logger = logging.getLogger(__name__)


async def sync_daily_stats():
    """Fetch AdSense data for current month (day 1 → today) and upsert into daily_stats."""
    tz7 = timezone(timedelta(hours=7))
    today = datetime.now(tz7).date()
    month_start = today.replace(day=1)

    import app.config as cfg
    db = SessionLocal()
    try:
        tokens = db.query(AdsenseToken).all()
        if not tokens:
            logger.info("[SYNC] No AdSense tokens configured, skipping")
            return

        # Fetch data from all tokens
        all_rows = []
        for tk in tokens:
            try:
                exp = tk.expires_at.replace(tzinfo=None) if tk.expires_at and tk.expires_at.tzinfo else tk.expires_at
                now_utc = datetime.utcnow()
                if not exp or exp < now_utc:
                    new_data = await refresh_access_token(
                        tk.refresh_token, cfg.GOOGLE_CLIENT_ID, cfg.GOOGLE_CLIENT_SECRET
                    )
                    tk.access_token = new_data["access_token"]
                    tk.expires_at = now_utc + timedelta(seconds=new_data.get("expires_in", 3600))
                    db.commit()

                rows, _ = await get_report(
                    tk.access_token, tk.account_id,
                    str(month_start), str(today),
                )
                all_rows.extend(rows)
            except Exception as e:
                logger.error(f"[SYNC] Token {tk.id} error: {e}")
                continue

        if not all_rows:
            logger.info(f"[SYNC] No data for {month_start} → {today}")
            return

        # Map domain -> user
        users = db.query(User).filter(User.subdomain != "").all()
        domain_to_user = {u.subdomain: u for u in users}

        saved = 0
        for r in all_rows:
            domain = r.get("domain", "")
            user = domain_to_user.get(domain)
            if not user:
                continue

            row_date = date.fromisoformat(r.get("date", str(today)))
            pageviews = r.get("pageviews", 0)
            earnings = r.get("earnings", 0)
            rpm = (earnings / pageviews * 1000) if pageviews > 0 else 0

            existing = db.query(DailyStat).filter(
                DailyStat.account_id == user.id,
                DailyStat.date == row_date,
            ).first()

            if existing:
                existing.page_views = pageviews
                existing.rpm = round(rpm, 4)
                existing.revenue_usd = round(earnings, 4)
            else:
                db.add(DailyStat(
                    account_id=user.id,
                    date=row_date,
                    page_views=pageviews,
                    rpm=round(rpm, 4),
                    revenue_usd=round(earnings, 4),
                ))
            saved += 1

        db.commit()
        logger.info(f"[SYNC] {month_start} → {today}: saved {saved} records")

    except Exception as e:
        logger.error(f"[SYNC] Failed: {e}")
    finally:
        db.close()

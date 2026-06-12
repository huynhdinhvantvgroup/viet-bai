"""Seed bonus tiers + system config defaults."""
import json
from app.models import SessionLocal, SystemConfig, engine, Base

BONUS_TIERS = [
    {"min_usd": 0, "max_usd": 999, "bonus_rate": 0.00},
    {"min_usd": 1000, "max_usd": 1999, "bonus_rate": 0.08},
    {"min_usd": 2000, "max_usd": 2999, "bonus_rate": 0.10},
    {"min_usd": 3000, "max_usd": 5999, "bonus_rate": 0.15},
    {"min_usd": 6000, "max_usd": 8999, "bonus_rate": 0.18},
    {"min_usd": 9000, "max_usd": 10999, "bonus_rate": 0.22},
    {"min_usd": 11000, "max_usd": 12999, "bonus_rate": 0.24},
    {"min_usd": 13000, "max_usd": 20999, "bonus_rate": 0.26},
    {"min_usd": 21000, "max_usd": 30999, "bonus_rate": 0.28},
    {"min_usd": 31000, "max_usd": 40999, "bonus_rate": 0.30},
    {"min_usd": 41000, "max_usd": 50000, "bonus_rate": 0.32},
]

DEFAULTS = {
    "bonus_tiers": json.dumps(BONUS_TIERS),
    "conversion_rate": "0.35",
    "usd_vnd_rate": "26000",
}


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for key, value in DEFAULTS.items():
            existing = db.get(SystemConfig, key)
            if not existing:
                db.add(SystemConfig(key=key, value=value))
                print(f"  Added: {key}")
            else:
                print(f"  Exists: {key}")
        db.commit()
        print("Seed complete!")
    finally:
        db.close()


if __name__ == "__main__":
    seed()

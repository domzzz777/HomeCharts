"""
Seed the database with REAL listings scraped from multiple markets:
  - Batumi, Georgia   — myhome.ge (Facebook XML feed)
  - Malta             — maltapark.com (category 248)
  - Albania           — century21albania.com
  - Greece            — tranio.com (JSON-LD, apartments)

Each listing gets its actual photos and a synthetic price-drop history so
the dashboard demonstrates price-drop detection from day one.

Usage:
    python seed_demo.py                         # all markets, 2 pages each
    python seed_demo.py --market georgia        # only Georgia
    python seed_demo.py --market malta          # only Malta
    python seed_demo.py --market albania        # only Albania
    python seed_demo.py --market greece         # only Greece
    python seed_demo.py --pages 3               # more listings per market
    python seed_demo.py --clear                 # wipe DB first, then reseed
"""

import argparse
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

from database import create_tables, SessionLocal, Listing, PriceHistory
from scraper import scrape_myhome_batumi, scrape_maltapark, scrape_c21_albania, scrape_tranio_greece, scrape_tranio_spain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def ensure_images(listings: list[dict]) -> list[dict]:
    for l in listings:
        if not l.get("images") and l.get("image_url"):
            l["images"] = [l["image_url"]]
    return listings


def _add_synthetic_history(
    db,
    listing_obj: Listing,
    current_price: float,
    currency: str,
    now: datetime,
) -> None:
    drop_pct   = random.uniform(0.10, 0.22)
    unit       = 1000 if current_price >= 5000 else 100
    peak_price = max(round(current_price / (1 - drop_pct) / unit) * unit, current_price + unit)
    mid_price  = round((peak_price + current_price) / 2 / (unit // 2)) * (unit // 2)

    days_total = random.randint(50, 90)
    days_mid   = random.randint(20, days_total - 15)

    for price, days_ago in [
        (peak_price,    days_total),
        (mid_price,     days_mid),
        (current_price, random.randint(2, 8)),
    ]:
        change = ((price - peak_price) / peak_price * 100) if price != peak_price else 0.0
        db.add(PriceHistory(
            listing_id=listing_obj.id,
            price=price,
            currency=currency,
            recorded_at=now - timedelta(days=days_ago),
            change_pct=round(change, 2),
        ))


def _save_listings(db, listings: list[dict], now: datetime) -> tuple[int, int]:
    saved = skipped = 0
    for l in listings:
        ext_id = l["external_id"]
        if db.query(Listing).filter_by(external_id=ext_id).first():
            skipped += 1
            continue

        current_price = l["price"]
        currency      = l.get("currency", "USD")
        imgs          = l.get("images") or ([l["image_url"]] if l.get("image_url") else [])

        listing_obj = Listing(
            external_id=ext_id,
            portal=l["portal"],
            title=l.get("title"),
            url=l.get("url"),
            city=l.get("city"),
            country=l.get("country"),
            neighborhood=l.get("neighborhood"),
            price=current_price,
            currency=currency,
            area_sqm=l.get("area_sqm"),
            rooms=l.get("rooms"),
            floor=l.get("floor"),
            total_floors=l.get("total_floors"),
            image_url=imgs[0] if imgs else None,
            images=json.dumps(imgs) if imgs else None,
            phone=l.get("phone"),
            first_seen_at=now - timedelta(days=random.randint(55, 95)),
            last_seen_at=now - timedelta(hours=random.randint(1, 6)),
            last_scraped_at=now - timedelta(hours=random.randint(1, 6)),
        )
        db.add(listing_obj)
        db.flush()
        _add_synthetic_history(db, listing_obj, current_price, currency, now)
        saved += 1

    db.commit()
    return saved, skipped


async def seed(clear: bool = False, pages: int = 2, market: str = "all",
               start_page: int = 1) -> None:
    create_tables()
    db = SessionLocal()

    if clear:
        count = db.query(Listing).count()
        db.query(Listing).delete()
        db.commit()
        logger.info(f"Cleared {count} existing listings")

    now = datetime.utcnow()
    total_saved = 0

    markets = {
        "georgia": ("myhome.ge (Batumi)",              scrape_myhome_batumi),
        "malta":   ("maltapark.com (Malta)",            scrape_maltapark),
        "albania": ("century21albania.com (Albania)",   scrape_c21_albania),
        "greece":  ("tranio.com (Greece)",              scrape_tranio_greece),
        "spain":   ("tranio.com (Spain)",               scrape_tranio_spain),
    }

    to_scrape = markets if market == "all" else {market: markets[market]}

    for key, (label, scraper_fn) in to_scrape.items():
        logger.info(f"Scraping {pages} page(s) from {label} (start_page={start_page}) …")
        try:
            kwargs = {"max_pages": pages}
            if key in ("greece", "spain"):
                kwargs["start_page"] = start_page
            raw = await scraper_fn(**kwargs)
        except Exception as exc:
            logger.error(f"{label} scrape failed: {exc}")
            continue

        seen: set = set()
        candidates = []
        for l in raw:
            if l.get("price") and l["external_id"] not in seen:
                seen.add(l["external_id"])
                candidates.append(l)

        if not candidates:
            logger.warning(f"{label}: no listings with prices found")
            continue

        enriched = ensure_images(candidates)
        saved, skipped = _save_listings(db, enriched, now)
        logger.info(f"{label}: {saved} new listings saved, {skipped} already existed")
        total_saved += saved

    db.close()
    logger.info(f"\n✓ Done! {total_saved} total new listings saved.\n"
                f"  Start the app: uvicorn app:app --reload")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed HomeCharts with real listings")
    parser.add_argument("--clear",  action="store_true", help="Wipe DB first")
    parser.add_argument("--pages",      type=int, default=2, help="Pages per market (default 2)")
    parser.add_argument("--start-page", type=int, default=1, help="Start page for Greece scrape (default 1)")
    parser.add_argument("--market", default="all",
                        choices=["all", "georgia", "malta", "albania", "greece", "spain"],
                        help="Which market to seed (default: all)")
    args = parser.parse_args()
    asyncio.run(seed(clear=args.clear, pages=args.pages, market=args.market,
                     start_page=args.start_page))

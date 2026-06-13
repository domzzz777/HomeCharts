import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import resend

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import EmailAlert, Listing, create_tables, get_db
from scraper import run_scrape_all, scrape_single_url, upsert_listing
from scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    start_scheduler()
    logger.info("HomeCharts started — SQLite DB ready, scheduler running")
    yield


app = FastAPI(
    title="HomeCharts",
    description="Real estate price drop tracker — Batumi & emerging markets",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def root():
    return FileResponse(
        "static/landing.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/app", response_class=FileResponse, include_in_schema=False)
async def dashboard():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ─── Stats ────────────────────────────────────────────────────────────────────


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    listings = db.query(Listing).filter(Listing.is_active == True).all()
    # Only count drops that pass all three validation rules
    validated = [(l, _validate_drop(l)) for l in listings]
    drop_listings = [(l, dp) for l, (dp, susp) in validated if dp < -2 and not susp]
    biggest = min(drop_listings, key=lambda x: x[1], default=None)
    avg_drop = (
        sum(dp for _, dp in drop_listings) / len(drop_listings)
        if drop_listings
        else 0.0
    )
    last_scraped = max(
        (l.last_scraped_at for l in listings if l.last_scraped_at),
        default=None,
    )
    return {
        "total_tracked": len(listings),
        "active_drops": len(drop_listings),
        "biggest_drop_pct": round(biggest[1], 1) if biggest else 0.0,
        "avg_drop_pct": round(avg_drop, 1),
        "last_updated": last_scraped.isoformat() if last_scraped else None,
        "portals": sorted({l.portal for l in listings}),
        "markets": sorted({l.city for l in listings}),
    }


@app.get("/api/markets")
async def get_markets(db: Session = Depends(get_db)):
    """Per-country stats for the landing page market cards."""
    listings = db.query(Listing).filter(Listing.is_active == True).all()

    COUNTRY_META = {
        "Georgia": {"flag": "🇬🇪", "subtitle": "Batumi · Black Sea"},
        "Albania": {"flag": "🇦🇱", "subtitle": "Tirana · Durrës"},
        "Malta":   {"flag": "🇲🇹", "subtitle": "Valletta · Gozo"},
        "Greece":  {"flag": "🇬🇷", "subtitle": "Athens · Crete · Islands"},
        "Spain":   {"flag": "🇪🇸", "subtitle": "Costa del Sol · Mallorca · Tenerife"},
    }

    buckets: dict = {}
    for l in listings:
        c = l.country or "Unknown"
        if c not in buckets:
            buckets[c] = {"total": 0, "drops": 0, "drop_values": [], "images": []}
        buckets[c]["total"] += 1
        drop_pct, suspicious = _validate_drop(l)
        if not suspicious and drop_pct < -1:
            buckets[c]["drops"] += 1
            buckets[c]["drop_values"].append(drop_pct)
        # collect up to 3 sample images per country
        imgs = []
        if l.images:
            try:
                imgs = json.loads(l.images)
            except Exception:
                pass
        if not imgs and l.image_url:
            imgs = [l.image_url]
        if imgs and len(buckets[c]["images"]) < 3:
            buckets[c]["images"].append(imgs[0])

    result = []
    for country, meta in COUNTRY_META.items():
        data = buckets.get(country, {"total": 0, "drops": 0, "drop_values": [], "images": []})
        biggest = min(data["drop_values"], default=0.0)
        avg = (sum(data["drop_values"]) / len(data["drop_values"])) if data["drop_values"] else 0.0
        result.append({
            "country":        country,
            "flag":           meta["flag"],
            "subtitle":       meta["subtitle"],
            "total_listings": data["total"],
            "active_drops":   data["drops"],
            "biggest_drop_pct": round(biggest, 1),
            "avg_drop_pct":   round(avg, 1),
            "sample_images":  data["images"][:3],
        })

    return result


# ─── Listings ─────────────────────────────────────────────────────────────────


# ─── Drop validation ──────────────────────────────────────────────────────────

# Thresholds — adjust here without touching anything else.
MIN_TRACKING_DAYS    = 0    # show drops immediately once a price change is detected
MAX_SINGLE_DROP_PCT  = 25.0 # a single scan-to-scan drop cannot exceed this %
MAX_TOTAL_DROP_PCT   = 50.0 # total peak-to-current drop cannot exceed this %
                            # (rule 3 — assumed 50% since the spec was cut off)


def _validate_drop(l: Listing) -> tuple:
    """
    Returns (validated_drop_pct: float, suspicious: bool).

    A drop is shown only when ALL three rules pass:
      1. The listing has been tracked for at least MIN_TRACKING_DAYS.
      2. No single consecutive scan-to-scan drop exceeds MAX_SINGLE_DROP_PCT.
      3. The total peak-to-current drop does not exceed MAX_TOTAL_DROP_PCT.

    When any rule fails, drop_pct is returned as 0.0 and suspicious=True.
    """
    raw_drop = l.price_drop_pct           # negative means price went down

    # No drop to validate
    if raw_drop >= -1.0:
        return (0.0, False)

    history = sorted(l.price_history, key=lambda x: x.recorded_at)

    # ── Rule 1: minimum tracking age ──────────────────────────────────────────
    if l.first_seen_at:
        age_days = (datetime.utcnow() - l.first_seen_at).days
        if age_days < MIN_TRACKING_DAYS:
            return (0.0, True)

    # ── Rule 2: no single scan-to-scan drop > MAX_SINGLE_DROP_PCT ────────────
    for ph in history:
        if ph.change_pct is not None and ph.change_pct < -MAX_SINGLE_DROP_PCT:
            return (0.0, True)

    # ── Rule 3: total cumulative drop ≤ MAX_TOTAL_DROP_PCT ───────────────────
    if raw_drop < -MAX_TOTAL_DROP_PCT:
        return (0.0, True)

    return (round(raw_drop, 1), False)


def _parse_images(l: Listing) -> list:
    if l.images:
        try:
            imgs = json.loads(l.images)
            if isinstance(imgs, list):
                return imgs
        except (json.JSONDecodeError, TypeError):
            pass
    return [l.image_url] if l.image_url else []


def _to_dict(l: Listing) -> dict:
    history = sorted(l.price_history, key=lambda x: x.recorded_at)
    images = _parse_images(l)
    drop_pct, suspicious = _validate_drop(l)
    return {
        "id": l.id,
        "external_id": l.external_id,
        "portal": l.portal,
        "title": l.title,
        "url": l.url,
        "city": l.city,
        "country": l.country,
        "neighborhood": l.neighborhood,
        "price": l.price,
        "currency": l.currency,
        "area_sqm": l.area_sqm,
        "rooms": l.rooms,
        "floor": l.floor,
        "total_floors": l.total_floors,
        "image_url": l.image_url,
        "images": images,
        "phone": l.phone,
        "drop_pct": drop_pct,
        "drop_suspicious": suspicious,
        "max_price": l.max_price,
        "initial_price": l.initial_price,
        "first_seen_at": l.first_seen_at.isoformat() if l.first_seen_at else None,
        "last_seen_at": l.last_seen_at.isoformat() if l.last_seen_at else None,
        "history": [
            {
                "price": ph.price,
                "currency": ph.currency,
                "recorded_at": ph.recorded_at.isoformat(),
                "change_pct": ph.change_pct,
            }
            for ph in history
        ],
    }


@app.get("/api/ticker")
async def get_ticker(db: Session = Depends(get_db)):
    """Return top 30 active price drops for the ticker."""
    import random as _rnd
    listings = db.query(Listing).filter(Listing.is_active == True).all()
    drop_items = []
    now = datetime.utcnow()

    def _make_label(l: Listing) -> str:
        parts = []
        if l.rooms:
            parts.append(f"{l.rooms}BR")
        if l.city and l.city not in ("Georgia", "Albania", "Malta", "Greece", "Spain", "Montenegro", "Portugal", "Cyprus"):
            parts.append(l.city)
        elif l.country:
            parts.append(l.country)
        return " · ".join(parts) if parts else (l.country or "")

    def _ago(ts) -> str:
        diff = now - (ts or now)
        mins = int(diff.total_seconds() / 60)
        if mins < 60:   return f"{mins}m ago"
        if mins < 1440: return f"{mins // 60}h ago"
        return f"{mins // 1440}d ago"

    for l in listings:
        drop_pct, suspicious = _validate_drop(l)
        if suspicious or drop_pct >= -1.0:
            continue
        ts = l.last_seen_at or l.last_scraped_at or now
        drop_items.append({
            "label":    _make_label(l),
            "drop_pct": drop_pct,
            "price":    None,
            "currency": None,
            "ago":      _ago(ts),
            "country":  l.country or "",
            "url":      l.url or "#",
            "kind":     "drop",
        })

    if not drop_items:
        return []

    brackets = [
        (1,  7,  6), (7,  15, 6), (15, 25, 6), (25, 50, 6), (50, 999, 4),
    ]
    seen: set = set()
    result: list = []
    for lo, hi, picks in brackets:
        bucket = [x for x in drop_items if lo <= abs(x["drop_pct"]) < hi]
        _rnd.shuffle(bucket)
        for x in bucket[:picks]:
            seen.add(id(x))
            result.append(x)
    for x in drop_items:
        if len(result) >= 30: break
        if id(x) not in seen: result.append(x)
    _rnd.shuffle(result)
    return result[:30]


class TrackUrlRequest(BaseModel):
    url: str


@app.post("/api/track-url")
async def track_url(body: TrackUrlRequest, db: Session = Depends(get_db)):
    """Scrape a single listing URL, save it to the DB and return the listing dict."""
    url = body.url.strip()
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL — must start with http(s)://")

    listing_data = await scrape_single_url(url)
    if not listing_data:
        raise HTTPException(
            status_code=422,
            detail="Could not extract listing data from this URL. "
                   "Make sure it is a direct link to a property listing page.",
        )

    saved = upsert_listing(db, listing_data)
    if not saved:
        raise HTTPException(status_code=500, detail="Failed to save listing to database.")

    # Re-query so relationships (price_history) are loaded
    db.refresh(saved)
    result = _to_dict(saved)
    result["status"] = "tracked"
    result["is_new"] = len(saved.price_history) <= 1
    return result


# ─── Email alerts (Resend) ─────────────────────────────────────────────────────

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
ALERT_FROM     = os.getenv("ALERT_FROM", "contact@home-charts.com")

# Initialise Resend SDK (no-op if key missing)
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Send a single HTML email via Resend. Returns True on success."""
    if not RESEND_API_KEY:
        logger.warning("Resend not configured — set RESEND_API_KEY in .env")
        return False
    try:
        resend.Emails.send({
            "from":    f"HomeCharts <{ALERT_FROM}>",
            "to":      [to],
            "subject": subject,
            "html":    html_body,
        })
        logger.info(f"Email sent via Resend to {to}: {subject}")
        return True
    except Exception as exc:
        logger.error(f"Resend email failed to {to}: {exc}")
        return False


def _fmt_price(listing) -> str:
    sym_map = {"USD": "$", "EUR": "€", "GBP": "£", "PLN": "zł", "GEL": "₾", "ALL": "L", "MTL": "€"}
    sym = sym_map.get(listing.currency or "USD", listing.currency or "$")
    p   = round(listing.price or 0)
    if listing.currency in ("PLN", "GEL", "ALL"):
        return f"{p:,} {sym}"
    return f"{sym}{p:,}"


def _listing_thumb(listing) -> str:
    """Return first raw image URL (direct, no proxy — proxy URLs break in email clients)."""
    try:
        imgs = json.loads(listing.images or "[]")
        if imgs:
            return imgs[0] if imgs[0].startswith("http") else ""
    except Exception:
        pass
    return ""


# ── Shared email CSS reset + dark-mode forcing ────────────────────────────────
_EMAIL_HEAD = """<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/>
<meta name="supported-color-schemes" content="dark"/>
<style>
  body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}
  table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}
  img{-ms-interpolation-mode:bicubic;border:0;display:block;}
  body{margin:0!important;padding:0!important;background-color:#080e1a!important;}
  @media (prefers-color-scheme:dark){
    body,#body-wrapper{background-color:#080e1a!important;}
  }
</style>
</head>"""

_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"


def _welcome_email_html(listing) -> str:
    flag     = {"Georgia":"🇬🇪","Albania":"🇦🇱","Malta":"🇲🇹","Greece":"🇬🇷","Spain":"🇪🇸","Poland":"🇵🇱"}.get(listing.country or "", "🏠")
    price    = _fmt_price(listing)
    thumb    = _listing_thumb(listing)
    location = ", ".join(filter(None, [listing.city, listing.country]))
    specs_parts = list(filter(None, [
        f"{listing.rooms} {'room' if listing.rooms == 1 else 'rooms'}" if listing.rooms else None,
        f"{listing.area_sqm} m²" if listing.area_sqm else None,
    ]))
    specs = " · ".join(specs_parts)

    img_row = f"""
    <tr>
      <td style="padding:0;line-height:0;">
        <img src="{thumb}" width="560" alt="Property photo"
             style="width:100%;max-width:560px;height:220px;object-fit:cover;display:block;border-radius:16px 16px 0 0;" />
      </td>
    </tr>""" if thumb else f"""
    <tr>
      <td bgcolor="#0f172a" align="center"
          style="padding:40px 0;border-radius:16px 16px 0 0;background-color:#0f172a;">
        <span style="font-size:56px;line-height:1;">🏠</span>
      </td>
    </tr>"""

    specs_row = f"""
            <tr>
              <td style="padding:4px 0 12px;{_FONT}font-size:13px;color:#64748b;">
                {flag} {location}{(' &nbsp;·&nbsp; ' + specs) if specs else ''}
              </td>
            </tr>""" if location else ""

    return f"""<!DOCTYPE html>
<html lang="pl">{_EMAIL_HEAD}
<body id="body-wrapper" style="margin:0;padding:0;background-color:#080e1a;{_FONT}">
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#080e1a"
       style="background-color:#080e1a;padding:32px 16px;">
  <tr><td align="center">

  <!-- Outer card -->
  <table width="560" cellpadding="0" cellspacing="0"
         style="max-width:560px;width:100%;background-color:#0d1626;
                border-radius:16px;overflow:hidden;
                border:1px solid #1e293b;">

    {img_row}

    <!-- Header bar with logo -->
    <tr>
      <td bgcolor="#0a1628" style="padding:20px 28px 16px;background-color:#0a1628;
                                   border-bottom:1px solid #1e293b;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="{_FONT}font-size:22px;font-weight:900;letter-spacing:-.02em;color:#f1f5f9;">
              Home<span style="color:#ef4444;">Charts</span>
            </td>
            <td align="right"
                style="{_FONT}font-size:11px;color:#475569;font-weight:500;
                       text-transform:uppercase;letter-spacing:.08em;">
              Price Tracker
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Body -->
    <tr>
      <td style="padding:28px 28px 0;">
        <table width="100%" cellpadding="0" cellspacing="0">

          <!-- Headline -->
          <tr>
            <td style="{_FONT}font-size:21px;font-weight:700;color:#f1f5f9;
                       padding-bottom:8px;line-height:1.3;">
              ✅ Zacząłeś śledzić nieruchomość
            </td>
          </tr>
          <tr>
            <td style="{_FONT}font-size:14px;color:#64748b;padding-bottom:24px;line-height:1.6;">
              Od teraz monitorujemy cenę tej nieruchomości i wyślemy Ci email przy każdym spadku.
            </td>
          </tr>

          <!-- Property card -->
          <tr>
            <td bgcolor="#0b1120" style="background-color:#0b1120;border:1px solid #1e293b;
                                        border-radius:12px;padding:20px 20px 18px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="{_FONT}font-size:15px;font-weight:600;color:#e2e8f0;
                             padding-bottom:8px;line-height:1.4;">
                    {listing.title or 'Nieruchomość'}
                  </td>
                </tr>
                {specs_row}
                <tr>
                  <td style="{_FONT}font-size:28px;font-weight:800;color:#f1f5f9;
                             letter-spacing:-.02em;padding-bottom:4px;">
                    {price}
                  </td>
                </tr>
                <tr>
                  <td style="{_FONT}font-size:10px;color:#334155;text-transform:uppercase;
                             letter-spacing:.1em;font-weight:600;">
                    CENA STARTOWA (MOMENT DODANIA)
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Spacer -->
          <tr><td style="height:20px;">&nbsp;</td></tr>

          <!-- Info box -->
          <tr>
            <td bgcolor="#0c1a35" style="background-color:#0c1a35;border:1px solid #1e3a6e;
                                        border-radius:10px;padding:14px 16px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="{_FONT}font-size:13px;font-weight:700;color:#60a5fa;
                             padding-bottom:6px;">
                    📡 Jak działa śledzenie?
                  </td>
                </tr>
                <tr>
                  <td style="{_FONT}font-size:13px;color:#93c5fd;line-height:1.65;">
                    HomeCharts sprawdza cenę tej nieruchomości dwa razy dziennie
                    (06:00 i 13:00 UTC). Gdy wykryje spadek, natychmiast wyślemy
                    powiadomienie na Twój adres email.
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Spacer -->
          <tr><td style="height:24px;">&nbsp;</td></tr>

          <!-- CTA button -->
          <tr>
            <td align="center" style="padding-bottom:28px;">
              <a href="{listing.url}" target="_blank"
                 style="{_FONT}display:inline-block;background-color:#3b82f6;color:#ffffff;
                        text-decoration:none;font-weight:700;font-size:15px;
                        padding:14px 40px;border-radius:10px;letter-spacing:.01em;">
                Otwórz ogłoszenie &rarr;
              </a>
            </td>
          </tr>

        </table>
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td bgcolor="#060d1a" style="background-color:#060d1a;padding:16px 28px;
                                   border-top:1px solid #1e293b;
                                   {_FONT}font-size:11px;color:#334155;text-align:center;
                                   line-height:1.7;">
        Otrzymujesz ten email, bo dodałeś nieruchomość do śledzenia na HomeCharts.<br/>
        &copy; 2026 HomeCharts &middot; Where smart investors find the dip
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body></html>"""


def _alert_email_html(listing, old_price: float, new_price: float, drop_pct: float) -> str:
    sym_map  = {"USD":"$","EUR":"€","GBP":"£","PLN":"zł","GEL":"₾","ALL":"L"}
    sym      = sym_map.get(listing.currency or "USD", listing.currency or "$")
    suffix   = listing.currency in ("PLN","GEL","ALL")
    fmt      = lambda p: f"{round(p):,} {sym}" if suffix else f"{sym}{round(p):,}"
    flag     = {"Georgia":"🇬🇪","Albania":"🇦🇱","Malta":"🇲🇹","Greece":"🇬🇷","Spain":"🇪🇸","Poland":"🇵🇱"}.get(listing.country or "", "🏠")
    thumb    = _listing_thumb(listing)
    location = ", ".join(filter(None, [listing.city, listing.country]))

    img_row = f"""
    <tr>
      <td style="padding:0;line-height:0;">
        <img src="{thumb}" width="560" alt="Property photo"
             style="width:100%;max-width:560px;height:220px;object-fit:cover;
                    display:block;border-radius:16px 16px 0 0;" />
      </td>
    </tr>""" if thumb else ""

    return f"""<!DOCTYPE html>
<html lang="en">{_EMAIL_HEAD}
<body id="body-wrapper" style="margin:0;padding:0;background-color:#080e1a;{_FONT}">
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#080e1a"
       style="background-color:#080e1a;padding:32px 16px;">
  <tr><td align="center">

  <table width="560" cellpadding="0" cellspacing="0"
         style="max-width:560px;width:100%;background-color:#0d1626;
                border-radius:16px;overflow:hidden;border:1px solid #1e293b;">

    {img_row}

    <!-- Header -->
    <tr>
      <td bgcolor="#0a1628" style="padding:22px 28px 18px;background-color:#0a1628;
                                   border-bottom:1px solid #1e293b;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="{_FONT}font-size:22px;font-weight:900;color:#f1f5f9;">
              Home<span style="color:#ef4444;">Charts</span>
            </td>
            <td align="right"
                style="{_FONT}font-size:11px;color:#ef4444;font-weight:600;
                       text-transform:uppercase;letter-spacing:.1em;">
              Price Drop Alert
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Body -->
    <tr>
      <td style="padding:28px 28px 0;">
        <table width="100%" cellpadding="0" cellspacing="0">

          <!-- Title -->
          <tr>
            <td style="{_FONT}font-size:20px;font-weight:700;color:#f1f5f9;padding-bottom:6px;">
              {flag} Price Drop Detected!
            </td>
          </tr>
          <tr>
            <td style="{_FONT}font-size:14px;color:#64748b;padding-bottom:22px;line-height:1.5;">
              {listing.title or 'Property listing'}
              {'&nbsp;&middot;&nbsp;' + location if location else ''}
            </td>
          </tr>

          <!-- Price boxes — 3 columns -->
          <tr>
            <td style="padding-bottom:22px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <!-- Was -->
                  <td width="32%" bgcolor="#1e293b"
                      style="background-color:#1e293b;border-radius:10px;padding:14px 12px;
                             text-align:center;vertical-align:top;">
                    <div style="{_FONT}font-size:10px;color:#64748b;text-transform:uppercase;
                                letter-spacing:.1em;font-weight:600;margin-bottom:6px;">Was</div>
                    <div style="{_FONT}font-size:18px;font-weight:700;color:#64748b;
                                text-decoration:line-through;">{fmt(old_price)}</div>
                  </td>
                  <td width="4%">&nbsp;</td>
                  <!-- Now -->
                  <td width="32%" bgcolor="#1e293b"
                      style="background-color:#1e293b;border-radius:10px;padding:14px 12px;
                             text-align:center;vertical-align:top;">
                    <div style="{_FONT}font-size:10px;color:#64748b;text-transform:uppercase;
                                letter-spacing:.1em;font-weight:600;margin-bottom:6px;">Now</div>
                    <div style="{_FONT}font-size:18px;font-weight:700;color:#f1f5f9;">
                      {fmt(new_price)}</div>
                  </td>
                  <td width="4%">&nbsp;</td>
                  <!-- Drop -->
                  <td width="28%" bgcolor="#1a0a0a"
                      style="background-color:#1a0a0a;border:1px solid #7f1d1d;
                             border-radius:10px;padding:14px 12px;
                             text-align:center;vertical-align:top;">
                    <div style="{_FONT}font-size:10px;color:#f87171;text-transform:uppercase;
                                letter-spacing:.1em;font-weight:600;margin-bottom:6px;">Drop</div>
                    <div style="{_FONT}font-size:22px;font-weight:900;color:#ef4444;">
                      &minus;{abs(drop_pct):.1f}%</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td align="center" style="padding-bottom:28px;">
              <a href="{listing.url}" target="_blank"
                 style="{_FONT}display:inline-block;background-color:#ef4444;color:#ffffff;
                        text-decoration:none;font-weight:700;font-size:15px;
                        padding:14px 40px;border-radius:10px;">
                View Listing &rarr;
              </a>
            </td>
          </tr>

        </table>
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td bgcolor="#060d1a" style="background-color:#060d1a;padding:16px 28px;
                                   border-top:1px solid #1e293b;
                                   {_FONT}font-size:11px;color:#334155;
                                   text-align:center;line-height:1.7;">
        You&apos;re receiving this because you subscribed to price alerts on HomeCharts.<br/>
        &copy; 2026 HomeCharts &middot; Where smart investors find the dip
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body></html>"""


class AlertRequest(BaseModel):
    listing_id: int
    email: str


@app.post("/api/alert")
async def create_alert(body: AlertRequest, db: Session = Depends(get_db)):
    """Subscribe an email address to price change notifications for a listing."""
    listing = db.query(Listing).filter(Listing.id == body.listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Idempotent — don't add duplicate alert
    existing = db.query(EmailAlert).filter_by(listing_id=body.listing_id, email=email).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.commit()
        return {"status": "already_subscribed", "email": email}

    alert = EmailAlert(
        listing_id=body.listing_id,
        email=email,
        created_at=datetime.utcnow(),
        last_notified_price=listing.price,
    )
    db.add(alert)
    db.commit()

    # Send welcome / confirmation email
    _send_email(email, f"🏠 Zacząłeś śledzić nieruchomość — HomeCharts", _welcome_email_html(listing))

    return {"status": "subscribed", "email": email}


def send_price_drop_alerts(db, listing: Listing, old_price: float, new_price: float, drop_pct: float):
    """Called by the scraper whenever a price drop is recorded."""
    alerts = db.query(EmailAlert).filter_by(listing_id=listing.id, is_active=True).all()
    for alert in alerts:
        # Don't spam — only notify if price dropped further than last notification
        if alert.last_notified_price and new_price >= alert.last_notified_price:
            continue
        sent = _send_email(
            alert.email,
            f"📉 −{abs(drop_pct):.1f}% price drop — {listing.title or listing.city or 'Listing'}",
            _alert_email_html(listing, old_price, new_price, drop_pct),
        )
        if sent:
            alert.last_notified_at    = datetime.utcnow()
            alert.last_notified_price = new_price
    db.commit()


@app.get("/api/listings")
async def get_listings(
    sort: str = "drop",
    portal: Optional[str] = None,
    city: Optional[str] = None,
    min_drop: float = 0,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Listing).filter(Listing.is_active == True)
    if portal:
        query = query.filter(Listing.portal == portal)
    if city:
        query = query.filter(Listing.city == city)

    listings = query.all()
    result = [_to_dict(l) for l in listings if l.price_history]

    if min_drop > 0:
        result = [r for r in result if r["drop_pct"] <= -min_drop]

    if search:
        q = search.lower()
        result = [
            r for r in result
            if q in (r["title"] or "").lower()
            or q in (r["neighborhood"] or "").lower()
        ]

    sort_map = {
        "drop": lambda x: x["drop_pct"],
        "price_asc": lambda x: x["price"] or 0,
        "price_desc": lambda x: -(x["price"] or 0),
        "latest": lambda x: x["last_seen_at"] or "",
        "area": lambda x: -(x["area_sqm"] or 0),
    }
    result.sort(key=sort_map.get(sort, sort_map["drop"]))
    return result


@app.get("/api/listings/{listing_id}")
async def get_listing(listing_id: int, db: Session = Depends(get_db)):
    l = db.query(Listing).filter(Listing.id == listing_id).first()
    if not l:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _to_dict(l)


# ─── Image proxy ──────────────────────────────────────────────────────────────

# Domains whose images require a server-side proxy due to hotlink protection
_PROXY_DOMAINS = {"tranio.com"}

_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/webp,image/avif,image/*,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


@app.get("/api/proxy-image")
async def proxy_image(url: str = Query(..., description="Image URL to proxy")):
    """
    Fetch an image server-side and return it to the browser.
    Used for portals (e.g. tranio.com) that block direct hotlinking.
    """
    # Only proxy from explicitly allowed domains
    allowed = any(d in url for d in _PROXY_DOMAINS)
    if not allowed or not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL not allowed for proxying")

    # Pick the appropriate Referer
    referer = "https://tranio.com/" if "tranio.com" in url else None
    headers = {**_PROXY_HEADERS}
    if referer:
        headers["Referer"] = referer

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Image fetch failed")
            content_type = r.headers.get("content-type", "image/jpeg")
            return Response(content=r.content, media_type=content_type)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Image proxy error: {exc}")


# ─── Scrape trigger ───────────────────────────────────────────────────────────


@app.post("/api/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scrape_all)
    return {"status": "started", "message": "Scrape job running in background"}

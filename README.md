# HomeCharts 📉

> Real estate price drop tracker for emerging investment markets.  
> Starting with **Batumi, Georgia** — a Black Sea market ignored by platforms like luxurypricedrops.com.

---

## Features

- Scrapes **myhome.ge** and **ss.ge** daily for Batumi apartment listings
- Detects price drops by comparing each scan to historical data
- Stores everything in a local **SQLite** database
- Dashboard shows drop %, current vs peak price, and **interactive price history charts**
- Manual scrape trigger via the UI or API
- Auto-refresh every 60 seconds
- Scheduler runs scrapes at **06:00 and 13:00 UTC** daily

---

## Quick Start

```bash
# 1. Install dependencies
cd HomeCharts
pip install -r requirements.txt

# 2. Seed with 25 realistic demo listings (Batumi, price drops included)
python seed_demo.py

# 3. Start the server
uvicorn app:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

---

## Scraping Live Data

Click **Scrape Now** in the header, or call the API:

```bash
curl -X POST http://localhost:8000/api/scrape
```

The scraper targets:
| Portal | URL |
|--------|-----|
| myhome.ge | `https://www.myhome.ge/en/s/Batumi?AdTypeID=1&PrTypeID=1.2` |
| ss.ge | `https://ss.ge/en/real-estate/batumi/apartments` |

> **Note:** If portal HTML markup changes, update the CSS selectors in `scraper.py`  
> (look for `# TODO: verify selector` comments).  
> The scrapers first try Next.js `__NEXT_DATA__` JSON extraction before falling back to HTML parsing.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` | GET | Summary stats (total, drops, biggest drop) |
| `/api/listings` | GET | All listings with price history |
| `/api/listings?sort=drop` | GET | Sort by biggest drop |
| `/api/listings?portal=myhome.ge` | GET | Filter by portal |
| `/api/listings?min_drop=10` | GET | Only ≥10% drops |
| `/api/listings?search=boulevard` | GET | Full-text search |
| `/api/listings/{id}` | GET | Single listing details |
| `/api/scrape` | POST | Trigger a scrape in the background |

---

## Project Structure

```
HomeCharts/
├── app.py           # FastAPI app, API endpoints, static file serving
├── database.py      # SQLAlchemy models: Listing + PriceHistory
├── scraper.py       # myhome.ge + ss.ge scrapers, upsert logic
├── scheduler.py     # APScheduler (06:00 + 13:00 UTC daily)
├── seed_demo.py     # 25 realistic Batumi demo listings
├── requirements.txt
├── data/
│   └── homecharts.db   # SQLite database (auto-created)
└── static/
    └── index.html      # Dark-themed SPA (Tailwind + Chart.js)
```

---

## Adding More Markets

1. Add a new scraper function in `scraper.py` (model after `scrape_myhome_batumi`)
2. Call it inside `run_scrape_all()`
3. The UI market pills (Greece, Albania, Malta, Poland) are ready — just wire up the data

---

## Roadmap

- [ ] Twitter/X auto-posting for biggest daily drops
- [ ] Email digest
- [ ] Greece (Spitogatos, xe.gr)
- [ ] Albania (njoftime.com)
- [ ] Malta (maltapark.com)
- [ ] Poland (otodom.pl)
- [ ] Price-per-m² normalization
- [ ] Playwright fallback for JS-heavy portals

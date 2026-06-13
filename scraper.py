"""
Scrapers for Batumi real estate portals.

Supported portals:
  - myhome.ge  (primary Georgian portal — parses React-Query dehydratedState)
  - ss.ge      (secondary Georgian classifieds — HTML card parsing)

myhome.ge implementation notes
───────────────────────────────
The site uses Next.js + React Query.  Listing data is embedded in the HTML
inside __NEXT_DATA__ → props.pageProps.dehydratedState.queries[].state.data.
The SSR pre-renders generic listings (mix of cities); adding CityIDList=2
to the URL URL biases the result toward Batumi.  We filter afterwards by
city_name == "Batumi".

Each listing already includes all photos in images[{large, thumb, blur}],
so a separate detail-page fetch is not required.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from database import SessionLocal, Listing, PriceHistory, EmailAlert

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

REQUEST_DELAY = (1.5, 4.0)
MAX_RETRIES   = 3
MAX_PAGES     = 15   # more pages = more Batumi listings in the SSR mix


# ─── Shared helpers ────────────────────────────────────────────────────────────


def _collect_images(photos: list, max_images: int = 12) -> list[str]:
    """Normalise a photos list → deduplicated list of full-size URL strings."""
    urls: list[str] = []
    for p in photos:
        if isinstance(p, dict):
            url = p.get("large") or p.get("medium") or p.get("thumb") or p.get("url")
        elif isinstance(p, str):
            url = p
        else:
            continue
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= max_images:
            break
    return urls


def _parse_price(text: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", "").replace("\u00a0", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


# ─── myhome.ge ─────────────────────────────────────────────────────────────────

MYHOME_BASE   = "https://www.myhome.ge"

# Facebook Marketplace XML feed — contains all for-sale listings with photos.
# The feed already includes Batumi listings (city == "ბათუმი") and each one
# has up to 16 full-res photos served from static-statements.tnet.ge.
# Prices in the feed are in GEL; we convert to USD using a fixed rate.
MYHOME_FB_XML = "https://www.myhome.ge/facebook.xml"
GEL_TO_USD    = 0.37   # approximate; update as needed

BATUMI_GEO    = "ბათუმი"   # Georgian for "Batumi"


def _parse_myhome_xml_listing(item) -> Optional[dict]:
    """Parse one <listing> element from myhome.ge's facebook.xml feed."""
    try:
        def tag(name: str) -> str:
            el = item.find(name)
            return el.get_text(strip=True) if el else ""

        listing_id = tag("home_listing_id")
        if not listing_id:
            return None

        # Images: all <image><url> children
        image_urls: list[str] = []
        for img_tag in item.find_all("image"):
            url_el = img_tag.find("url")
            if url_el:
                u = url_el.get_text(strip=True)
                if u.startswith("http") and u not in image_urls:
                    image_urls.append(u)
        image_urls = image_urls[:12]

        # Price in GEL → convert to USD
        price_raw = tag("price")                          # e.g. "360525 GEL"
        price_usd: Optional[float] = None
        pm = re.match(r"([\d.]+)\s*(GEL|USD|\$)?", price_raw, re.IGNORECASE)
        if pm:
            amount = float(pm.group(1))
            currency_str = (pm.group(2) or "GEL").upper()
            price_usd = round(amount * GEL_TO_USD) if currency_str == "GEL" else amount

        # Title: translate common Georgian patterns to English
        name_raw = tag("name")
        title = _translate_geo_title(name_raw) or "Apartment in Batumi"

        # Address from <address><component>
        addr_el = item.find("address")
        neighborhood: Optional[str] = None
        if addr_el:
            comp = addr_el.find("component")
            neighborhood = comp.get_text(strip=True) if comp else None

        # Rooms from <num_rooms>
        rooms_raw = tag("num_rooms")
        rooms = int(rooms_raw) if rooms_raw.isdigit() else None

        # The listing <url> is a direct child of <listing>.
        # Photo <url> tags are nested inside <image> — skip those.
        listing_url = None
        for url_el in item.find_all("url", recursive=False):
            u = url_el.get_text(strip=True)
            if u.startswith("http") and "myhome.ge" in u:
                listing_url = u
                break
        if not listing_url:
            # Fallback: construct from ID
            listing_url = f"{MYHOME_BASE}/pr/{listing_id}"

        return {
            "external_id": f"myhome_{listing_id}",
            "portal":       "myhome.ge",
            "title":        title,
            "url":          listing_url,
            "city":         "Batumi",
            "country":      "Georgia",
            "neighborhood": neighborhood,
            "price":        price_usd,
            "currency":     "USD",
            "area_sqm":     None,
            "rooms":        rooms,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_urls[0] if image_urls else None,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"myhome.ge XML listing parse error: {exc}")
        return None


def _translate_geo_title(geo_text: str) -> str:
    """
    Convert common Georgian real estate title patterns to English.
    Only handles the most frequent phrases; everything else is left as-is.
    """
    if not geo_text:
        return ""
    patterns = [
        (r"იყიდება\s*(\d+)\s*ოთახიანი\s*ბინა\s*(ბათუმში)?",
         lambda m: f"{m.group(1)}-Room Apartment For Sale in Batumi"),
        (r"ქირავდება\s*(\d+)\s*ოთახიანი\s*ბინა\s*(ბათუმში)?",
         lambda m: f"{m.group(1)}-Room Apartment For Rent in Batumi"),
        (r"სტუდიო\s*(ბინა)?\s*(ბათუმში)?",
         lambda m: "Studio Apartment in Batumi"),
        (r"ბინა\s*(ბათუმში)?",
         lambda m: "Apartment in Batumi"),
    ]
    for pat, repl in patterns:
        m = re.search(pat, geo_text, re.UNICODE)
        if m:
            try:
                return repl(m)
            except Exception:
                pass
    # Fallback: if it looks Georgian, just return generic
    if any("\u10d0" <= c <= "\u10ff" for c in geo_text):
        return "Apartment in Batumi"
    return geo_text


# Georgian mobile: 9 digits starting with 5, optionally prefixed with +995 / 995
_GEO_PHONE_RE = re.compile(
    r'(?:\+?995\s?)?'          # optional country code
    r'([5][0-9]{2})'           # 3-digit prefix (5XX)
    r'[\s\-\.]?'
    r'([0-9]{2,3})'
    r'[\s\-\.]?'
    r'([0-9]{2,3})'
    r'[\s\-\.]?'
    r'([0-9]{0,3})'
)


def _normalize_geo_phone(raw: str) -> Optional[str]:
    """Return E.164 digits (no +) suitable for wa.me links, e.g. '995598488688'."""
    digits = re.sub(r'\D', '', raw)
    if digits.startswith('995') and len(digits) >= 12:
        return digits[:12]
    if digits.startswith('5') and len(digits) == 9:
        return '995' + digits
    return None


async def _fetch_phone_from_description(
    client: httpx.AsyncClient, listing_id: str
) -> Optional[str]:
    """
    Fetch a myhome.ge listing page and look for a Georgian mobile number
    in the seller's free-text description (comment field in __NEXT_DATA__).
    Returns normalized E.164 digits or None.
    """
    try:
        await asyncio.sleep(random.uniform(0.3, 0.8))
        resp = await client.get(f"{MYHOME_BASE}/en/pr/{listing_id}", timeout=15.0)
        if resp.status_code != 200:
            return None
        nd_tag = BeautifulSoup(resp.text, "html.parser").find(
            "script", id="__NEXT_DATA__"
        )
        if not nd_tag:
            return None
        raw_json = json.dumps(
            json.loads(nd_tag.string).get("props", {}).get("pageProps", {})
        )
        # Pull all "comment" values (first long one is the real description)
        comments = re.findall(r'"comment"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_json)
        for ct in comments:
            # strip unicode escapes to plain ASCII/latin so regex works
            desc = re.sub(r'\\u[0-9a-fA-F]{4}', ' ', ct)
            for m in _GEO_PHONE_RE.finditer(desc):
                phone = _normalize_geo_phone(m.group(0))
                if phone:
                    return phone
    except Exception as exc:
        logger.debug(f"phone fetch {listing_id}: {exc}")
    return None


async def scrape_myhome_batumi(max_pages: int = MAX_PAGES) -> list[dict]:
    """
    Scrape Batumi for-sale listings from myhome.ge Facebook Marketplace XML feed.
    Returns listings with real CDN photos from static-statements.tnet.ge.
    max_pages is ignored (the feed is one file); kept for API compatibility.
    """
    async with httpx.AsyncClient(
        headers={**HEADERS, "User-Agent": "facebookexternalhit/1.1"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        results: list[dict] = []
        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                resp = await client.get(MYHOME_FB_XML)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "xml")
                items = soup.find_all("listing")
                batumi = [
                    l for l in items
                    if BATUMI_GEO in (l.find("city").get_text() if l.find("city") else "")
                    and (l.find("availability") or object()).get_text("") == "for_sale"
                ]
                results = [r for l in batumi if (r := _parse_myhome_xml_listing(l))]
                logger.info(f"myhome.ge XML: {len(results)} Batumi for-sale listings")
                break
            except httpx.HTTPError as exc:
                logger.warning(f"myhome.ge XML fetch attempt {attempt + 1}: {exc}")

        if not results:
            return []

        # Fetch phone numbers from individual listing pages in parallel.
        # Use a semaphore to cap concurrent requests at 6.
        sem = asyncio.Semaphore(6)

        async def _phone_task(listing: dict) -> None:
            lid = listing["external_id"].replace("myhome_", "")
            async with sem:
                phone = await _fetch_phone_from_description(client, lid)
            if phone:
                listing["phone"] = phone

        await asyncio.gather(*[_phone_task(r) for r in results])
        phones_found = sum(1 for r in results if r.get("phone"))
        logger.info(f"myhome.ge: {phones_found}/{len(results)} listings have phone in description")
        return results


# ─── ss.ge ─────────────────────────────────────────────────────────────────────

SS_BASE   = "https://ss.ge"
SS_SEARCH = "https://ss.ge/en/real-estate/batumi/apartments?page={page}"


def _parse_ss_card(card) -> Optional[dict]:
    try:
        link_el = card.find("a", href=re.compile(r"/en/real-estate/"))
        if not link_el:
            return None

        href = link_el["href"]
        id_match = re.search(r"/(\d+)(?:/|$|\?)", href)
        if not id_match:
            return None
        listing_id = id_match.group(1)
        url = urljoin(SS_BASE, href)

        price_el   = card.select_one(".price, .cost, [data-price], .listing-price")
        price      = _parse_price(price_el.get_text(strip=True)) if price_el else None
        title_el   = card.select_one("h3, h2, .title, .item-title")
        title      = title_el.get_text(strip=True) if title_el else None

        text   = card.get_text(" ", strip=True)
        area_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m²|sq\.?m|sqm|m2)", text)
        rooms_m = re.search(r"(\d+)\s*(?:room|br|bedroom)", text, re.IGNORECASE)

        img_els    = card.select("img[src]")
        image_urls = [img["src"] for img in img_els if img.get("src")]
        image_url  = image_urls[0] if image_urls else None

        return {
            "external_id": f"ssge_{listing_id}",
            "portal":       "ss.ge",
            "title":        title or "Apartment in Batumi",
            "url":          url,
            "city":         "Batumi",
            "country":      "Georgia",
            "neighborhood": None,
            "price":        price,
            "currency":     "USD",
            "area_sqm":     float(area_m.group(1)) if area_m else None,
            "rooms":        int(rooms_m.group(1))  if rooms_m else None,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_url,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"ss.ge card parse error: {exc}")
        return None


async def _scrape_ss_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    url = SS_SEARCH.format(page=page)
    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.sleep(random.uniform(*REQUEST_DELAY))
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            cards   = soup.select(".latest-announcements-item, .article-item, .s-item, .listing-item")
            results = [r for c in cards if (r := _parse_ss_card(c))]
            logger.info(f"ss.ge page {page}: {len(results)} items")
            if not results and page > 1:
                return []
            return results
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429):
                await asyncio.sleep(15 * (attempt + 1))
            else:
                logger.warning(f"ss.ge page {page} HTTP {exc.response.status_code}")
                break
        except httpx.HTTPError as exc:
            logger.warning(f"ss.ge page {page} attempt {attempt + 1}: {exc}")
    return []


async def scrape_ss_batumi(max_pages: int = 5) -> list[dict]:
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=30.0, follow_redirects=True
    ) as client:
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            items = await _scrape_ss_page(client, page)
            if not items:
                break
            results.extend(items)
    return results


# ─── maltapark.com ─────────────────────────────────────────────────────────────

MALTAPARK_BASE     = "https://www.maltapark.com"
MALTAPARK_CAT_URL  = "https://www.maltapark.com/listings/category/248/?page={page}"

# Major Maltese localities for city detection
_MALTA_CITIES = [
    "Valletta", "Sliema", "St Julian", "San Ġwann", "Msida", "Birkirkara",
    "Qormi", "Mosta", "Naxxar", "Mellieħa", "St Paul", "Marsaskala",
    "Marsaxlokk", "Paola", "Rabat", "Mdina", "Zejtun", "Zebbug", "Mgarr",
    "Floriana", "Pembroke", "Swieqi", "Attard", "Balzan", "Luqa", "Marsa",
    "Siggiewi", "Tarxien", "Zabbar", "Zurrieq", "Birgu", "Senglea",
    "Cospicua", "Gozo", "Victoria", "Marsalforn", "Xlendi", "Xaghra",
    "Xewkija", "Nadur", "Hamrun", "Gzira", "Pieta", "Tarxien",
]
_MALTA_CITY_RE = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in _MALTA_CITIES) + r')\b',
    re.IGNORECASE,
)


def _parse_maltapark_card(card) -> Optional[dict]:
    """Parse one .item div from maltapark.com category 248 (Property For Sale)."""
    try:
        item_id = card.get("data-itemid")
        if not item_id:
            return None

        # Title
        header_el = card.select_one("a.header")
        title = header_el.get_text(strip=True) if header_el else f"Property in Malta #{item_id}"

        # Price (€ 355,000) — must be a real-estate value (≥ 10 000 EUR)
        price_el = card.select_one(".price span")
        price: Optional[float] = None
        if price_el:
            raw_price = price_el.get_text(strip=True).replace(",", "").replace(" ", "")
            pm = re.search(r"[\d\.]+", raw_price)
            if pm:
                try:
                    v = float(pm.group(0))
                    if v >= 10_000:   # skip sub-10k non-property items
                        price = v
                except ValueError:
                    pass

        # City — listed explicitly in the .extra section
        city = "Malta"
        loc_span = card.select(".extra .item span")
        if loc_span:
            city = loc_span[-1].get_text(strip=True) or "Malta"
        else:
            m = _MALTA_CITY_RE.search(card.get_text())
            if m:
                city = m.group(1)

        # Thumbnail URL; detail page has more images but we only grab thumb here
        img_el = card.select_one(".image img")
        img_src: Optional[str] = None
        if img_el and img_el.get("src"):
            s = img_el["src"]
            img_src = s if s.startswith("http") else MALTAPARK_BASE + s

        # Construct up to 10 image URLs from the known CDN pattern
        photo_count_el = card.select_one(".photocounter")
        try:
            n_photos = int(re.sub(r"\D", "", photo_count_el.get_text())) if photo_count_el else 1
        except ValueError:
            n_photos = 1
        n_photos = min(n_photos, 10)
        image_urls: list[str] = [
            f"{MALTAPARK_BASE}/asset/itemthumbs/{item_id}/{item_id}_{i}.jpg"
            for i in range(1, n_photos + 1)
        ]
        if img_src and img_src not in image_urls:
            image_urls = [img_src] + image_urls

        listing_url = f"{MALTAPARK_BASE}/item/details/{item_id}"

        return {
            "external_id": f"maltapark_{item_id}",
            "portal":       "maltapark.com",
            "title":        title,
            "url":          listing_url,
            "city":         city,
            "country":      "Malta",
            "neighborhood": None,
            "price":        price,
            "currency":     "EUR",
            "area_sqm":     None,
            "rooms":        None,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_urls[0] if image_urls else None,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"maltapark card parse error: {exc}")
        return None


# Maltese mobile: 8 digits starting with 7, 9, or 99
_MALTA_PHONE_RE = re.compile(r'\b([79]\d{7})\b')


async def _fetch_maltapark_phone(
    client: httpx.AsyncClient, listing_url: str
) -> Optional[str]:
    """
    Fetch a maltapark.com listing detail page and return the seller's phone
    as +356XXXXXXXX (E.164-style digits only, no +).
    """
    try:
        await asyncio.sleep(random.uniform(0.2, 0.5))
        resp = await client.get(listing_url, timeout=15.0)
        if resp.status_code != 200:
            return None
        m = _MALTA_PHONE_RE.search(resp.text)
        if m:
            return "356" + m.group(1)
    except Exception as exc:
        logger.debug(f"maltapark phone fetch {listing_url}: {exc}")
    return None


async def scrape_maltapark(max_pages: int = 10) -> list[dict]:
    """Scrape Property For Sale listings from maltapark.com (category 248)."""
    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=30.0, follow_redirects=True
    ) as client:
        for page in range(1, max_pages + 1):
            url = MALTAPARK_CAT_URL.format(page=page)
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.sleep(random.uniform(*REQUEST_DELAY))
                    resp = await client.get(url)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "lxml")
                    cards = [
                        c for c in soup.select(".item")
                        if c.get("data-itemid")
                    ]
                    if not cards:
                        logger.info(f"maltapark.com: no cards on page {page}, stopping")
                        return results
                    page_results = [r for c in cards if (r := _parse_maltapark_card(c))]
                    logger.info(f"maltapark.com page {page}: {len(page_results)} listings")
                    results.extend(page_results)
                    break
                except httpx.HTTPError as exc:
                    logger.warning(f"maltapark page {page} attempt {attempt + 1}: {exc}")

        # Fetch seller phone numbers from detail pages in parallel
        sem = asyncio.Semaphore(8)

        async def _phone_task(listing: dict) -> None:
            async with sem:
                phone = await _fetch_maltapark_phone(client, listing["url"])
                if phone:
                    listing["phone"] = phone

        logger.info(f"maltapark.com: fetching seller phones for {len(results)} listings…")
        await asyncio.gather(*[_phone_task(r) for r in results])
        phones_found = sum(1 for r in results if r.get("phone"))
        logger.info(f"maltapark.com: {phones_found}/{len(results)} listings have seller phone")

    return results


# ─── century21albania.com ───────────────────────────────────────────────────────

C21_BASE       = "https://www.century21albania.com"
C21_SEARCH_URL = (
    "https://www.century21albania.com/properties"
    "?deal_type=sale&property_type=apartment&page={page}"
)

_ALBANIA_CITIES = [
    "Tirana", "Tiranë", "Durrës", "Durres", "Vlorë", "Vlore",
    "Shkodër", "Shkoder", "Elbasan", "Korçë", "Korce", "Berat",
    "Lushnjë", "Lushnj", "Fier", "Gjirokastër", "Gjirokastr",
    "Sarandë", "Sarande", "Lezhë", "Lezhe", "Kavajë", "Kavaje",
    "Pogradec", "Kukës", "Kukes",
]
_ALBANIA_CITY_RE = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in _ALBANIA_CITIES) + r')\b',
    re.IGNORECASE,
)


def _parse_c21_albania_card(card) -> Optional[dict]:
    """Parse one property card from century21albania.com."""
    try:
        link_el = card.find("a", href=re.compile(r"/property/"))
        if not link_el:
            return None
        listing_url = link_el["href"]
        if not listing_url.startswith("http"):
            listing_url = C21_BASE + listing_url

        # ID from URL slug
        id_m = re.search(r"/property/(\d+)/", listing_url)
        if not id_m:
            return None
        listing_id = id_m.group(1)

        full_text = card.get_text(" ", strip=True)

        # Skip rentals (Qira = rent in Albanian)
        if re.search(r'\bQira\b', full_text):
            return None

        # Price (digits followed by €)
        price: Optional[float] = None
        price_m = re.search(r"([\d,\.]+)\s*€", full_text)
        if price_m:
            try:
                price = float(price_m.group(1).replace(",", "").replace(".", ""))
                # Sanity: prices should be between 10k and 50M EUR
                if not (10_000 <= price <= 50_000_000):
                    price = None
            except ValueError:
                pass

        # Title
        title_el = card.select_one("h2, h3, h4, .title, .property-title, .item-title")
        if not title_el:
            # Use the link text
            title_el = link_el
        title = title_el.get_text(strip=True)
        # Strip leading noise: "Shitje Roy142386 4 EXCLUSIVE ! " or "ShitjeEon14264 25"
        title = re.sub(
            r'^(?:Shitje|Qira)\s*\w{0,20}\d+\s*\d*\s*',
            '', title, flags=re.IGNORECASE
        ).strip()
        if not title:
            title = "Apartment in Albania"

        # Area (m²)
        area: Optional[float] = None
        area_m = re.search(r"(\d+)\s*m\s*2", full_text)
        if area_m:
            area = float(area_m.group(1))

        # City
        city = "Albania"
        city_m = _ALBANIA_CITY_RE.search(full_text)
        if city_m:
            city = city_m.group(1)

        # Images — CDN thumbnails from img tags
        image_urls = [
            img["src"] for img in card.find_all("img")
            if img.get("src", "").startswith("http")
        ]

        return {
            "external_id": f"c21al_{listing_id}",
            "portal":       "century21albania.com",
            "title":        title,
            "url":          listing_url,
            "city":         city,
            "country":      "Albania",
            "neighborhood": None,
            "price":        price,
            "currency":     "EUR",
            "area_sqm":     area,
            "rooms":        None,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_urls[0] if image_urls else None,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"c21 Albania card parse error: {exc}")
        return None


_C21_OFFICE_PHONE = re.compile(r'^(?:tel:)?(?:\+?355)?42\d{6}$')   # C21 Albania office landline
_C21_MOBILE_RE    = re.compile(r'tel:(\+?355[67]\d{8}|\+?355[67]\d{7})', re.IGNORECASE)


async def _fetch_c21_albania_phone(
    client: httpx.AsyncClient, listing_url: str
) -> Optional[str]:
    """
    Fetch a Century21 Albania listing page and return the agent's mobile number
    (the unique per-listing tel: link, skipping the shared office landline).
    """
    try:
        await asyncio.sleep(random.uniform(0.3, 0.7))
        resp = await client.get(listing_url, timeout=15.0)
        if resp.status_code != 200:
            return None
        for m in _C21_MOBILE_RE.finditer(resp.text):
            raw = re.sub(r'\D', '', m.group(1))
            # Normalise: ensure country code 355 is present
            if not raw.startswith('355'):
                raw = '355' + raw
            if len(raw) >= 11:
                return raw
    except Exception as exc:
        logger.debug(f"c21 albania phone fetch {listing_url}: {exc}")
    return None


async def scrape_c21_albania(max_pages: int = 5) -> list[dict]:
    """Scrape for-sale apartment listings from century21albania.com."""
    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=30.0, follow_redirects=True
    ) as client:
        for page in range(1, max_pages + 1):
            url = C21_SEARCH_URL.format(page=page)
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.sleep(random.uniform(*REQUEST_DELAY))
                    resp = await client.get(url)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "lxml")
                    cards = [
                        c for c in soup.select('[class*="property"]')
                        if c.find("a", href=re.compile(r"/property/"))
                    ]
                    if not cards:
                        logger.info(f"c21albania page {page}: no cards, stopping")
                        return results
                    page_results = [r for c in cards if (r := _parse_c21_albania_card(c))]
                    logger.info(f"c21albania page {page}: {len(page_results)} listings")
                    results.extend(page_results)
                    break
                except httpx.HTTPError as exc:
                    logger.warning(f"c21albania page {page} attempt {attempt + 1}: {exc}")

        # Fetch agent mobile numbers from individual listing detail pages
        sem = asyncio.Semaphore(6)

        async def _phone_task(listing: dict) -> None:
            async with sem:
                phone = await _fetch_c21_albania_phone(client, listing["url"])
                if phone:
                    listing["phone"] = phone

        logger.info(f"c21albania: fetching agent phones for {len(results)} listings…")
        await asyncio.gather(*[_phone_task(r) for r in results])
        phones_found = sum(1 for r in results if r.get("phone"))
        logger.info(f"c21albania: {phones_found}/{len(results)} listings have agent phone")

    return results


# ─── tranio.com (Greece) ────────────────────────────────────────────────────────

TRANIO_BASE        = "https://tranio.com"
TRANIO_GREECE_URL  = "https://tranio.com/greece/?page={page}"  # all property types (3 042 listings)
TRANIO_HEADERS     = {
    **HEADERS,
    "Referer": "https://tranio.com/",
}

# Strip Tranio category prefixes like "Houses, villas, cottages in X" → "X"
_GREECE_CITY_CLEAN = re.compile(
    r'^(?:New homes?|Apartments?|Penthouses?|Properties?|Houses?(?:,\s*villas?(?:,\s*cottages?)?)?\s*|Villas?|For sale)\s+in\s+',
    re.IGNORECASE,
)
_GREECE_CITY_RE = re.compile(
    r'\b(Athens|Thessaloniki|Crete|Santorini|Mykonos|Rhodes|Corfu|Paros|Naxos|'
    r'Glyfada|Varkiza|Voula|Alimos|Piraeus|Kallithea|Palaio\s*Faliro|Gazi|Marousi|Moschato|'
    r'Kalamata|Nafplio|Loutraki|Halkidiki|Chalkidiki|Zakynthos|Kefalonia|'
    r'Lesbos|Lesvos|Kos|Patras|Larissa|Heraklion|Rethymno|Chania|'
    r'Elounda|Lasithi|Sitia|Kalyves|Maleme|Ammoudara|Kissamos|Rethymno|'
    r'Kassandreia|Sithonia|Nikiti|Neos\s*Marmaras|Pefkochori|Kallithea\s*Chalkidikis|'
    r'Peloponnese|Messenia|Kyparissia|Korinthia|Xilokastro|'
    r'Attica|Vrilissia|Nikaia|Agia\s*Varvara|Peristeri|Agia\s*Paraskevi|'
    r'Lagonisi|Saronida|Sisi|Ammoudara)\b',
    re.IGNORECASE,
)


def _parse_tranio_listing(ld: dict) -> Optional[dict]:
    """Parse a single JSON-LD Offer/Apartment object from tranio.com."""
    try:
        url = ld.get("url", "")
        if not url or "tranio.com" not in url:
            return None

        # External ID from URL slug  e.g. "new-home-in-athens-2426373"
        slug_m = re.search(r"/adt/([^/]+)/?$", url)
        if not slug_m:
            return None
        listing_id = slug_m.group(1)

        price_str = str(ld.get("price") or "")
        if not price_str:
            return None
        # Handle "from 270000" style prices
        price_str = re.sub(r'^\s*from\s+', '', price_str, flags=re.IGNORECASE)
        try:
            price = float(price_str.replace(",", "").strip())
        except ValueError:
            return None
        if price < 30_000:   # skip sub-30k non-residential items
            return None

        title = ld.get("name", "").replace("\u00a0", " ").strip()
        if not title:
            title = "Apartment in Greece"

        # Area in m²
        floor_size = ld.get("floorSize") or {}
        area: Optional[float] = None
        try:
            area = float(floor_size.get("value", 0)) or None
        except (TypeError, ValueError):
            area = None

        # Rooms
        rooms: Optional[int] = None
        nr = ld.get("numberOfRooms")
        try:
            rooms = int(nr) if nr else None
        except (TypeError, ValueError):
            rooms = None

        # City: extracted from "category" field ("New homes in Athens" → "Athens")
        category = ld.get("category", "")
        city = "Greece"
        city_m = _GREECE_CITY_RE.search(category)
        if city_m:
            city = city_m.group(1)
        else:
            # fallback: strip prefix and take the remainder
            stripped = _GREECE_CITY_CLEAN.sub("", category).strip()
            # remove suffixes like "(Attica)", "(Halkidiki)"
            stripped = re.sub(r"\s*\([^)]+\)$", "", stripped).strip()
            if stripped and len(stripped) < 40:
                city = stripped

        # Thumbnail image (77x56 — served only with Referer: tranio.com)
        image_url = ld.get("image", "")
        image_urls = [image_url] if image_url else []

        return {
            "external_id": f"tranio_{listing_id}",
            "portal":       "tranio.com",
            "title":        title,
            "url":          url,
            "city":         city,
            "country":      "Greece",
            "neighborhood": None,
            "price":        price,
            "currency":     "EUR",
            "area_sqm":     area,
            "rooms":        rooms,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_urls[0] if image_urls else None,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"tranio listing parse error: {exc}")
        return None


async def _fetch_tranio_images(
    client: httpx.AsyncClient, listing: dict
) -> None:
    """
    Fetch the Tranio detail page for one listing and replace the thumbnail
    with the full-resolution images found in the HTML (462×308 and 924×616).
    Mutates the listing dict in-place.
    """
    try:
        await asyncio.sleep(random.uniform(0.3, 0.8))
        resp = await client.get(listing["url"], timeout=15.0)
        if resp.status_code != 200:
            return
        # Extract all tranio photo URLs from the page source (deduplicated)
        all_urls = list(dict.fromkeys(
            re.findall(r'https://tranio\.com/photos/[^\s"\'\\>]+\.jpg', resp.text)
        ))
        if all_urls:
            listing["images"]    = all_urls[:10]
            listing["image_url"] = all_urls[0]
    except Exception as exc:
        logger.debug(f"tranio image fetch {listing.get('url')}: {exc}")


async def scrape_tranio_greece(max_pages: int = 7, start_page: int = 1) -> list[dict]:
    """Scrape all for-sale listings from tranio.com/greece/ (apartments + villas + houses)."""
    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=TRANIO_HEADERS, timeout=30.0, follow_redirects=True
    ) as client:
        for page in range(start_page, start_page + max_pages):
            url = TRANIO_GREECE_URL.format(page=page)
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.sleep(random.uniform(*REQUEST_DELAY))
                    resp = await client.get(url)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")
                    # Each listing is a separate JSON-LD script block
                    ld_scripts = soup.find_all("script", type="application/ld+json")
                    page_results: list[dict] = []
                    for s in ld_scripts:
                        try:
                            data = json.loads(s.string or "[]")
                        except (json.JSONDecodeError, TypeError):
                            continue
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            types_str = str(item.get("@type", ""))
                            if any(t in types_str for t in ("Apartment", "House", "Villa", "Residence")):
                                r = _parse_tranio_listing(item)
                                if r:
                                    page_results.append(r)
                    if not page_results:
                        logger.info(f"tranio.com Greece: no listings on page {page}, stopping")
                        return results
                    logger.info(f"tranio.com Greece page {page}: {len(page_results)} listings")
                    results.extend(page_results)
                    break
                except httpx.HTTPError as exc:
                    logger.warning(f"tranio.com page {page} attempt {attempt + 1}: {exc}")

        # Fetch full-resolution images from detail pages in parallel.
        # Tranio only provides 77×56 thumbnails in JSON-LD; detail pages have
        # 924×616 and 462×308 images accessible with the correct Referer header.
        sem = asyncio.Semaphore(8)

        async def _img_task(listing: dict) -> None:
            async with sem:
                await _fetch_tranio_images(client, listing)

        logger.info(f"tranio.com Greece: fetching detail images for {len(results)} listings…")
        await asyncio.gather(*[_img_task(r) for r in results])
        with_imgs = sum(1 for r in results if len(r.get("images", [])) > 1)
        logger.info(f"tranio.com Greece: {with_imgs}/{len(results)} listings have detail images")

    return results


# ─── tranio.com (Spain) ─────────────────────────────────────────────────────────

# Reuse the same category prefix cleaner — identical format on Tranio
_SPAIN_CITY_CLEAN = _GREECE_CITY_CLEAN

# Known Spanish cities / regions scraped from tranio.com
_SPAIN_CITY_RE = re.compile(
    r'\b('
    # Costa del Sol
    r'Marbella|Mijas|Estepona|Nerja|Fuengirola|Benalm[aá]dena|Torremolinos|M[aá]laga|Casares|Manilva|'
    # Costa Blanca
    r'Alicante|Torrevieja|Benidorm|Calpe|Polop|Altea|Denia|Javea|Xabia|Finestrat|'
    r'Los\s*Alc[aá]zares|Orihuela|Rojales|Guardamar|Villajoyosa|'
    # Tenerife & Canaries
    r'Tenerife|Playa\s*Para[ií]so|Los\s*Cristianos|Costa\s*Adeje|Fanabe|Fa[ñn]abe|'
    r'Puerto\s*de\s*Santiago|Santa\s*Cruz\s*de\s*Tenerife|El\s*M[eé]dano|Golf\s*del\s*Sur|'
    r'La\s*Caleta|Adeje|Guia\s*de\s*Isora|Callao\s*Salvaje|Puerto\s*de\s*la\s*Cruz|'
    r'Tamaimo|Los\s*Gigantes|San\s*Miguel\s*de\s*Abona|Las\s*Galletas|'
    r'Gran\s*Canaria|Las\s*Palmas|Lanzarote|Fuerteventura|'
    # Barcelona & Costa Brava
    r'Barcelona|Lloret\s*de\s*Mar|Blanes|Sitges|Girona|Roses|Empuriabrava|'
    # Mallorca & Balearics
    r'Mallorca|Majorca|Palma|Pollensa|Alcudia|Andratx|Ibiza|Formentera|Menorca|'
    r'Sol\s*de\s*Mallorca|Costa\s*de\s*la\s*Calma|Puerto\s*de\s*Andratx|'
    r'Santa\s*Ponsa|Calvi[aà]|Camp\s*de\s*Mar|Port\s*d[\'e]\s*Andratx|'
    r'Portals\s*Nous|Cas\s*Catala|Illetes|Bendinat|Portals\s*Vells|'
    r'Cala\s*Millor|Cala\s*d\'Or|Cala\s*Ratjada|Porto\s*Cristo|'
    # Madrid
    r'Madrid|'
    # Murcia / Mar Menor
    r'Murcia|Los\s*Alc[aá]zares|'
    # Valencia
    r'Valencia|'
    # Generic
    r'Spain'
    r')\b',
    re.IGNORECASE,
)

# Region lookup: city → canonical region name used as `city` in DB
_SPAIN_CITY_TO_REGION: dict[str, str] = {}


def _spain_city_from_category(category: str) -> str:
    """Extract and normalise city from a Tranio category string."""
    cat = category.replace("\u00a0", " ").strip()
    # "Majorca (Mallorca)" → normalise to "Mallorca"
    cat = re.sub(r'\bMajorca\s*\(Mallorca\)', 'Mallorca', cat, flags=re.IGNORECASE)
    cat = re.sub(r'\bMajorca\b', 'Mallorca', cat, flags=re.IGNORECASE)
    # Try known-city regex first
    m = _SPAIN_CITY_RE.search(cat)
    if m:
        city = m.group(1).strip()
        # Normalise Majorca→Mallorca in matched result too
        return re.sub(r'^Majorca$', 'Mallorca', city, flags=re.IGNORECASE)
    # Fallback: strip type prefix ("New homes in X" → "X")
    stripped = _SPAIN_CITY_CLEAN.sub("", cat).strip()
    stripped = re.sub(r"\s*\([^)]+\)$", "", stripped).strip()
    return stripped if stripped and len(stripped) < 50 else "Spain"


def _parse_tranio_spain_listing(ld: dict) -> Optional[dict]:
    """Parse a single JSON-LD item from tranio.com Spain pages."""
    try:
        url = ld.get("url", "")
        if not url or "tranio.com" not in url:
            return None
        slug_m = re.search(r"/adt/([^/]+)/?$", url)
        if not slug_m:
            return None
        listing_id = slug_m.group(1)

        price_str = str(ld.get("price") or "")
        if not price_str:
            return None
        price_str = re.sub(r"^\s*from\s+", "", price_str, flags=re.IGNORECASE)
        try:
            price = float(price_str.replace(",", "").strip())
        except ValueError:
            return None
        if price < 30_000:
            return None

        title = ld.get("name", "").replace("\u00a0", " ").strip()
        if not title:
            title = "Property in Spain"

        floor_size = ld.get("floorSize") or {}
        area: Optional[float] = None
        try:
            area = float(floor_size.get("value", 0)) or None
        except (TypeError, ValueError):
            area = None

        rooms: Optional[int] = None
        nr = ld.get("numberOfRooms")
        try:
            rooms = int(nr) if nr else None
        except (TypeError, ValueError):
            rooms = None

        category = ld.get("category", "")
        city = _spain_city_from_category(category)

        image_url = ld.get("image", "")
        image_urls = [image_url] if image_url else []

        return {
            "external_id": f"tranio_{listing_id}",
            "portal":       "tranio.com",
            "title":        title,
            "url":          url,
            "city":         city,
            "country":      "Spain",
            "neighborhood": None,
            "price":        price,
            "currency":     "EUR",
            "area_sqm":     area,
            "rooms":        rooms,
            "floor":        None,
            "total_floors": None,
            "image_url":    image_urls[0] if image_urls else None,
            "images":       image_urls,
        }
    except Exception as exc:
        logger.debug(f"tranio spain parse error: {exc}")
        return None


async def scrape_tranio_spain(max_pages: int = 7, start_page: int = 1) -> list[dict]:
    """
    Scrape Spanish for-sale listings from tranio.com.

    Covers two feeds:
    - tranio.com/spain/?page=N         (Madrid, Costa del Sol, Costa Blanca, Barcelona …)
    - tranio.com/spain/tenerife/?page=N (Tenerife — 579 listings, separate feed)
    """
    FEEDS = [
        ("https://tranio.com/spain/?page={page}",          "Spain"),
        ("https://tranio.com/spain/tenerife/?page={page}", "Spain/Tenerife"),
        ("https://tranio.com/spain/majorca/?page={page}",  "Spain/Mallorca"),
    ]

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=TRANIO_HEADERS, timeout=30.0, follow_redirects=True
    ) as client:
        for url_tpl, feed_label in FEEDS:
            for page in range(start_page, start_page + max_pages):
                url = url_tpl.format(page=page)
                for attempt in range(MAX_RETRIES):
                    try:
                        await asyncio.sleep(random.uniform(*REQUEST_DELAY))
                        resp = await client.get(url)
                        if resp.status_code == 403:
                            logger.info(f"tranio.com {feed_label}: 403 on page {page}, stopping feed")
                            break
                        resp.raise_for_status()
                        soup = BeautifulSoup(resp.text, "html.parser")
                        page_results: list[dict] = []
                        for s in soup.find_all("script", type="application/ld+json"):
                            try:
                                data = json.loads(s.get_text() or "[]")
                            except (json.JSONDecodeError, TypeError):
                                continue
                            items = data if isinstance(data, list) else [data]
                            for item in items:
                                types_str = str(item.get("@type", ""))
                                if any(t in types_str for t in ("Apartment", "House", "Villa", "Residence")):
                                    r = _parse_tranio_spain_listing(item)
                                    if r:
                                        page_results.append(r)
                        if not page_results:
                            logger.info(f"tranio.com {feed_label}: no listings on page {page}, stopping feed")
                            break
                        logger.info(f"tranio.com {feed_label} page {page}: {len(page_results)} listings")
                        results.extend(page_results)
                        break
                    except httpx.HTTPError as exc:
                        logger.warning(f"tranio.com {feed_label} page {page} attempt {attempt + 1}: {exc}")
                else:
                    continue
                # If inner loop hit 403/no-results break, propagate it
                if resp.status_code == 403 or not page_results:
                    break

        # Fetch full-res images in parallel
        sem = asyncio.Semaphore(8)

        async def _img_task(listing: dict) -> None:
            async with sem:
                await _fetch_tranio_images(client, listing)

        logger.info(f"tranio.com Spain: fetching detail images for {len(results)} listings…")
        await asyncio.gather(*[_img_task(r) for r in results])
        with_imgs = sum(1 for r in results if len(r.get("images", [])) > 1)
        logger.info(f"tranio.com Spain: {with_imgs}/{len(results)} listings have detail images")

    return results


# ─── Database persistence ───────────────────────────────────────────────────────


def upsert_listing(db, listing_data: dict) -> Optional[Listing]:
    """Insert or update a listing; record a PriceHistory row on price change."""
    external_id = listing_data.get("external_id")
    new_price   = listing_data.get("price")
    if not external_id or new_price is None:
        return None

    now        = datetime.utcnow()
    new_images = listing_data.get("images") or []
    if listing_data.get("image_url") and listing_data["image_url"] not in new_images:
        new_images = [listing_data["image_url"]] + new_images

    existing = db.query(Listing).filter_by(external_id=external_id).first()

    if existing:
        old_price = existing.price
        existing.last_seen_at    = now
        existing.last_scraped_at = now
        existing.is_active       = True
        if new_images:
            existing.images    = json.dumps(new_images)
            existing.image_url = new_images[0]

        if listing_data.get("phone") and not existing.phone:
            existing.phone = listing_data["phone"]

        if old_price is not None and abs(new_price - old_price) > 0.01:
            change_pct = (new_price - old_price) / old_price * 100
            db.add(PriceHistory(
                listing_id=existing.id,
                price=new_price,
                currency=listing_data.get("currency", "USD"),
                recorded_at=now,
                change_pct=change_pct,
            ))
            existing.price = new_price
            if change_pct < -2:
                logger.info(
                    f"Price drop: {external_id} "
                    f"{old_price:.0f} → {new_price:.0f} ({change_pct:+.1f}%)"
                )
                # Fire email alerts (import lazily to avoid circular dependency)
                try:
                    from app import send_price_drop_alerts
                    send_price_drop_alerts(db, existing, old_price, new_price, change_pct)
                except Exception as _e:
                    logger.debug(f"Email alert skipped: {_e}")
    else:
        listing = Listing(
            external_id=external_id,
            portal=listing_data["portal"],
            title=listing_data.get("title"),
            url=listing_data.get("url"),
            city=listing_data.get("city", "Batumi"),
            country=listing_data.get("country", "Georgia"),
            neighborhood=listing_data.get("neighborhood"),
            price=new_price,
            currency=listing_data.get("currency", "USD"),
            area_sqm=listing_data.get("area_sqm"),
            rooms=listing_data.get("rooms"),
            floor=listing_data.get("floor"),
            total_floors=listing_data.get("total_floors"),
            image_url=new_images[0] if new_images else listing_data.get("image_url"),
            images=json.dumps(new_images) if new_images else None,
            phone=listing_data.get("phone"),
            first_seen_at=listing_data.get("first_seen_at") or now,
            last_seen_at=now,
            last_scraped_at=now,
        )
        db.add(listing)
        db.flush()
        # Use listing creation date as the timestamp of the first price point
        initial_date = listing_data.get("first_seen_at") or now
        db.add(PriceHistory(
            listing_id=listing.id,
            price=new_price,
            currency=listing_data.get("currency", "USD"),
            recorded_at=initial_date,
            change_pct=0.0,
        ))
        existing = listing

    db.commit()
    return existing


async def scrape_single_url(url: str) -> Optional[dict]:
    """
    Scrape a single listing URL and return a listing dict ready for upsert_listing().
    Supports: myhome.ge, ss.ge, maltapark.com, century21albania.com, tranio.com,
    idealista.com, fotocasa.es, spitogatos.gr, rightmove.co.uk, immobiliare.it,
    seloger.com, immowelt.de, otodom.pl, daft.ie, + any portal with JSON-LD or OG tags.
    """
    from urllib.parse import urlparse

    parsed  = urlparse(url)
    host    = parsed.netloc.lower().lstrip("www.")
    now     = datetime.utcnow()

    # ── portal routing ──────────────────────────────────────────────────────────
    if "myhome.ge" in host:
        portal, country, city = "myhome.ge", "Georgia", "Batumi"
    elif "ss.ge" in host:
        portal, country, city = "ss.ge", "Georgia", "Batumi"
    elif "maltapark.com" in host:
        portal, country, city = "maltapark.com", "Malta", "Malta"
    elif "century21albania.com" in host or "c21albania" in host:
        portal, country, city = "century21albania.com", "Albania", "Tirana"
    elif "tranio.com" in host:
        portal = "tranio.com"
        if "/greece" in url:
            country, city = "Greece", "Greece"
        elif "/spain" in url:
            country, city = "Spain", "Spain"
        else:
            country, city = "Unknown", "Unknown"
    elif "idealista.com" in host or "fotocasa.es" in host:
        portal, country, city = host, "Spain", "Spain"
    elif "spitogatos.gr" in host or "xe.gr" in host:
        portal, country, city = host, "Greece", "Greece"
    elif "rightmove.co.uk" in host or "zoopla.co.uk" in host:
        portal, country, city = host, "United Kingdom", "UK"
    elif "immobiliare.it" in host:
        portal, country, city = host, "Italy", "Italy"
    elif "seloger.com" in host or "leboncoin.fr" in host:
        portal, country, city = host, "France", "France"
    elif "immowelt.de" in host or "immoscout24.de" in host:
        portal, country, city = host, "Germany", "Germany"
    elif "otodom.pl" in host or "olx.pl" in host:
        portal, country, city = host, "Poland", "Poland"
    elif "daft.ie" in host or "myhome.ie" in host:
        portal, country, city = host, "Ireland", "Ireland"
    elif "sreality.cz" in host:
        portal, country, city = host, "Czech Republic", "Czech Republic"
    elif "ingatlan.com" in host:
        portal, country, city = host, "Hungary", "Hungary"
    elif "imovirtual.com" in host or "casa.pt" in host:
        portal, country, city = host, "Portugal", "Portugal"
    elif "property24.com" in host:
        portal, country, city = host, "South Africa", "South Africa"
    else:
        portal, country, city = host or "custom", "Unknown", "Unknown"

    headers = {**HEADERS, "Referer": f"https://{host}/"}
    async with httpx.AsyncClient(
        headers=headers, timeout=20, follow_redirects=True,
        limits=httpx.Limits(max_connections=5),
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"scrape_single_url: failed to fetch {url}: {exc}")
            return None

        html   = resp.text
        soup   = BeautifulSoup(html, "html.parser")

        # ── 1. Try JSON-LD ────────────────────────────────────────────────────
        listing_data: Optional[dict] = None

        ld_scripts = [s.get_text() for s in soup.find_all("script", type="application/ld+json")]
        _RE_PROP = ("RealEstate", "Apartment", "House", "Residence", "Villa",
                    "Product", "Offer", "SingleFamilyResidence", "Accommodation")
        for raw in ld_scripts:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            # flatten: handle list, @graph wrapper, or bare object
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict) and "@graph" in data:
                candidates = data["@graph"]
            else:
                candidates = [data]
            for ld in candidates:
                t = ld.get("@type", "")
                # @type can be a string OR a list (e.g. ["Product","Apartment"])
                type_str = " ".join(t) if isinstance(t, list) else str(t)
                if any(x in type_str for x in _RE_PROP):
                    listing_data = ld
                    break
            if listing_data:
                break

        if listing_data:
            # extract price
            price, currency = None, "EUR"
            offers = listing_data.get("offers") or listing_data.get("price")
            if isinstance(offers, dict):
                price    = _safe_float(offers.get("price") or offers.get("lowPrice"))
                currency = offers.get("priceCurrency", "EUR")
            elif isinstance(offers, (int, float, str)):
                price = _safe_float(offers)
            if price is None:
                price = _safe_float(listing_data.get("price"))

            # extract title
            title = (
                listing_data.get("name")
                or listing_data.get("headline")
                or soup.find("title") and soup.find("title").get_text(strip=True)
            )

            # extract images
            imgs: list[str] = []
            raw_imgs = listing_data.get("image") or []
            if isinstance(raw_imgs, str):
                raw_imgs = [raw_imgs]
            elif isinstance(raw_imgs, dict):
                raw_imgs = [raw_imgs.get("url") or raw_imgs.get("contentUrl") or ""]
            # each item may be a string, {"url":...}, or {"contentUrl":...}
            imgs = []
            for i in raw_imgs:
                if not i:
                    continue
                if isinstance(i, dict):
                    u = i.get("url") or i.get("contentUrl") or i.get("thumbnail") or ""
                    if u:
                        imgs.append(u)
                elif isinstance(i, str) and i.startswith("http"):
                    imgs.append(i)

            # extract rooms / area
            rooms    = _safe_int(listing_data.get("numberOfRooms") or listing_data.get("numberOfBedrooms"))
            area_sqm = _safe_float(
                listing_data.get("floorSize", {}).get("value") if isinstance(listing_data.get("floorSize"), dict)
                else listing_data.get("floorSize") or listing_data.get("area")
            )

            # extract address for city
            addr = listing_data.get("address") or {}
            if isinstance(addr, dict):
                city = addr.get("addressLocality") or addr.get("addressRegion") or city
                # override country from portal detection if address says something
                _addr_country = addr.get("addressCountry") or ""
                _COUNTRY_MAP = {
                    "pl": "Poland", "polska": "Poland", "poland": "Poland",
                    "de": "Germany", "deutschland": "Germany",
                    "fr": "France", "france": "France",
                    "es": "Spain", "españa": "Spain",
                    "it": "Italy", "italia": "Italy",
                    "pt": "Portugal",
                    "gr": "Greece", "ελλάδα": "Greece",
                    "mt": "Malta",
                    "al": "Albania",
                    "ge": "Georgia",
                    "cz": "Czech Republic",
                    "hu": "Hungary",
                    "ie": "Ireland",
                    "gb": "United Kingdom", "uk": "United Kingdom",
                }
                _key = _addr_country.lower().strip()
                if _key in _COUNTRY_MAP:
                    country = _COUNTRY_MAP[_key]

        else:
            # ── 2. Fallback: OG / meta tags ───────────────────────────────────
            def meta(prop: str) -> Optional[str]:
                t = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                return t["content"].strip() if t and t.get("content") else None

            title    = meta("og:title") or (soup.find("title") and soup.find("title").get_text(strip=True))
            og_img   = meta("og:image")
            imgs     = [og_img] if og_img else []
            price    = _safe_float(meta("product:price:amount") or meta("og:price:amount"))
            currency = meta("product:price:currency") or meta("og:price:currency") or "EUR"
            rooms    = None
            area_sqm = None

        if price is None:
            # last-ditch: scan visible text for price pattern
            text_blob = soup.get_text(" ", strip=True)
            m = re.search(r"[\$€£]\s*([\d,\.]+)", text_blob)
            if m:
                price = _safe_float(m.group(1).replace(",", ""))

        if price is None:
            logger.warning(f"scrape_single_url: no price found at {url}")
            return None

        # dedupe images
        seen_img: set = set()
        clean_imgs: list[str] = []
        for i in imgs:
            if i and i not in seen_img:
                seen_img.add(i)
                clean_imgs.append(i)

        external_id = re.sub(r"[^a-zA-Z0-9]", "_", url)[:180]

        # ── Extract listing creation date ─────────────────────────────────
        first_seen: Optional[datetime] = None

        # 1) JSON-LD datePosted / dateCreated
        if listing_data:
            for date_field in ("datePosted", "dateCreated", "uploadDate", "datePublished"):
                raw_date = listing_data.get(date_field)
                if raw_date:
                    first_seen = _parse_iso_date(raw_date)
                    if first_seen:
                        break

        # 2) __NEXT_DATA__ (otodom, olx, idealista-style Next.js portals)
        if not first_seen:
            nd_match = re.search(
                r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                html, re.DOTALL
            )
            if nd_match:
                try:
                    nd = json.loads(nd_match.group(1))
                    # walk common paths
                    def _nd_get(obj, *keys):
                        for k in keys:
                            if not isinstance(obj, dict):
                                return None
                            obj = obj.get(k)
                        return obj
                    for path in [
                        ("props","pageProps","ad","createdAt"),
                        ("props","pageProps","listing","createdAt"),
                        ("props","pageProps","property","createdAt"),
                        ("props","pageProps","ad","dateCreated"),
                        ("props","pageProps","advert","datePublished"),
                        ("props","pageProps","data","listing","createdAt"),
                    ]:
                        val = _nd_get(nd, *path)
                        if val:
                            first_seen = _parse_iso_date(str(val))
                            if first_seen:
                                break
                except Exception:
                    pass

        # 3) <meta property="article:published_time" ...>
        if not first_seen:
            m = re.search(
                r'<meta[^>]+(?:article:published_time|datePublished)[^>]+content=["\']([^"\']+)["\']',
                html, re.I
            )
            if m:
                first_seen = _parse_iso_date(m.group(1))

        # 4) <time datetime="..."> first occurrence
        if not first_seen:
            m = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', html)
            if m:
                first_seen = _parse_iso_date(m.group(1))

        return {
            "external_id":  external_id,
            "portal":       portal,
            "title":        str(title)[:300] if title else url[:120],
            "url":          url,
            "city":         city,
            "country":      country,
            "price":        price,
            "currency":     currency,
            "area_sqm":     area_sqm,
            "rooms":        rooms,
            "images":       clean_imgs[:12],
            "image_url":    clean_imgs[0] if clean_imgs else None,
            "first_seen_at": first_seen,   # None → upsert_listing uses now()
        }


def _parse_iso_date(s: str) -> Optional[datetime]:
    """Parse ISO-8601 date/datetime string, return datetime or None."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19].replace("Z", ""), fmt[:len(s[:19])])
            # reject obviously wrong dates (future or before 2000)
            if datetime(2000, 1, 1) < dt < datetime.utcnow() + timedelta(days=1):
                return dt
        except ValueError:
            continue
    return None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return None


def _safe_int(v) -> Optional[int]:
    f = _safe_float(v)
    return int(f) if f is not None else None




async def run_scrape_all() -> None:
    logger.info("=== Scrape run started ===")
    all_listings: list[dict] = []

    for fn, name in [
        (scrape_myhome_batumi,  "myhome.ge"),
        (scrape_ss_batumi,      "ss.ge"),
        (scrape_maltapark,      "maltapark.com"),
        (scrape_c21_albania,    "century21albania.com"),
        (scrape_tranio_greece,  "tranio.com (Greece)"),
        (scrape_tranio_spain,   "tranio.com (Spain)"),
    ]:
        try:
            items = await fn()
            logger.info(f"{name}: {len(items)} listings scraped")
            all_listings.extend(items)
        except Exception as exc:
            logger.error(f"{name} scrape failed: {exc}", exc_info=True)

    db = SessionLocal()
    try:
        saved = sum(1 for d in all_listings if upsert_listing(db, d) is not None)
        logger.info(f"=== Scrape complete: {saved}/{len(all_listings)} listings saved ===")

        # ── Re-scrape all user-tracked URLs (those with active email alerts) ──
        tracked_ids = (
            db.query(EmailAlert.listing_id)
            .filter(EmailAlert.is_active == True)
            .distinct()
            .all()
        )
        if tracked_ids:
            id_list = [row[0] for row in tracked_ids]
            tracked_listings = db.query(Listing).filter(Listing.id.in_(id_list)).all()
            logger.info(f"Re-scraping {len(tracked_listings)} user-tracked listing(s)…")
            for listing in tracked_listings:
                try:
                    fresh = await scrape_single_url(listing.url)
                    if fresh:
                        fresh["external_id"] = listing.external_id  # keep same DB record
                        upsert_listing(db, fresh)
                except Exception as exc:
                    logger.warning(f"Re-scrape failed for {listing.url}: {exc}")
    finally:
        db.close()

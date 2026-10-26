"""
Mini-Pupillage Scraper
======================
Scrapes London chambers websites and aggregated listings for mini-pupillage
openings and inserts new entries into Supabase.

Run via GitHub Actions (reads SUPABASE_URL and SUPABASE_KEY from environment).
"""

import os
import re
import sys
import time
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ——— Logging ———
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

# ——— Supabase connection ———
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    log.error("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")
    sys.exit(1)

db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ——— HTTP headers ———
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

REQUEST_TIMEOUT = 18   # seconds per request
POLITE_DELAY    = 1.5  # seconds between requests to the same domain


# ============================================================
#  HTTP helpers
# ============================================================

def get_soup(url: str, timeout: int = REQUEST_TIMEOUT) -> BeautifulSoup | None:
    """Fetch URL and return BeautifulSoup, or None on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as e:
        log.warning("  HTTP error fetching %s: %s", url, e)
        return None


def make_absolute(href: str, base_url: str) -> str:
    """Convert a possibly-relative URL to absolute."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(base_url, href)


# ============================================================
#  Date extraction
# ============================================================

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

DATE_PATTERNS = [
    # 15 January 2025 / 15th January 2025
    re.compile(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+(\d{4})\b",
        re.IGNORECASE,
    ),
    # January 15 2025 / January 15, 2025
    re.compile(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        re.IGNORECASE,
    ),
    # ISO 2025-01-15
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    # DD/MM/YYYY
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
]


def parse_date_from_match(m, pattern_index: int) -> str | None:
    """Convert a regex match to YYYY-MM-DD string."""
    try:
        if pattern_index == 0:
            day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            month = MONTHS.get(month_str)
            if month:
                return datetime(year, month, day).strftime("%Y-%m-%d")
        elif pattern_index == 1:
            month_str, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            month = MONTHS.get(month_str)
            if month:
                return datetime(year, month, day).strftime("%Y-%m-%d")
        elif pattern_index == 2:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(year, month, day).strftime("%Y-%m-%d")
        elif pattern_index == 3:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        pass
    return None


def extract_date_from_text(text: str) -> str | None:
    """Search a text block for any recognisable date and return ISO string."""
    for idx, pattern in enumerate(DATE_PATTERNS):
        for m in pattern.finditer(text):
            result = parse_date_from_match(m, idx)
            if result:
                # Ignore dates that are obviously years without months (artefacts)
                return result
    return None


def extract_deadline_from_soup(soup: BeautifulSoup) -> str | None:
    """
    Look for deadline/closing-date context in a page and extract a date.
    Searches a window of ~150 chars following deadline-related keywords.
    """
    deadline_keywords = [
        "deadline", "closing date", "applications close", "apply by",
        "apply before", "close of applications", "applications must be",
        "by no later than", "submission deadline", "application window closes",
    ]
    full_text = soup.get_text(separator=" ", strip=True)

    for kw in deadline_keywords:
        idx = full_text.lower().find(kw)
        while idx != -1:
            snippet = full_text[max(0, idx - 10) : idx + 160]
            d = extract_date_from_text(snippet)
            if d:
                return d
            idx = full_text.lower().find(kw, idx + 1)

    # Fallback: search whole page for any date
    return extract_date_from_text(full_text)


# ============================================================
#  Practice area detection
# ============================================================

# Only these six practice areas are tracked. Each regex maps page text onto one label.
TARGET_AREAS = [
    ("Civil Litigation",       re.compile(r"\bcivil\b(?!\s+liberties)|litigation|dispute resolution|commercial (?:law|litigation|disputes)|professional liability|insurance|construction|banking|arbitration", re.I)),
    ("Corporate Transactions", re.compile(r"corporate|company law|insolvency|restructuring|mergers", re.I)),
    ("Commercial Property",    re.compile(r"commercial property|real estate|real property|landlord", re.I)),
    ("Immigration",            re.compile(r"immigration|asylum", re.I)),
    ("Tax",                    re.compile(r"\btax(?:ation)?\b|\bvat\b", re.I)),
    ("Chancery",               re.compile(r"chancery|\btrusts?\b|probate|pensions", re.I)),
]


def detect_practice_areas(soup: BeautifulSoup, fallback: str = "") -> str:
    """
    Detect target practice areas mentioned in page text and merge them with the
    chambers' default areas. Returns a comma-separated list of target labels only.
    """
    text  = soup.get_text(" ")
    found = [label for label, rx in TARGET_AREAS if rx.search(text)]
    defaults = [a.strip() for a in (fallback or "").split(",") if a.strip()]
    merged = defaults + [a for a in found if a not in defaults]
    return ", ".join(merged)


def has_target_area(practice_areas: str | None) -> bool:
    """True if the practice-area string contains at least one target label."""
    if not practice_areas:
        return False
    return any(label in practice_areas for label, _ in TARGET_AREAS)


# ============================================================
#  Mini-pupillage page finder
# ============================================================

MINI_PUP_KEYWORDS = [
    "mini-pupillage", "mini pupillage", "minipupillage",
    "mini_pupillage", "mini pup",
]


def find_mini_pup_url(base_url: str, soup: BeautifulSoup) -> str | None:
    """
    Search a homepage soup for a link that leads to a mini-pupillage page.
    Returns the first matching absolute URL, or None.
    """
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").lower()
        text = a.get_text().lower()
        for kw in MINI_PUP_KEYWORDS:
            if kw in href or kw in text:
                return make_absolute(a["href"], base_url)
    return None


# ============================================================
#  Supabase helpers
# ============================================================

def opening_exists(chambers_name: str, deadline: str | None) -> bool:
    """Check whether an opening with this chambers name and deadline already exists."""
    query = db.table("openings").select("id").eq("chambers_name", chambers_name)
    if deadline:
        query = query.eq("deadline", deadline)
    else:
        query = query.is_("deadline", "null")
    result = query.limit(1).execute()
    return len(result.data) > 0


def insert_opening(entry: dict) -> bool:
    """
    Insert an opening if it does not already exist.
    Returns True if a new row was inserted, False if it already existed.
    """
    if opening_exists(entry["chambers_name"], entry.get("deadline")):
        return False
    db.table("openings").insert(entry).execute()
    return True


# ============================================================
#  Per-chambers scraper
# ============================================================

def scrape_chambers(name: str, base_url: str, default_areas: str) -> dict | None:
    """
    Attempt to find and parse a mini-pupillage page for a given chambers.
    Returns a dict suitable for insertion into the openings table, or None.
    """
    log.info("  Scraping: %s", name)

    # Step 1: fetch homepage and look for a mini-pup link
    homepage_soup = get_soup(base_url)
    if homepage_soup is None:
        log.warning("  Could not fetch homepage for %s", name)
        return None

    mp_url  = find_mini_pup_url(base_url, homepage_soup)
    mp_soup = None

    if mp_url and mp_url != base_url:
        log.info("  Found mini-pup page: %s", mp_url)
        time.sleep(POLITE_DELAY)
        mp_soup = get_soup(mp_url)

    # Step 2: use mini-pup page if found, otherwise fall back to homepage
    active_soup = mp_soup or homepage_soup
    active_url  = mp_url  or base_url

    # Check if mini-pupillage is mentioned at all
    page_text = active_soup.get_text().lower()
    if not any(kw in page_text for kw in MINI_PUP_KEYWORDS):
        # Nothing found — only add if we have a direct mini-pup URL
        if mp_url is None:
            log.info("  No mini-pupillage content found for %s — skipping", name)
            return None
        # If we followed a specific page URL, include it even without content match

    deadline = extract_deadline_from_soup(active_soup)
    areas    = detect_practice_areas(active_soup, fallback=default_areas)

    note = None
    if mp_soup is None:
        note = "Mini-pupillage mentioned on site — check directly for current details."

    return {
        "chambers_name":  name,
        "website":        active_url,
        "deadline":       deadline,
        "practice_areas": areas or default_areas,
        "location":       "London",
        "notes":          note,
        "source":         "auto",
    }


# ============================================================
#  Source 1: Pupillage Gateway
# ============================================================

def scrape_pupillage_gateway() -> list[dict]:
    log.info("\n=== Source 1: Pupillage Gateway ===")
    results = []

    # Try the vacancy listings page
    urls_to_try = [
        "https://www.pupillagegateway.com/students/pupillage-vacancies/",
        "https://www.pupillagegateway.com/vacancies/",
        "https://www.pupillagegateway.com/",
    ]

    for url in urls_to_try:
        soup = get_soup(url)
        if soup is None:
            continue

        page_text = soup.get_text().lower()
        if "mini" not in page_text:
            continue

        # Look for listing containers
        listing_selectors = [
            soup.find_all("article"),
            soup.find_all("div", class_=re.compile(r"vacanc|listing|opportunit|card", re.I)),
            soup.find_all("li",  class_=re.compile(r"vacanc|listing|opportunit", re.I)),
        ]

        for listings in listing_selectors:
            for item in listings:
                item_text = item.get_text().lower()
                if not any(kw in item_text for kw in MINI_PUP_KEYWORDS):
                    continue

                # Try to extract a chambers name
                name_el = item.find(["h2", "h3", "h4", "strong", "a"])
                if not name_el:
                    continue
                chambers_name = name_el.get_text(strip=True)
                if not chambers_name or len(chambers_name) > 120:
                    continue

                link_el = item.find("a", href=True)
                link    = make_absolute(link_el["href"], url) if link_el else url

                deadline = extract_deadline_from_soup(
                    BeautifulSoup(str(item), "html.parser")
                )

                results.append({
                    "chambers_name":  chambers_name,
                    "website":        link,
                    "deadline":       deadline,
                    "practice_areas": detect_practice_areas(BeautifulSoup(str(item), "html.parser")) or None,
                    "location":       "London",
                    "notes":          "Found via Pupillage Gateway listing",
                    "source":         "auto",
                })

        if results:
            break

    log.info("  Pupillage Gateway: %d candidate entries found", len(results))
    return results


# ============================================================
#  Source 2: Bar Council mini-pupillage page
# ============================================================

def scrape_bar_council() -> list[dict]:
    log.info("\n=== Source 2: Bar Council ===")
    results = []

    url  = "https://www.barcouncil.org.uk/becoming-a-barrister/mini-pupillages.html"
    soup = get_soup(url)
    if soup is None:
        log.warning("  Could not fetch Bar Council page")
        return results

    chambers_keywords = [
        "chambers", "court", "buildings", "square", "row", "garden",
        "temple", "bench", "gray", "lincoln", "essex", "field",
    ]

    seen_names: set[str] = set()

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]

        if not text or len(text) < 4 or len(text) > 130:
            continue
        if not any(kw in text.lower() for kw in chambers_keywords):
            continue
        if text in seen_names:
            continue

        seen_names.add(text)
        abs_href = make_absolute(href, url) if not href.startswith("http") else href

        results.append({
            "chambers_name":  text,
            "website":        abs_href,
            "deadline":       None,
            "practice_areas": None,
            "location":       "London",
            "notes":          "Listed on Bar Council mini-pupillage page",
            "source":         "auto",
        })

    log.info("  Bar Council: %d candidate entries found", len(results))
    return results


# ============================================================
#  Source 3: Individual chambers
# ============================================================

# (name, base_url, default_practice_areas)
CHAMBERS = [
    # ——— Chancery ———
    ("Serle Court",                "https://www.serlecourt.co.uk",           "Chancery, Civil Litigation, Corporate Transactions"),
    ("Wilberforce Chambers",       "https://www.wilberforce.co.uk",          "Chancery, Commercial Property, Tax, Civil Litigation"),
    ("Maitland Chambers",          "https://www.maitlandchambers.com",       "Chancery, Commercial Property, Corporate Transactions, Civil Litigation"),
    ("3 Stone Buildings",          "https://www.3stonebuildings.com",        "Chancery, Corporate Transactions, Civil Litigation"),
    ("4 Stone Buildings",          "https://www.4stonebuildings.com",        "Chancery, Corporate Transactions, Civil Litigation"),
    ("5 Stone Buildings",          "https://www.5sblaw.com",                 "Chancery, Tax"),
    ("New Square Chambers",        "https://www.newsquarechambers.co.uk",    "Chancery, Commercial Property, Civil Litigation"),
    ("XXIV Old Buildings",         "https://www.xxiv.co.uk",                 "Chancery, Civil Litigation, Tax"),
    ("Radcliffe Chambers",         "https://www.radcliffechambers.com",      "Chancery, Commercial Property, Tax, Civil Litigation"),
    ("Ten Old Square",             "https://www.tenoldsquare.com",           "Chancery, Tax"),
    ("Enterprise Chambers",        "https://www.enterprisechambers.com",     "Chancery, Commercial Property, Corporate Transactions, Civil Litigation"),
    ("4 New Square",               "https://www.4newsquare.com",             "Civil Litigation, Chancery"),
    # ——— Corporate ———
    ("Erskine Chambers",           "https://www.erskinechambers.com",        "Corporate Transactions, Chancery, Civil Litigation"),
    ("South Square",               "https://www.southsquare.com",            "Corporate Transactions, Civil Litigation"),
    ("3 Verulam Buildings",        "https://www.3vb.com",                    "Civil Litigation, Corporate Transactions"),
    ("Fountain Court Chambers",    "https://www.fountaincourt.co.uk",        "Civil Litigation, Corporate Transactions"),
    ("One Essex Court",            "https://www.oeclaw.co.uk",               "Civil Litigation, Corporate Transactions"),
    # ——— Commercial Property ———
    ("Falcon Chambers",            "https://www.falcon-chambers.com",        "Commercial Property, Civil Litigation"),
    ("Tanfield Chambers",          "https://www.tanfieldchambers.co.uk",     "Commercial Property, Civil Litigation"),
    ("Lamb Chambers",              "https://www.lambchambers.co.uk",         "Commercial Property, Chancery, Civil Litigation"),
    ("Hardwicke",                  "https://www.hardwicke.co.uk",            "Commercial Property, Chancery, Civil Litigation"),
    ("Landmark Chambers",          "https://www.landmarkchambers.co.uk",     "Commercial Property, Immigration, Civil Litigation"),
    ("Cornerstone Barristers",     "https://www.cornerstonebarristers.com",  "Commercial Property, Civil Litigation"),
    ("42 Bedford Row",             "https://www.42br.com",                   "Commercial Property, Civil Litigation"),
    # ——— Tax ———
    ("Pump Court Tax Chambers",    "https://www.pumptax.com",                "Tax"),
    ("Gray's Inn Tax Chambers",    "https://www.taxbar.com",                 "Tax"),
    ("Monckton Chambers",          "https://www.monckton.com",               "Tax, Civil Litigation"),
    ("Devereux Chambers",          "https://www.devchambers.co.uk",          "Tax, Civil Litigation"),
    # ——— Civil Litigation (commercial and general civil) ———
    ("Essex Court Chambers",       "https://www.essexcourt.com",             "Civil Litigation"),
    ("Brick Court Chambers",       "https://www.brickcourt.co.uk",           "Civil Litigation"),
    ("20 Essex Street",            "https://www.20essexst.com",              "Civil Litigation"),
    ("Blackstone Chambers",        "https://www.blackstonechambers.com",     "Civil Litigation"),
    ("39 Essex Chambers",          "https://www.39essex.com",                "Civil Litigation"),
    ("Outer Temple Chambers",      "https://www.outertemple.com",            "Civil Litigation"),
    ("Henderson Chambers",         "https://www.hendersonchambers.co.uk",    "Civil Litigation"),
    ("Crown Office Chambers",      "https://www.crownoffice.com",            "Civil Litigation"),
    ("4 Pump Court",               "https://www.4pumpcourt.com",             "Civil Litigation"),
    ("Keating Chambers",           "https://www.keatingchambers.com",        "Civil Litigation"),
    ("Littleton Chambers",         "https://www.littletonchambers.com",      "Civil Litigation"),
    # ——— Employment ———
    ("Cloisters",                  "https://www.cloisters.com",              "Employment, Discrimination, Human Rights"),
    ("Old Square Chambers",        "https://www.oldsquare.co.uk",            "Employment, Personal Injury"),
    ("Devereux Chambers",          "https://www.devchambers.co.uk",          "Employment, Personal Injury, Tax"),
    ("5 Essex Court",              "https://www.5essexcourt.co.uk",          "Employment, Regulatory, Police Law"),
    # ——— Mixed / General Common Law ———
    ("Keating Chambers",           "https://www.keatingchambers.com",        "Construction, Engineering, Technology"),
    ("Atkin Chambers",             "https://www.atkinchambers.com",          "Construction, Engineering, Energy"),
    ("4 Pump Court",               "https://www.4pumpcourt.com",             "Construction, Commercial, Insurance"),
    ("Guildhall Chambers",         "https://www.guildhallchambers.co.uk",    "Criminal, Civil, Employment"),
    ("St Ives Chambers",           "https://www.stiveschambers.co.uk",       "Criminal, Family, Civil"),
    ("Kings Chambers",             "https://www.kingschambers.com",          "Commercial, Employment, Personal Injury"),
    ("Exchange Chambers",          "https://www.exchangechambers.co.uk",     "Criminal, Civil, Employment, Family"),
    ("Parklane Plowden",           "https://www.parklaneplowden.co.uk",      "Personal Injury, Clinical Negligence, Employment"),
    ("St Philips Chambers",        "https://www.st-philips.com",             "Commercial, Criminal, Employment, Family"),
    ("Trinity Chambers",           "https://www.trinitychambers.co.uk",      "Criminal, Family, Civil"),
    ("Arden Chambers",             "https://www.ardenchambers.com",          "Housing, Local Government, Planning"),
    ("Civitas Law",                "https://www.civitaslaw.com",             "Criminal, Civil"),
    ("Apex Chambers",              "https://www.apexchambers.co.uk",         "Criminal, Civil, Family"),
    # ——— Immigration ———
    ("Garden Court Chambers",      "https://www.gclaw.co.uk",                "Immigration"),
    ("Doughty Street Chambers",    "https://www.doughtystreet.co.uk",        "Immigration, Civil Litigation"),
    ("Goldsmith Chambers",         "https://www.goldsmithchambers.com",      "Immigration, Civil Litigation"),
    ("No5 Chambers",               "https://www.no5.com",                    "Immigration, Civil Litigation"),
    ("Lamb Building",              "https://www.lambbuilding.co.uk",         "Immigration"),
    ("Field Court Chambers",       "https://www.fieldcourt.co.uk",           "Immigration"),
    ("1 Pump Court",               "https://www.1pumpcourt.co.uk",           "Immigration"),
]
   

# ============================================================
#  Main
# ============================================================

def main() -> None:
    new_count    = 0
    skip_count   = 0
    error_count  = 0
    all_entries: list[dict] = []

    # ——— Source 1: Pupillage Gateway ———
    try:
        entries = scrape_pupillage_gateway()
        all_entries.extend(entries)
    except Exception as exc:
        log.error("Pupillage Gateway scrape failed entirely: %s", exc)
        error_count += 1

    # ——— Source 2: Bar Council ———
    try:
        entries = scrape_bar_council()
        all_entries.extend(entries)
    except Exception as exc:
        log.error("Bar Council scrape failed entirely: %s", exc)
        error_count += 1

    # ——— Source 3: Individual chambers ———
    log.info("\n=== Source 3: Individual Chambers (%d total) ===", len(CHAMBERS))
    for (name, url, areas) in CHAMBERS:
        try:
            time.sleep(POLITE_DELAY)
            entry = scrape_chambers(name, url, areas)
            if entry:
                all_entries.append(entry)
        except Exception as exc:
            log.error("  %s failed: %s", name, exc)
            error_count += 1

    # ——— Insert new entries into Supabase ———
    log.info("\n=== Inserting into Supabase ===")
       default_areas = {name.lower(): areas for (name, _url, areas) in CHAMBERS}
    for entry in all_entries:
        if not entry.get("chambers_name"):
            continue
        # Gateway / Bar Council entries carry no areas: borrow them from CHAMBERS if known
        if not entry.get("practice_areas"):
            entry["practice_areas"] = default_areas.get(entry["chambers_name"].lower())
        if not has_target_area(entry.get("practice_areas")):
            log.info("  – Not a target practice area, skipping: %s", entry["chambers_name"])
            skip_count += 1
            continue
            inserted = insert_opening(entry)
            if inserted:
                log.info("  ✓ Inserted: %s (deadline: %s)", entry["chambers_name"], entry.get("deadline"))
                new_count += 1
            else:
                log.info("  – Already exists: %s", entry["chambers_name"])
                skip_count += 1
        except Exception as exc:
            log.error("  ✗ Error inserting '%s': %s", entry.get("chambers_name", "?"), exc)
            error_count += 1

    # ——— Summary ———
    log.info("\n%s", "=" * 55)
    log.info("Scrape complete.")
    log.info("  New openings inserted : %d", new_count)
    log.info("  Already existed       : %d", skip_count)
    log.info("  Errors                : %d", error_count)
    log.info("  Total candidates      : %d", len(all_entries))
    log.info("%s", "=" * 55)


if __name__ == "__main__":
    main()

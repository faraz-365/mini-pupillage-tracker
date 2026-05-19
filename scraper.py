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

AREA_KEYWORDS = [
    ("criminal",              "Criminal"),
    ("serious crime",         "Criminal"),
    ("financial crime",       "Financial Crime"),
    ("regulatory",            "Regulatory"),
    ("family",                "Family"),
    ("employment",            "Employment"),
    ("discrimination",        "Employment"),
    ("immigration",           "Immigration"),
    ("asylum",                "Immigration"),
    ("public law",            "Public Law"),
    ("judicial review",       "Public Law"),
    ("human rights",          "Human Rights"),
    ("civil liberties",       "Civil Liberties"),
    ("planning",              "Planning"),
    ("environment",           "Environmental"),
    ("commercial",            "Commercial"),
    ("chancery",              "Chancery"),
    ("company law",           "Company Law"),
    ("insolvency",            "Insolvency"),
    ("banking",               "Banking & Finance"),
    ("competition",           "Competition"),
    ("intellectual property", "Intellectual Property"),
    ("personal injury",       "Personal Injury"),
    ("clinical negligence",   "Clinical Negligence"),
    ("medical negligence",    "Clinical Negligence"),
    ("construction",          "Construction"),
    ("shipping",              "Shipping"),
    ("arbitration",           "Arbitration"),
    ("housing",               "Housing"),
    ("tax",                   "Tax"),
    ("pensions",              "Pensions"),
    ("costs",                 "Costs"),
]


def detect_practice_areas(soup: BeautifulSoup, fallback: str = "") -> str:
    """Detect practice areas mentioned in page text."""
    text  = soup.get_text().lower()
    found = []
    seen  = set()
    for kw, label in AREA_KEYWORDS:
        if kw in text and label not in seen:
            found.append(label)
            seen.add(label)
    if found:
        return ", ".join(found[:6])
    return fallback


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
                    "practice_areas": None,
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
    # ——— Public Law / Human Rights / Civil Liberties ———
    ("Doughty Street Chambers",    "https://www.doughtystreet.co.uk",        "Public Law, Human Rights, Civil Liberties, Criminal"),
    ("Matrix Chambers",            "https://www.matrixlaw.co.uk",            "Public Law, Human Rights, Commercial"),
    ("Garden Court Chambers",      "https://www.gclaw.co.uk",                "Public Law, Criminal, Immigration, Family"),
    ("Landmark Chambers",          "https://www.landmarkchambers.co.uk",     "Planning, Public Law, Environmental"),
    ("Brick Court Chambers",       "https://www.brickcourt.co.uk",           "Commercial, Public Law, European Law"),
    ("Blackstone Chambers",        "https://www.blackstonechambers.com",     "Public Law, Commercial, Employment, Human Rights"),
    ("11KBW",                      "https://www.11kbw.com",                  "Public Law, Employment, Information Law"),
    ("Monckton Chambers",          "https://www.monckton.com",               "Public Law, Competition, Commercial, Tax"),
    ("Cornerstone Barristers",     "https://www.cornerstonebarristers.com",  "Planning, Public Law, Housing, Local Government"),
    ("Francis Taylor Building",    "https://www.ftbchambers.co.uk",          "Planning, Environmental, Public Law"),
    ("39 Essex Chambers",          "https://www.39essex.com",                "Public Law, Commercial, Personal Injury, Health"),
    ("1 Crown Office Row",         "https://www.1cor.com",                   "Personal Injury, Clinical Negligence, Human Rights"),
    ("Henderson Chambers",         "https://www.hendersonchambers.co.uk",    "Personal Injury, Product Liability, Commercial"),
    ("Pump Court Chambers",        "https://www.pumpcourt.com",              "Criminal, Family, Civil"),
    # ——— Commercial / Chancery ———
    ("Outer Temple Chambers",      "https://www.outertemple.com",            "Commercial, Employment, Personal Injury"),
    ("3 Verulam Buildings",        "https://www.3vb.com",                    "Commercial, Banking & Finance, Arbitration"),
    ("Essex Court Chambers",       "https://www.essexcourt.com",             "Commercial, Shipping, Arbitration, Public Law"),
    ("20 Essex Street",            "https://www.20essexst.com",              "Commercial, Shipping, Arbitration"),
    ("One Essex Court",            "https://www.oeclaw.co.uk",               "Commercial, Competition, Arbitration"),
    ("Serle Court",                "https://www.serlecourt.co.uk",           "Chancery, Commercial, Trusts, Company Law"),
    ("Wilberforce Chambers",       "https://www.wilberforce.co.uk",          "Chancery, Trusts, Pensions, Commercial"),
    ("South Square",               "https://www.southsquare.com",            "Commercial, Insolvency, Company Law"),
    ("4 New Square",               "https://www.4newsquare.com",             "Commercial, Professional Liability, Insurance"),
    ("Fountain Court Chambers",    "https://www.fountaincourt.co.uk",        "Commercial, Banking & Finance, Insurance"),
    ("Crown Office Chambers",      "https://www.crownoffice.com",            "Personal Injury, Professional Liability, Insurance"),
    ("Littleton Chambers",         "https://www.littletonchambers.com",      "Employment, Commercial"),
    ("Maitland Chambers",          "https://www.maitlandchambers.com",       "Chancery, Commercial, Insolvency"),
    ("3 Stone Buildings",          "https://www.3stonebuildings.com",        "Chancery, Company Law, Trusts"),
    ("New Square Chambers",        "https://www.newsquarechambers.co.uk",    "Chancery, Intellectual Property, Tax"),
    ("Erskine Chambers",           "https://www.erskinechambers.com",        "Company Law, Commercial, Insolvency"),
    # ——— Criminal ———
    ("2 Hare Court",               "https://www.2harecourt.com",             "Criminal, Regulatory"),
    ("23 Essex Street",            "https://www.23es.com",                   "Criminal, Regulatory"),
    ("25 Bedford Row",             "https://www.25bedfordrow.com",           "Criminal, Family"),
    ("9 Bedford Row",              "https://www.9bedfordrow.co.uk",          "Criminal, Family, Immigration"),
    ("6KBW College Hill",          "https://www.6kbw.com",                   "Criminal, Regulatory, Financial Crime"),
    ("QEB Hollis Whiteman",        "https://www.qebholliswhiteman.co.uk",    "Criminal, Regulatory, Financial Crime"),
    ("Red Lion Chambers",          "https://www.redlionchambers.co.uk",      "Criminal, Civil Liberties"),
    ("Furnival Chambers",          "https://www.furnivallaw.co.uk",          "Criminal"),
    ("Charter Chambers",           "https://www.charterchambers.com",        "Criminal"),
    ("Temple Garden Chambers",     "https://www.tgchambers.com",             "Personal Injury, Clinical Negligence, Criminal"),
    # ——— Family ———
    ("1 Garden Court",             "https://www.1gc.com",                    "Family"),
    ("4 Paper Buildings",          "https://www.4pb.com",                    "Family, International Family"),
    ("14 Gray's Inn Square",       "https://www.14gis.co.uk",                "Family"),
    ("36 Family",                  "https://www.36family.co.uk",             "Family"),
    ("Harcourt Chambers",          "https://www.harcourtchambers.co.uk",     "Family, Civil"),
    ("Coram Chambers",             "https://www.coramchambers.co.uk",        "Family, Child Law"),
    ("42 Bedford Row",             "https://www.42br.com",                   "Family, Civil"),
    ("Field Court Chambers",       "https://www.fieldcourt.co.uk",           "Family, Public Law, Immigration"),
    # ——— Personal Injury / Clinical Negligence ———
    ("1 Chancery Lane",            "https://www.1chancerylane.com",          "Personal Injury, Clinical Negligence"),
    ("2 Temple Gardens",           "https://www.2tg.co.uk",                  "Personal Injury, Clinical Negligence"),
    ("12 King's Bench Walk",       "https://www.12kbw.co.uk",                "Personal Injury, Clinical Negligence"),
    ("Deans Court Chambers",       "https://www.deanscourt.co.uk",           "Personal Injury, Clinical Negligence, Criminal"),
    ("Hardwicke",                  "https://www.hardwicke.co.uk",            "Personal Injury, Construction, Property"),
    ("Zenith Chambers",            "https://www.zenithchambers.co.uk",       "Personal Injury, Clinical Negligence"),
    # ——— Employment ———
    ("Cloisters",                  "https://www.cloisters.com",              "Employment, Discrimination, Human Rights"),
    ("Old Square Chambers",        "https://www.oldsquare.co.uk",            "Employment, Personal Injury"),
    ("Devereux Chambers",          "https://www.devchambers.co.uk",          "Employment, Personal Injury, Tax"),
    ("5 Essex Court",              "https://www.5essexcourt.co.uk",          "Employment, Regulatory, Police Law"),
    # ——— Immigration / Asylum ———
    ("Goldsmith Chambers",         "https://www.goldsmithchambers.com",      "Immigration, Criminal, Family"),
    ("No5 Chambers",               "https://www.no5.com",                    "Immigration, Criminal, Civil"),
    ("Lamb Building",              "https://www.lambbuilding.co.uk",         "Immigration, Family"),
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
    for entry in all_entries:
        if not entry.get("chambers_name"):
            continue
        try:
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

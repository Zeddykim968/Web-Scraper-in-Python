"""
BuyRentKenya Property Scraper — KRPPI Data Collection
======================================================
Scrapes property-for-sale listings from buyrentkenya.com using plain HTTP
requests (no browser/Playwright required — the site serves data in HTML).

Filters listings whose datePublished falls between January 2025 and today.

Output CSV columns:
  Name, Type, Category, Price, Location, County,
  No. of Bedrooms, No. of Bathrooms, No. of Ensuite Bedrooms,
  Date, Floor_area_sqm, Land_Size, Elevator, Parking,
  Condition, DSQ, Floor_Number, URL

Usage:
  python buyrentkenya_scraper.py                        # 50 pages
  python buyrentkenya_scraper.py --pages 100            # more pages
  python buyrentkenya_scraper.py --resume               # resume existing CSV
  python buyrentkenya_scraper.py --output myfile.csv
"""

import re
import json
import time
import logging
import argparse
import urllib.request
import urllib.error
from html import unescape
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Constants 
BASE_URL = "https://www.buyrentkenya.com"
SEARCH_URL = f"{BASE_URL}/property-for-sale"
DATE_FROM = date(2025, 1, 1)
DATE_TO = date.today()
REQUEST_DELAY = 0.8  # seconds between requests (be polite)

KENYA_COUNTIES = [
    "Nairobi", "Mombasa", "Kisumu", "Nakuru", "Uasin Gishu", "Kiambu",
    "Machakos", "Kajiado", "Muranga", "Nyeri", "Meru", "Embu", "Kirinyaga",
    "Nyandarua", "Laikipia", "Samburu", "Isiolo", "Marsabit", "Mandera",
    "Wajir", "Garissa", "Tana River", "Kilifi", "Kwale", "Taita Taveta",
    "Lamu", "Trans Nzoia", "West Pokot", "Elgeyo Marakwet", "Nandi",
    "Baringo", "Kericho", "Bomet", "Narok", "Kisii", "Nyamira", "Migori",
    "Homa Bay", "Siaya", "Vihiga", "Kakamega", "Bungoma", "Busia",
    "Turkana", "Kitui", "Makueni"
]

OUTPUT_COLUMNS = [
    "Name", "Type", "Category", "Price", "Location", "County",
    "No. of Bedrooms", "No. of Bathrooms", "No. of Ensuite Bedrooms",
    "Date", "Floor_area_sqm", "Land_Size", "Elevator", "Parking",
    "Condition", "DSQ", "Floor_Number", "URL",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# HTTP helpers 

def fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            log.warning(f"HTTP {e.code} for {url} (attempt {attempt}/{retries})")
            if e.code in (403, 404, 410):
                return None
            time.sleep(2 * attempt)
        except Exception as e:
            log.warning(f"Error fetching {url}: {e} (attempt {attempt}/{retries})")
            time.sleep(2 * attempt)
    return None


# Parsing helpers 

def clean(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(text.strip().split()) or None


def extract_county(location: str | None) -> str | None:
    if not location:
        return None
    loc_lower = location.lower()
    for county in KENYA_COUNTIES:
        if county.lower() in loc_lower:
            return county
    return None


def parse_float(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", str(text).replace(",", ""))
    try:
        return float(m.group().replace(",", "")) if m else None
    except Exception:
        return None


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", str(text))
    return int(m.group()) if m else None


def accom_category_to_type(accom_cat: str | None) -> str:
    """Map JSON-LD accommodationCategory to preferred Type."""
    if not accom_cat:
        return "Residential"
    cat = accom_cat.upper()
    if cat == "LAND":
        return "Land"
    if cat in ("COMM", "COMMERCIAL"):
        return "Commercial"
    return "Residential"  # RESI and anything else


def infer_category(name: str, category_text: str) -> str | None:
    combined = " ".join([name or "", category_text or ""]).lower()
    for cat in ["apartment", "bungalow", "maisonette", "townhouse", "villa",
                "studio", "penthouse", "bedsitter"]:
        if cat in combined:
            return cat.capitalize()
    if "house" in combined:
        return "House"
    if "land" in combined or "plot" in combined:
        return "Land"
    return None


def parse_date_str(text: str | None) -> date | None:
    if not text:
        return None
    # ISO format e.g. "2025-03-14T10:22:00.000000Z"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def is_in_date_range(d: date | None) -> bool:
    if d is None:
        return True  # include unknowns — better to over-collect
    return DATE_FROM <= d <= DATE_TO


# Search page — collect listing URLs

def get_listing_urls_from_search_page(page_num: int) -> list[str]:
    """
    Fetch a search results page and return all /listings/* hrefs found.
    The search results page at /property-for-sale?page=N is plain HTML
    with listing links in <a href="/listings/..."> tags.
    """
    url = f"{SEARCH_URL}?page={page_num}"
    body = fetch(url)
    if not body:
        return []

    # Extract all /listings/... hrefs (exclude anchors and query strings)
    raw = re.findall(r'href="(/listings/[^"#?]+)"', body)
    # Deduplicate while preserving order, exclude non-listing paths
    seen = set()
    urls = []
    for path in raw:
        if path not in seen and re.search(r'/listings/[^/]+-\d+$', path):
            seen.add(path)
            urls.append(BASE_URL + path)

    log.info(f"  Search page {page_num}: {len(urls)} listing URLs")
    return urls


# Detail page — extract all fields 

def scrape_listing_detail(url: str) -> dict:
    record: dict = {col: None for col in OUTPUT_COLUMNS}
    record["URL"] = url

    body = fetch(url)
    if not body:
        return record

    # JSON-LD structured data 
    ld_raw = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
        body, re.DOTALL
    )
    ld_graph = []
    if ld_raw:
        try:
            ld = json.loads(ld_raw[0])
            ld_graph = ld.get("@graph", [])
        except Exception:
            pass

    # Index by type
    ld_by_type: dict = {}
    for node in ld_graph:
        t = node.get("@type", "")
        ld_by_type[t] = node

    # Name
    name = None
    if "RealEstateListing" in ld_by_type:
        raw_name = ld_by_type["RealEstateListing"].get("name", "")
        # Strip the " | BuyRentKenya" suffix and price suffix
        raw_name = re.sub(r"\s*\|\s*BuyRentKenya.*$", "", raw_name)
        raw_name = re.sub(r"\s+for\s+KSh[\d,\s]+$", "", raw_name)
        name = clean(raw_name)
    if not name:
        # Fallback to og:title meta
        m = re.search(r'<meta[^>]+og:title[^>]+content="([^"]+)"', body)
        if m:
            name = clean(m.group(1).split("|")[0])
    record["Name"] = name

    # Price
    price = None
    # Offer in JSON-LD
    offer = ld_by_type.get("Offer", {})
    price_val = offer.get("price") or offer.get("Price")
    currency = offer.get("priceCurrency", "KSh")
    if price_val:
        price = f"KSh {float(price_val):,.0f}" if isinstance(price_val, (int, float)) else f"KSh {price_val}"
    if not price:
        # Regex from HTML body
        m = re.search(r"KSh[\s\xa0]*([\d,]+(?:\.\d+)?)", body)
        if m:
            price = f"KSh {m.group(1)}"
    record["Price"] = price

    # Date 
    listing_date = None
    rel = ld_by_type.get("RealEstateListing", {})
    for date_field in ("datePublished", "dateCreated", "dateModified"):
        raw_date = rel.get(date_field)
        if raw_date:
            listing_date = parse_date_str(raw_date)
            if listing_date:
                break
    record["Date"] = listing_date.strftime("%Y-%m-%d") if listing_date else None

    # Category & accommodation type 
    accommodation = ld_by_type.get("Accommodation", {})
    product = ld_by_type.get("Product", {})

    raw_category = product.get("category", "")
    record["Category"] = infer_category(name or "", raw_category)

    description = (
        product.get("description") or
        rel.get("description") or ""
    )

    # Use JSON-LD accommodationCategory as the primary source of Type
    accom_cat = accommodation.get("accommodationCategory")
    record["Type"] = accom_category_to_type(accom_cat)

    # Location & County 
    # There are multiple PostalAddress nodes — pick the one for this listing
    # (identified by the listing ID in its @id, not the org's address)
    listing_id_m = re.search(r'/listings/[^/]+-(\d+)$', url)
    listing_id = listing_id_m.group(1) if listing_id_m else None

    listing_address = None
    # ld_graph has multiple PostalAddress nodes; pick the listing one
    for node in ld_graph:
        if node.get("@type") == "PostalAddress":
            node_id = node.get("@id", "")
            if listing_id and f"listing-{listing_id}" in node_id:
                listing_address = node
                break
    # Fallback: second PostalAddress (first is org's office address)
    if not listing_address:
        pa_nodes = [n for n in ld_graph if n.get("@type") == "PostalAddress"]
        if len(pa_nodes) >= 2:
            listing_address = pa_nodes[1]
        elif pa_nodes:
            listing_address = pa_nodes[0]

    if listing_address:
        locality = clean(listing_address.get("addressLocality"))
        region = clean(listing_address.get("addressRegion"))
        parts = [p for p in [locality, region] if p and p != locality or p == locality]
        # Use locality + region if different
        if locality and region and locality.lower() != region.lower():
            record["Location"] = f"{locality}, {region}"
        elif locality:
            record["Location"] = locality
        elif region:
            record["Location"] = region

    # County from breadcrumb 
    breadcrumb = ld_by_type.get("BreadcrumbList", {})
    crumb_items = breadcrumb.get("itemListElement", [])
    for item in crumb_items:
        crumb_name = item.get("name", "")
        # County entries are labeled "Xxx County"
        county_m = re.match(r"^(.+?)\s+County$", crumb_name, re.IGNORECASE)
        if county_m:
            record["County"] = county_m.group(1)
            break

    # Fallback: extract county from location
    if not record["County"] and record["Location"]:
        record["County"] = extract_county(record["Location"])

    # Bedrooms & Bathrooms 
    beds = accommodation.get("numberOfBedrooms")
    baths = accommodation.get("numberOfBathroomsTotal")

    if beds is not None:
        record["No. of Bedrooms"] = parse_int(str(beds))
    else:
        m = re.search(r"(\d+)\s*[Bb]ed(?:room)?s?", body)
        if m:
            record["No. of Bedrooms"] = int(m.group(1))

    if baths is not None:
        record["No. of Bathrooms"] = parse_int(str(baths))
    else:
        m = re.search(r"(\d+)\s*[Bb]ath(?:room)?s?", body)
        if m:
            record["No. of Bathrooms"] = int(m.group(1))

    # En-suite bedrooms 
    ensuite_m = re.search(
        r"(\d+)\s*(?:en[\s\-]?suite|ensuite)", body, re.IGNORECASE
    )
    if ensuite_m:
        record["No. of Ensuite Bedrooms"] = int(ensuite_m.group(1))
    elif "en suite" in body.lower() or "ensuite" in body.lower():
        # If mentioned but no count, assume 1
        record["No. of Ensuite Bedrooms"] = 1

    # Floor area (sqm) 
    area_m = re.search(
        r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*m|sqm|m²|sqft|sq ft|ft^2|square\s*met(?:er|re)s?)",
        body, re.IGNORECASE
    )
    if area_m:
        record["Floor_area_sqm"] = parse_float(area_m.group(1))

    # Land size 
    land_m = re.search(
        r"([\d,]+(?:\.\d+)?)\s*(?:acres?|ha\b|hectares?)",
        body, re.IGNORECASE
    )
    if land_m:
        unit = re.search(r"(?:acres?|ha\b|hectares?)", land_m.group(0), re.IGNORECASE)
        record["Land_Size"] = f"{land_m.group(1)} {unit.group(0) if unit else ''}"

    # Amenities from description text 
    desc_lower = description.lower()
    body_lower = body.lower()

    # Elevator
    record["Elevator"] = 1 if re.search(r"\b(?:lift|elevator)\b", desc_lower + body_lower) else 0

    # Parking
    record["Parking"] = 1 if re.search(r"\b(?:parking|garage|carport|car\s*park|car slot)\b", desc_lower + body_lower) else 0

    # DSQ
    record["DSQ"] = 1 if re.search(
        r"\b(?:dsq|servant[\s']?s?\s*quarter|staff\s*quarter|domestic\s*staff\s*quarter|dsq unit)\b",
        desc_lower + body_lower
    ) else 0

    # Condition 
    if re.search(
        r"\b(?:off[\s\-]?plan|under\s*construction|brand[\s\-]?new|newly\s*built|new\s*develop)\b",
        desc_lower + body_lower
    ):
        record["Condition"] = "New"
    elif re.search(
        r"\b(?:existing|resale|pre[\s\-]?owned|second[\s\-]?hand|old\s*house)\b",
        desc_lower + body_lower
    ):
        record["Condition"] = "Existing"

    # Floor number (apartments) 
    floor_m = re.search(
        r"(?:on\s+(?:the\s+)?)?(?:floor|level)\s*(?:no\.?\s*)?(\d+|ground|basement|lower|upper|top)",
        body, re.IGNORECASE
    )
    if floor_m:
        val = floor_m.group(1).lower()
        if val == "ground":
            record["Floor_Number"] = 0
        elif val in ("basement", "lower"):
            record["Floor_Number"] = -1
        else:
            record["Floor_Number"] = parse_int(val)

    return record


# Main scraper loop 

def scrape(
    max_pages: int = 50,
    output_file: str = "buyrentkenya_krppi.csv",
    resume: bool = False,
) -> list[dict]:
    already_done: set[str] = set()
    existing_df: pd.DataFrame | None = None

    if resume and Path(output_file).exists():
        try:
            existing_df = pd.read_csv(output_file)
            already_done = set(existing_df["URL"].dropna().tolist())
            log.info(f"Resume mode — {len(already_done)} URLs already scraped")
        except Exception as e:
            log.warning(f"Could not read existing CSV: {e}")

    all_records: list[dict] = []
    skipped_date = 0
    skipped_done = 0

    log.info(f"Date filter: {DATE_FROM} → {DATE_TO}")
    log.info(f"Max pages: {max_pages} | Output: {output_file}")

    for page_num in range(2, max_pages + 1):
        log.info(f"=== Search page {page_num}/{max_pages} ===")
        listing_urls = get_listing_urls_from_search_page(page_num)

        if not listing_urls:
            log.info("No listings found on this page — stopping.")
            break

        for idx, listing_url in enumerate(listing_urls):
            if listing_url in already_done:
                skipped_done += 1
                continue

            if idx < 2 or idx >= len(listing_urls) - 2:
                log.info(f"  Scraping listing {idx + 1}/{len(listing_urls)}: {listing_url}")
            else:
                log.info(f"  Scraping listing {idx + 1}/{len(listing_urls)}")
            record = scrape_listing_detail(listing_url)

            # Date filter
            if record["Date"]:
                try:
                    d = datetime.strptime(record["Date"], "%Y-%m-%d").date()
                    if not is_in_date_range(d):
                        skipped_date += 1
                        log.info(f"    → SKIP (date out of range: {record['Date']})")
                        time.sleep(REQUEST_DELAY)
                        continue
                except ValueError:
                    pass

            all_records.append(record)
            if idx < 2 or idx >= len(listing_urls) - 2:
                log.info(
                    f"    → Collected: {record['Name'] or 'N/A'}[:50] | "
                    f"{record['Price'] or 'N/A'} | "
                    f"{record['Location'] or 'N/A'} | "
                    f"{record['Date'] or 'no date'}"
                )

            # Auto-save every 25 records
            if len(all_records) % 25 == 0:
                _save(all_records, output_file, existing_df)
                log.info(f"  ✓ Auto-saved — {len(all_records)} records so far")

            time.sleep(REQUEST_DELAY)

        time.sleep(REQUEST_DELAY)

    log.info(
        f"\n=== DONE ===\n"
        f"  Collected:      {len(all_records)}\n"
        f"  Skipped (date): {skipped_date}\n"
        f"  Skipped (done): {skipped_done}"
    )

    _save(all_records, output_file, existing_df)
    return all_records


def _save(
    new_records: list[dict],
    output_file: str,
    existing_df: pd.DataFrame | None,
) -> None:
    df_new = pd.DataFrame(new_records, columns=OUTPUT_COLUMNS)
    if existing_df is not None and not existing_df.empty:
        df_final = pd.concat([existing_df, df_new], ignore_index=True)
        df_final = df_final.drop_duplicates(subset=["URL"], keep="last")
    else:
        df_final = df_new
    df_final.to_csv(output_file, index=False)
    log.info(f"Saved {len(df_final)} total records → {output_file}")


# Entry point 

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BuyRentKenya KRPPI Scraper")
    parser.add_argument(
        "--pages", type=int, default=50,
        help="Max search pages to crawl (default: 50, ~24 listings each)"
    )
    parser.add_argument(
        "--output", type=str, default="buyrentkenya_krppi.csv",
        help="Output CSV file name"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from an existing CSV, skipping already-scraped URLs"
    )
    args = parser.parse_args()

    records = scrape(
        max_pages=args.pages,
        output_file=args.output,
        resume=args.resume,
    )

    if records:
        df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
        print(f"\n=== SAMPLE (first 5 rows) ===")
        print(df.head(5).to_string())
        print(f"\nDate range in data: {df['Date'].min()} → {df['Date'].max()}")
        print(f"Types: {df['Type'].value_counts().to_dict()}")
        print(f"Categories: {df['Category'].value_counts().to_dict()}")

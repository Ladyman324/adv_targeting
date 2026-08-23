from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import random
import re
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path  # one naming convention, defined once

import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlparse

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Locator,
        Page,
        Response,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError as exc:
    raise SystemExit(
        "Playwright is not installed.\n\n"
        "Install it with:\n"
        "    python -m pip install playwright\n\n"
        "This script connects to your installed Chrome through CDP, so a "
        "Playwright browser download is normally unnecessary."
    ) from exc


# =============================================================================
# CONFIGURATION
# =============================================================================

CDP_PORT = 9222
CDP_HOST = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_USER_DATA_DIR = Path(r"C:\chrome_dev_profile")

BASE_URL = "https://www.raymondjames.com"
LOCATOR_URL = f"{BASE_URL}/find-an-advisor"
AUTOCOMPLETE_API_PATH = "/dotcom/api/getplaceautocomplete/"
DETAILS_API_PATH = "/dotcom/api/getplacedetailsbyplaceid/"

# Exact controls supplied from the current Raymond James locator markup.
LOCATION_INPUT_SELECTOR = "#zip-v2"
RADIUS_100_SELECTOR = "#mileage-100"
RADIUS_100_LABEL_SELECTOR = 'label[for="mileage-100"]'
NEXT_PAGE_SELECTOR = (
    'button.pagination-v2-result.next'
    '[aria-label="Next search results page"]'
)
PREVIOUS_PAGE_SELECTOR = (
    'button.pagination-v2-result.prev'
    '[aria-label="Previous search results page"]'
)
AUTOCOMPLETE_OPTION_SELECTOR = 'li[id^="autoComplete_result_"][role="option"]'
AUTOCOMPLETE_COMMIT_TIMEOUT_MS = 15_000

DEFAULT_RADIUS_MILES = 100
CHROME_START_TIMEOUT_SECONDS = 25
NATIVE_TAB_WAIT_SECONDS = 75
CONTROL_TIMEOUT_MS = 30_000
RESPONSE_TIMEOUT_SECONDS = 60
RESULT_RENDER_TIMEOUT_MS = 35_000

# These waits are not used to retry or evade a denial. They simply give the
# visible page time to behave like the successful manual workflow: render the
# results, scroll through them, and then click Next.
TYPE_DELAY_RANGE_MS = (45, 90)
AFTER_RESULTS_DELAY_RANGE_SECONDS = (2.5, 5.0)
BEFORE_NEXT_CLICK_DELAY_RANGE_SECONDS = (2.0, 4.0)
BETWEEN_LOCATIONS_DELAY_RANGE_SECONDS = (8.0, 14.0)
SCROLL_STEP_RANGE = (450, 850)
SCROLL_DELAY_RANGE_SECONDS = (0.18, 0.38)

TARGET_LOCATIONS = [
    # New England
    {"zip": "02108", "name": "Boston, MA"},
    {"zip": "01608", "name": "Worcester, MA"},
    {"zip": "01103", "name": "Springfield, MA"},
    {"zip": "04101", "name": "Portland, ME"},
    {"zip": "04401", "name": "Bangor, ME"},
    {"zip": "05401", "name": "Burlington, VT"},
    {"zip": "03101", "name": "Manchester, NH"},
    {"zip": "02903", "name": "Providence, RI"},
    {"zip": "06103", "name": "Hartford, CT"},
    {"zip": "06510", "name": "New Haven, CT"},

    # New York / New Jersey / Pennsylvania / Mid-Atlantic
    {"zip": "10001", "name": "New York, NY"},
    {"zip": "12207", "name": "Albany, NY"},
    {"zip": "13202", "name": "Syracuse, NY"},
    {"zip": "14604", "name": "Rochester, NY"},
    {"zip": "14202", "name": "Buffalo, NY"},
    {"zip": "07102", "name": "Newark, NJ"},
    {"zip": "08401", "name": "Atlantic City, NJ"},
    {"zip": "19102", "name": "Philadelphia, PA"},
    {"zip": "17101", "name": "Harrisburg, PA"},
    {"zip": "15219", "name": "Pittsburgh, PA"},
    {"zip": "16501", "name": "Erie, PA"},
    {"zip": "18503", "name": "Scranton, PA"},
    {"zip": "19801", "name": "Wilmington, DE"},
    {"zip": "21201", "name": "Baltimore, MD"},
    {"zip": "20001", "name": "Washington, DC"},
    {"zip": "20814", "name": "Bethesda, MD"},
    {"zip": "23219", "name": "Richmond, VA"},
    {"zip": "23510", "name": "Norfolk, VA"},
    {"zip": "24011", "name": "Roanoke, VA"},
    {"zip": "25301", "name": "Charleston, WV"},

    # Carolinas / Georgia / Florida
    {"zip": "28202", "name": "Charlotte, NC"},
    {"zip": "27601", "name": "Raleigh, NC"},
    {"zip": "28401", "name": "Wilmington, NC"},
    {"zip": "28801", "name": "Asheville, NC"},
    {"zip": "27401", "name": "Greensboro, NC"},
    {"zip": "29201", "name": "Columbia, SC"},
    {"zip": "29401", "name": "Charleston, SC"},
    {"zip": "29601", "name": "Greenville, SC"},
    {"zip": "30309", "name": "Atlanta, GA"},
    {"zip": "31401", "name": "Savannah, GA"},
    {"zip": "31201", "name": "Macon, GA"},
    {"zip": "30901", "name": "Augusta, GA"},
    {"zip": "31901", "name": "Columbus, GA"},
    {"zip": "32202", "name": "Jacksonville, FL"},
    {"zip": "32301", "name": "Tallahassee, FL"},
    {"zip": "32502", "name": "Pensacola, FL"},
    {"zip": "32801", "name": "Orlando, FL"},
    {"zip": "33602", "name": "Tampa, FL"},
    {"zip": "33901", "name": "Fort Myers, FL"},
    {"zip": "33401", "name": "West Palm Beach, FL"},
    {"zip": "33101", "name": "Miami, FL"},
    {"zip": "33040", "name": "Key West, FL"},

    # Deep South / Kentucky / Tennessee
    {"zip": "35203", "name": "Birmingham, AL"},
    {"zip": "35801", "name": "Huntsville, AL"},
    {"zip": "36104", "name": "Montgomery, AL"},
    {"zip": "36602", "name": "Mobile, AL"},
    {"zip": "39201", "name": "Jackson, MS"},
    {"zip": "39401", "name": "Hattiesburg, MS"},
    {"zip": "40202", "name": "Louisville, KY"},
    {"zip": "40507", "name": "Lexington, KY"},
    {"zip": "37201", "name": "Nashville, TN"},
    {"zip": "37902", "name": "Knoxville, TN"},
    {"zip": "37402", "name": "Chattanooga, TN"},
    {"zip": "38120", "name": "Memphis, TN"},

    # Ohio / Michigan / Indiana / Illinois
    {"zip": "44114", "name": "Cleveland, OH"},
    {"zip": "43215", "name": "Columbus, OH"},
    {"zip": "45202", "name": "Cincinnati, OH"},
    {"zip": "43604", "name": "Toledo, OH"},
    {"zip": "45402", "name": "Dayton, OH"},
    {"zip": "48226", "name": "Detroit, MI"},
    {"zip": "49503", "name": "Grand Rapids, MI"},
    {"zip": "48933", "name": "Lansing, MI"},
    {"zip": "49684", "name": "Traverse City, MI"},
    {"zip": "46204", "name": "Indianapolis, IN"},
    {"zip": "46802", "name": "Fort Wayne, IN"},
    {"zip": "46601", "name": "South Bend, IN"},
    {"zip": "47708", "name": "Evansville, IN"},
    {"zip": "60601", "name": "Chicago, IL"},
    {"zip": "62701", "name": "Springfield, IL"},
    {"zip": "61602", "name": "Peoria, IL"},
    {"zip": "61101", "name": "Rockford, IL"},
    {"zip": "61820", "name": "Champaign, IL"},

    # Wisconsin / Minnesota / Iowa / Missouri
    {"zip": "53202", "name": "Milwaukee, WI"},
    {"zip": "53703", "name": "Madison, WI"},
    {"zip": "54301", "name": "Green Bay, WI"},
    {"zip": "54401", "name": "Wausau, WI"},
    {"zip": "54601", "name": "La Crosse, WI"},
    {"zip": "55401", "name": "Minneapolis, MN"},
    {"zip": "55802", "name": "Duluth, MN"},
    {"zip": "55901", "name": "Rochester, MN"},
    {"zip": "50309", "name": "Des Moines, IA"},
    {"zip": "52401", "name": "Cedar Rapids, IA"},
    {"zip": "52801", "name": "Davenport, IA"},
    {"zip": "51101", "name": "Sioux City, IA"},
    {"zip": "63101", "name": "St. Louis, MO"},
    {"zip": "64106", "name": "Kansas City, MO"},
    {"zip": "65806", "name": "Springfield, MO"},
    {"zip": "65201", "name": "Columbia, MO"},

    # Arkansas / Louisiana / Oklahoma / Texas
    {"zip": "72201", "name": "Little Rock, AR"},
    {"zip": "72701", "name": "Fayetteville, AR"},
    {"zip": "72401", "name": "Jonesboro, AR"},
    {"zip": "70112", "name": "New Orleans, LA"},
    {"zip": "70801", "name": "Baton Rouge, LA"},
    {"zip": "71101", "name": "Shreveport, LA"},
    {"zip": "70501", "name": "Lafayette, LA"},
    {"zip": "70601", "name": "Lake Charles, LA"},
    {"zip": "73102", "name": "Oklahoma City, OK"},
    {"zip": "74103", "name": "Tulsa, OK"},
    {"zip": "73501", "name": "Lawton, OK"},
    {"zip": "75201", "name": "Dallas, TX"},
    {"zip": "76102", "name": "Fort Worth, TX"},
    {"zip": "77002", "name": "Houston, TX"},
    {"zip": "78701", "name": "Austin, TX"},
    {"zip": "78205", "name": "San Antonio, TX"},
    {"zip": "79901", "name": "El Paso, TX"},
    {"zip": "79101", "name": "Amarillo, TX"},
    {"zip": "79401", "name": "Lubbock, TX"},
    {"zip": "79701", "name": "Midland, TX"},
    {"zip": "78401", "name": "Corpus Christi, TX"},
    {"zip": "78501", "name": "McAllen, TX"},
    {"zip": "75701", "name": "Tyler, TX"},
    {"zip": "76701", "name": "Waco, TX"},
    {"zip": "77701", "name": "Beaumont, TX"},

    # Kansas / Nebraska / Dakotas
    {"zip": "67202", "name": "Wichita, KS"},
    {"zip": "66603", "name": "Topeka, KS"},
    {"zip": "67801", "name": "Dodge City, KS"},
    {"zip": "68102", "name": "Omaha, NE"},
    {"zip": "68508", "name": "Lincoln, NE"},
    {"zip": "69101", "name": "North Platte, NE"},
    {"zip": "69361", "name": "Scottsbluff, NE"},
    {"zip": "57104", "name": "Sioux Falls, SD"},
    {"zip": "57701", "name": "Rapid City, SD"},
    {"zip": "57501", "name": "Pierre, SD"},
    {"zip": "58102", "name": "Fargo, ND"},
    {"zip": "58501", "name": "Bismarck, ND"},
    {"zip": "58201", "name": "Grand Forks, ND"},
    {"zip": "58701", "name": "Minot, ND"},

    # Colorado / Wyoming / Montana / Idaho / Utah / New Mexico
    {"zip": "80202", "name": "Denver, CO"},
    {"zip": "80521", "name": "Fort Collins, CO"},
    {"zip": "80903", "name": "Colorado Springs, CO"},
    {"zip": "81003", "name": "Pueblo, CO"},
    {"zip": "81501", "name": "Grand Junction, CO"},
    {"zip": "81301", "name": "Durango, CO"},
    {"zip": "82001", "name": "Cheyenne, WY"},
    {"zip": "82601", "name": "Casper, WY"},
    {"zip": "82716", "name": "Gillette, WY"},
    {"zip": "59101", "name": "Billings, MT"},
    {"zip": "59801", "name": "Missoula, MT"},
    {"zip": "59601", "name": "Helena, MT"},
    {"zip": "59715", "name": "Bozeman, MT"},
    {"zip": "59401", "name": "Great Falls, MT"},
    {"zip": "59901", "name": "Kalispell, MT"},
    {"zip": "83702", "name": "Boise, ID"},
    {"zip": "83401", "name": "Idaho Falls, ID"},
    {"zip": "83301", "name": "Twin Falls, ID"},
    {"zip": "83814", "name": "Coeur d'Alene, ID"},
    {"zip": "84101", "name": "Salt Lake City, UT"},
    {"zip": "84601", "name": "Provo, UT"},
    {"zip": "84770", "name": "St. George, UT"},
    {"zip": "84720", "name": "Cedar City, UT"},
    {"zip": "87102", "name": "Albuquerque, NM"},
    {"zip": "87501", "name": "Santa Fe, NM"},
    {"zip": "88001", "name": "Las Cruces, NM"},
    {"zip": "88201", "name": "Roswell, NM"},

    # Arizona / Nevada / California
    {"zip": "85004", "name": "Phoenix, AZ"},
    {"zip": "85701", "name": "Tucson, AZ"},
    {"zip": "86001", "name": "Flagstaff, AZ"},
    {"zip": "85364", "name": "Yuma, AZ"},
    {"zip": "89101", "name": "Las Vegas, NV"},
    {"zip": "89501", "name": "Reno, NV"},
    {"zip": "89801", "name": "Elko, NV"},
    {"zip": "90012", "name": "Los Angeles, CA"},
    {"zip": "92101", "name": "San Diego, CA"},
    {"zip": "93301", "name": "Bakersfield, CA"},
    {"zip": "93721", "name": "Fresno, CA"},
    {"zip": "95814", "name": "Sacramento, CA"},
    {"zip": "94102", "name": "San Francisco, CA"},
    {"zip": "95113", "name": "San Jose, CA"},
    {"zip": "93101", "name": "Santa Barbara, CA"},
    {"zip": "93401", "name": "San Luis Obispo, CA"},
    {"zip": "93940", "name": "Monterey, CA"},
    {"zip": "96001", "name": "Redding, CA"},
    {"zip": "95501", "name": "Eureka, CA"},

    # Oregon / Washington
    {"zip": "97205", "name": "Portland, OR"},
    {"zip": "97401", "name": "Eugene, OR"},
    {"zip": "97501", "name": "Medford, OR"},
    {"zip": "97701", "name": "Bend, OR"},
    {"zip": "98101", "name": "Seattle, WA"},
    {"zip": "99201", "name": "Spokane, WA"},
    {"zip": "98901", "name": "Yakima, WA"},
    {"zip": "99336", "name": "Kennewick, WA"},
    {"zip": "98225", "name": "Bellingham, WA"},

    # Alaska: several separate population centers because a 100-mile radius
    # cannot cover this geographically large state from Anchorage alone.
    {"zip": "99501", "name": "Anchorage, AK"},
    {"zip": "99701", "name": "Fairbanks, AK"},
    {"zip": "99801", "name": "Juneau, AK"},
    {"zip": "99901", "name": "Ketchikan, AK"},
    {"zip": "99835", "name": "Sitka, AK"},
    {"zip": "99615", "name": "Kodiak, AK"},

    # Hawaii: one center for each principal populated island/region.
    {"zip": "96813", "name": "Honolulu, HI"},
    {"zip": "96720", "name": "Hilo, HI"},
    {"zip": "96732", "name": "Kahului, HI"},
    {"zip": "96740", "name": "Kailua-Kona, HI"},
    {"zip": "96766", "name": "Lihue, HI"},
]

CSV_FIELDS = [
    "advisor_id", "fa_number", "name", "designation", "designations",
    "title", "team_name", "phone", "email", "website_url",
    "advisor_profile_url", "contact_form_url", "photo_url",
    "advisor_subsidiary", "country", "role_code", "position_code",
    "class_code", "branch_id", "branch_name", "branch_subheaders",
    "branch_phone", "branch_email", "branch_website_url",
    "branch_contact_form_url", "branch_subsidiary", "branch_type_code",
    "branch_subtype_code", "branch_is_alex_brown", "address_line1",
    "address_line2", "address_line3", "city", "state", "zip",
    "latitude", "longitude", "min_distance_miles",
    "nearest_search_center", "source_locations", "source_seed_zips",
    "all_branch_ids", "all_branch_addresses", "discovery_count",
]

BRANCH_AND_ADDRESS_FIELDS = [
    "branch_id", "branch_name", "branch_subheaders", "branch_phone",
    "branch_email", "branch_website_url", "branch_contact_form_url",
    "branch_subsidiary", "branch_type_code", "branch_subtype_code",
    "branch_is_alex_brown", "address_line1", "address_line2",
    "address_line3", "city", "state", "zip", "latitude", "longitude",
]


# =============================================================================
# EXCEPTIONS
# =============================================================================

class RaymondJamesError(RuntimeError):
    pass


class RaymondJamesBlockedError(RaymondJamesError):
    pass


class SelectorError(RaymondJamesError):
    pass


# =============================================================================
# BASIC HELPERS
# =============================================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_url(value: Any) -> str:
    text = clean_text(value)
    return urljoin(BASE_URL, text) if text else ""


def blank(value: Any) -> bool:
    return value is None or clean_text(value) == ""


def float_or_none(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_address(address: dict[str, Any]) -> str:
    street = ", ".join(
        part for part in [
            clean_text(address.get("line1")),
            clean_text(address.get("line2")),
            clean_text(address.get("line3")),
        ] if part
    )
    city = clean_text(address.get("city"))
    state = clean_text(address.get("state"))
    postal = clean_text(address.get("zip"))
    city_state = ", ".join(part for part in [city, state] if part)
    if postal:
        city_state = f"{city_state} {postal}".strip()
    return " | ".join(part for part in [street, city_state] if part)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


EXPECTED_JURISDICTIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}


def normalize_state_code(value: str) -> str:
    """Normalize state/jurisdiction variants to two-letter codes."""
    normalized = clean_text(value).upper().replace(".", "").strip()

    aliases = {
        "WASHINGTON DC": "DC",
        "WASHINGTON D C": "DC",
        "DISTRICT OF COLUMBIA": "DC",
        "D C": "DC",
        "DC": "DC",
        "D.C.":"DC",
        "D.C., D.C.":"DC",
    }

    return aliases.get(normalized, normalized)


def state_code_from_location(name: str) -> str:
    """Extract and normalize the state code from 'City, ST'."""
    _, separator, state = clean_text(name).rpartition(",")

    if not separator:
        return ""

    return normalize_state_code(state)


def validate_target_locations() -> None:
    seen: set[str] = set()
    state_counts: dict[str, int] = {}

    for item in TARGET_LOCATIONS:
        name = item.get("name")
        seed_zip = item.get("zip")
        if not isinstance(name, str) or "," not in name:
            raise ValueError(f"Invalid target location: {item!r}")
        if not isinstance(seed_zip, str) or not re.fullmatch(r"\d{5}", seed_zip):
            raise ValueError(f"Invalid seed ZIP: {item!r}")

        normalized = normalize_location(name)
        if normalized in seen:
            raise ValueError(f"Duplicate target location: {name}")
        seen.add(normalized)

        state = state_code_from_location(name)
        if state not in EXPECTED_JURISDICTIONS:
            raise ValueError(f"Invalid or unsupported state code in {name!r}")
        state_counts[state] = state_counts.get(state, 0) + 1

    missing = EXPECTED_JURISDICTIONS - set(state_counts)
    if missing:
        raise ValueError(
            "National target grid is missing: " + ", ".join(sorted(missing))
        )

    if state_counts.get("AK", 0) < 6:
        raise ValueError("The Alaska grid must contain at least six centers.")
    if state_counts.get("HI", 0) < 5:
        raise ValueError("The Hawaii grid must contain at least five centers.")


def normalize_location(value: str) -> str:
    """Normalize location text for comparisons.

    Treats Washington, DC and Washington, D.C. as equivalent.
    """
    normalized = clean_text(value).casefold()

    # Normalize District of Columbia variants.
    normalized = re.sub(
        r"\b(?:d\s*\.\s*c\s*\.?|district\s+of\s+columbia)\b",
        "dc",
        normalized,
        flags=re.I,
    )

    # Ignore periods and normalize spacing around commas.
    normalized = normalized.replace(".", "")
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip()


def response_query(response: Response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.url).query)


def query_first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    return values[0] if values else ""


# =============================================================================
# CHROME / CDP
# =============================================================================

def get_cdp_metadata() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"{CDP_HOST}/json/version",
            timeout=2,
        ) as response:
            metadata = json.load(response)
        return metadata if metadata.get("webSocketDebuggerUrl") else None
    except Exception:
        return None


def launch_cdp_chrome() -> None:
    if not CHROME_PATH.is_file():
        raise FileNotFoundError(f"Chrome executable not found at:\n{CHROME_PATH}")

    CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Launching Chrome with remote debugging enabled...")

    subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            LOCATOR_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    deadline = time.monotonic() + CHROME_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if get_cdp_metadata():
            print("Chrome launched successfully.")
            return
        time.sleep(0.5)

    raise RuntimeError(f"Chrome did not expose CDP at {CDP_HOST}.")


def ensure_chrome_running() -> None:
    metadata = get_cdp_metadata()
    if metadata:
        print(
            f"Chrome CDP active on port {CDP_PORT}: "
            f"{metadata.get('Browser', 'Chrome')}"
        )
        return
    launch_cdp_chrome()


def open_locator_tab_natively() -> None:
    """Ask installed Chrome to open one normal locator tab.

    This is used only when no existing locator tab is present. The script does
    not open a new tab per city or per results page.
    """

    subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            LOCATOR_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def attach_page_diagnostics(page: Page) -> None:
    def on_response(response: Response) -> None:
        if response.status >= 400:
            print(
                f"      Browser HTTP {response.status} "
                f"[{response.request.resource_type}] {response.url}"
            )

    def on_request_failed(request: Any) -> None:
        print(
            f"      Browser request failed: {request.method} "
            f"{request.url} | {request.failure}"
        )

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    page.on("pageerror", lambda error: print(f"      Browser JS error: {error}"))


async def page_shows_access_denied(page: Page) -> bool:
    if "errors.edgesuite.net" in page.url.casefold():
        return True
    try:
        body_text = await page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return False
    normalized = body_text.casefold()
    return (
        "access denied" in normalized
        and "you don't have permission" in normalized
    )


async def find_existing_locator_page(context: BrowserContext) -> Page | None:
    """Return the unique usable locator tab.

    A common failure mode is having several Raymond James locator tabs open.
    Selecting the first matching URL can make the script manipulate a hidden
    background tab while the user watches a different tab. Only tabs that
    contain a visible #zip-v2 are considered usable.
    """

    matching_pages: list[Page] = []

    for candidate in context.pages:
        if "raymondjames.com/find-an-advisor" not in candidate.url:
            continue
        try:
            if await page_shows_access_denied(candidate):
                continue
            input_box = candidate.locator(LOCATION_INPUT_SELECTOR)
            if await input_box.count() and await input_box.first.is_visible():
                matching_pages.append(candidate)
        except Exception:
            continue

    if len(matching_pages) == 1:
        return matching_pages[0]

    if len(matching_pages) > 1:
        descriptions = []
        for candidate in matching_pages:
            try:
                title = await candidate.title()
            except Exception:
                title = ""
            descriptions.append(f"{title!r} | {candidate.url}")
        raise SelectorError(
            "More than one usable Raymond James locator tab is open. Close "
            "the extra locator tabs and rerun. Tabs found: "
            + "; ".join(descriptions)
        )

    return None


async def get_locator_page(context: BrowserContext) -> Page:
    page = await find_existing_locator_page(context)
    if page is None:
        print("No locator tab was open; asking Chrome to open one normal tab...")
        open_locator_tab_natively()
        deadline = time.monotonic() + NATIVE_TAB_WAIT_SECONDS
        while time.monotonic() < deadline:
            page = await find_existing_locator_page(context)
            if page is not None:
                break
            await asyncio.sleep(0.5)

    if page is None:
        raise RaymondJamesError(
            "No Raymond James locator tab appeared. Open the Find an Advisor "
            "page manually in the CDP Chrome window, then rerun."
        )

    attach_page_diagnostics(page)

    if await page_shows_access_denied(page):
        raise RaymondJamesBlockedError(
            "The active Raymond James tab is displaying Access Denied. "
            "The script will not reload it or make another request."
        )

    # Never page.goto() or page.reload() here. The script works only with the
    # normally loaded tab, matching the successful manual workflow.
    await page.bring_to_front()
    return page


# =============================================================================
# PASSIVE NETWORK CAPTURE
# =============================================================================

class NetworkCapture:
    """Capture only responses initiated by the site's own visible UI."""

    def __init__(self, page: Page):
        self.page = page
        self._queue: asyncio.Queue[Response] = asyncio.Queue()
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        path = urlparse(response.url).path.casefold()
        if path.endswith(AUTOCOMPLETE_API_PATH.casefold()) or path.endswith(
            DETAILS_API_PATH.casefold()
        ):
            self._queue.put_nowait(response)

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def close(self) -> None:
        """Detach the listener before disconnecting from the CDP browser."""
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass
        self.drain()

    async def wait_for(
        self,
        predicate: Callable[[Response], bool],
        *,
        timeout_seconds: float = RESPONSE_TIMEOUT_SECONDS,
    ) -> Response:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the expected site response.")
            response = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            if predicate(response):
                return response

    async def wait_for_optional(
        self,
        predicate: Callable[[Response], bool],
        *,
        timeout_seconds: float,
    ) -> Response | None:
        try:
            return await self.wait_for(
                predicate,
                timeout_seconds=timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return None


def is_autocomplete_response(
    response: Response,
    *,
    location_name: str,
) -> bool:
    parsed = urlparse(response.url)
    if not parsed.path.casefold().endswith(AUTOCOMPLETE_API_PATH.casefold()):
        return False
    query = parse_qs(parsed.query)
    return normalize_location(query_first(query, "input")) == normalize_location(
        location_name
    )


def is_details_response(
    response: Response,
    *,
    location_name: str,
    page_number: int | None = None,
    radius: int | None = None,
    place_id: str | None = None,
) -> bool:
    parsed = urlparse(response.url)
    if not parsed.path.casefold().endswith(DETAILS_API_PATH.casefold()):
        return False

    query = parse_qs(parsed.query)
    response_location = normalize_location(query_first(query, "location"))
    expected_location = normalize_location(location_name)

    if response_location and response_location != expected_location:
        return False

    if page_number is not None:
        if int_or_default(query_first(query, "page"), -1) != page_number:
            return False

    if radius is not None:
        if int_or_default(query_first(query, "radius"), -1) != radius:
            return False

    if place_id is not None:
        if clean_text(query_first(query, "placeid")) != clean_text(place_id):
            return False

    return True


async def read_json_response(
    response: Response,
    *,
    description: str,
) -> Any:
    # response.json() / response.text() already wait for the response body.
    # Calling response.finished() separately created Playwright completion tasks
    # that could raise noisy "Target closed" warnings during CDP disconnect.
    status = response.status
    if status == 403:
        try:
            body = await response.text()
        except Exception:
            body = ""
        raise RaymondJamesBlockedError(
            f"Raymond James returned HTTP 403 during {description}. "
            "The script stopped immediately without reloading or retrying.\n"
            f"URL: {response.url}\n"
            f"Response: {clean_text(body)[:1800]}"
        )

    if status >= 400:
        try:
            body = await response.text()
        except Exception:
            body = ""
        raise RaymondJamesError(
            f"Raymond James returned HTTP {status} during {description}.\n"
            f"URL: {response.url}\n"
            f"Response: {clean_text(body)[:1800]}"
        )

    try:
        return await response.json()
    except Exception as exc:
        try:
            body = await response.text()
        except Exception:
            body = ""
        raise RaymondJamesError(
            f"Expected JSON during {description}, but the response was not JSON.\n"
            f"URL: {response.url}\n"
            f"Response: {clean_text(body)[:1800]}"
        ) from exc


# =============================================================================
# VISIBLE UI LOCATORS AND ACTIONS
# =============================================================================

async def first_visible(
    locators: list[Locator],
    *,
    require_enabled: bool = False,
) -> Locator | None:
    for locator in locators:
        try:
            count = min(await locator.count(), 20)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                if require_enabled:
                    try:
                        if await candidate.is_disabled():
                            continue
                    except Exception:
                        aria_disabled = await candidate.get_attribute("aria-disabled")
                        if clean_text(aria_disabled).casefold() == "true":
                            continue
                return candidate
            except Exception:
                continue
    return None


async def find_location_input(page: Page) -> Locator:
    # Current exact markup: <input id="zip-v2" ... role="combobox">
    exact = page.locator(LOCATION_INPUT_SELECTOR)
    try:
        if await exact.count() and await exact.first.is_visible():
            return exact.first
    except Exception:
        pass

    # Keep semantic fallbacks in case Raymond James changes the element ID.
    locator = await first_visible([
        page.get_by_role("combobox").filter(
            has=page.locator('input.faa-v2-zip-input')
        ),
        page.locator('input.faa-v2-zip-input[role="combobox"]'),
        page.get_by_placeholder(
            re.compile(r"Enter\s+City,?\s*ST\s+or\s+ZIP\s+Code", re.I)
        ),
        page.get_by_label(re.compile(r"City.*ST.*ZIP|location", re.I)),
        page.locator('input[placeholder*="City" i][placeholder*="ZIP" i]'),
    ])
    if locator is None:
        raise SelectorError(
            'Could not find the location input. Expected #zip-v2.'
        )
    return locator


async def find_search_button(page: Page) -> Locator:
    locator = await first_visible([
        page.get_by_role(
            "button",
            name=re.compile(r"^\s*Find\s+An\s+Advisor\s*$", re.I),
        ),
        page.locator(
            'button:has-text("Find An Advisor"), '
            'input[type="submit"][value*="Find An Advisor" i]'
        ),
    ], require_enabled=True)
    if locator is None:
        raise SelectorError('Could not find the visible "Find An Advisor" button.')
    return locator


def score_place_suggestion(
    suggestion: dict[str, Any],
    location_name: str,
) -> int:
    description = clean_text(
        suggestion.get("Description") or suggestion.get("description")
    )
    description_lower = description.casefold()
    query_lower = normalize_location(location_name)
    city, separator, state = query_lower.rpartition(",")
    if not separator:
        city = query_lower
        state = ""
    else:
        city = city.strip()
        state = state.strip()

    score = 0
    if description_lower == query_lower:
        score += 300
    if description_lower.startswith(query_lower + ","):
        score += 250
    if city and city in description_lower:
        score += 50
    if state:
        state_pattern = rf"(?<![a-z]){re.escape(state)}(?![a-z])"
        if re.search(state_pattern, description_lower):
            score += 50
    if description_lower.endswith("usa") or "united states" in description_lower:
        score += 5
    return score


def autocomplete_display_text(description: str) -> str:
    """Convert API text such as 'New York, NY, USA' to the visible option text."""
    return re.sub(
        r",\s*(?:USA|United States(?: of America)?)\s*$",
        "",
        clean_text(description),
        flags=re.I,
    )


async def exact_autocomplete_option(
    page: Page,
    *,
    location_input: Locator,
    location_name: str,
    api_description: str,
) -> tuple[Locator, str]:
    controlled_list_id = clean_text(
        await location_input.get_attribute("aria-controls")
    )
    if not controlled_list_id:
        raise SelectorError(
            "#zip-v2 did not identify an autocomplete list through aria-controls."
        )

    controlled_list = page.locator(f'[id="{controlled_list_id}"]')
    await controlled_list.wait_for(state="visible", timeout=CONTROL_TIMEOUT_MS)

    expected_texts = {
        normalize_location(location_name),
        normalize_location(autocomplete_display_text(api_description)),
    }
    options = controlled_list.locator(AUTOCOMPLETE_OPTION_SELECTOR)

    deadline = time.monotonic() + CONTROL_TIMEOUT_MS / 1000
    observed: list[str] = []
    while time.monotonic() < deadline:
        observed = []
        count = min(await options.count(), 30)
        for index in range(count):
            option = options.nth(index)
            try:
                if not await option.is_visible():
                    continue
                option_text = clean_text(await option.inner_text())
                observed.append(option_text)
                if normalize_location(option_text) in expected_texts:
                    return option, controlled_list_id
            except Exception:
                continue
        await asyncio.sleep(0.1)

    raise SelectorError(
        f"Autocomplete did not show an exact visible option for {location_name}. "
        f"Observed options: {observed!r}"
    )


async def autocomplete_commit_state(
    page: Page,
    *,
    location_input: Locator,
    controlled_list_id: str,
    location_name: str,
) -> dict[str, Any]:
    return await page.evaluate(
        r"""
        ({inputSelector, listId, expected}) => {
            const normalize = value => (value || '')
                .replace(/\s+/g, ' ')
                .trim()
                .toLowerCase();
            const input = document.querySelector(inputSelector);
            const list = document.getElementById(listId);
            const visibleOptions = list
                ? Array.from(list.querySelectorAll('li[role="option"]'))
                    .filter(option => {
                        const style = window.getComputedStyle(option);
                        const rect = option.getBoundingClientRect();
                        return style.display !== 'none' &&
                               style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                    })
                    .map(option => (option.textContent || '').trim())
                : [];
            return {
                inputExists: Boolean(input),
                value: input?.value || '',
                expectedValue: normalize(input?.value) === normalize(expected),
                ariaExpanded: input?.getAttribute('aria-expanded'),
                ariaActiveDescendant: input?.getAttribute('aria-activedescendant'),
                listExists: Boolean(list),
                visibleOptions,
                committed: Boolean(input) &&
                    normalize(input.value) === normalize(expected) &&
                    input.getAttribute('aria-expanded') !== 'true' &&
                    visibleOptions.length === 0
            };
        }
        """,
        {
            "inputSelector": LOCATION_INPUT_SELECTOR,
            "listId": controlled_list_id,
            "expected": location_name,
        },
    )


async def verify_autocomplete_committed(
    page: Page,
    *,
    location_input: Locator,
    controlled_list_id: str,
    location_name: str,
    timeout_ms: int = AUTOCOMPLETE_COMMIT_TIMEOUT_MS,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    last_state: dict[str, Any] = {}

    while time.monotonic() < deadline:
        last_state = await autocomplete_commit_state(
            page,
            location_input=location_input,
            controlled_list_id=controlled_list_id,
            location_name=location_name,
        )
        if last_state.get("committed"):
            return
        await asyncio.sleep(0.1)

    raise SelectorError(
        "Autocomplete option did not commit/close. "
        f"Final state: {last_state!r}"
    )


async def type_location_and_select_suggestion(
    page: Page,
    capture: NetworkCapture,
    location_name: str,
) -> dict[str, Any]:
    """Populate and commit the visible city field without pointer clicks.

    The locator input is sometimes covered by its floating label or the sticky
    header. Pointer-based Locator.click() therefore retries indefinitely even
    though the input is visible and editable. This function focuses the exact
    input through the DOM, sends real keyboard events to that locator, and
    invokes the verified autocomplete option's native click handler.
    """

    if await page_shows_access_denied(page):
        raise RaymondJamesBlockedError(
            "The locator tab changed to an Access Denied page."
        )

    await page.bring_to_front()

    location_input = await find_location_input(page)
    await location_input.wait_for(state="visible", timeout=CONTROL_TIMEOUT_MS)

    capture.drain()

    # Focus through the DOM. Unlike Locator.click(), focus() does not require a
    # clear pointer path and is not blocked by the input's label or sticky menu.
    await location_input.evaluate("input => input.focus()")

    state_before = await location_input.evaluate(
        """
        input => ({
            value: input.value || '',
            disabled: Boolean(input.disabled),
            readOnly: Boolean(input.readOnly),
            active: document.activeElement === input,
            ariaExpanded: input.getAttribute('aria-expanded'),
            rect: (() => {
                const r = input.getBoundingClientRect();
                return {x: r.x, y: r.y, width: r.width, height: r.height};
            })()
        })
        """
    )
    print(f"      Input state before typing: {state_before}")

    if state_before.get("disabled") or state_before.get("readOnly"):
        raise SelectorError(f"#zip-v2 is not editable: {state_before!r}")
    if not state_before.get("active"):
        raise SelectorError(
            "#zip-v2 could not receive DOM focus. "
            f"Input state: {state_before!r}"
        )

    await location_input.press("Control+A", timeout=CONTROL_TIMEOUT_MS)
    await location_input.press("Backspace", timeout=CONTROL_TIMEOUT_MS)

    cleared_value = clean_text(await location_input.input_value())
    print(f"      Input after clear: {cleared_value!r}")
    if cleared_value:
        raise SelectorError(
            f"Could not clear #zip-v2; value remained {cleared_value!r}."
        )

    key_delay_ms = random.randint(*TYPE_DELAY_RANGE_MS)
    await location_input.type(
        location_name,
        delay=key_delay_ms,
        timeout=CONTROL_TIMEOUT_MS,
    )

    immediate_value = clean_text(await location_input.input_value())
    print(f"      Input immediately after typing: {immediate_value!r}")

    if normalize_location(immediate_value) != normalize_location(location_name):
        raise SelectorError(
            f"Keyboard typing did not populate #zip-v2. Expected "
            f"{location_name!r}, received {immediate_value!r}."
        )

    print(
        f"      Input populated by keyboard: {immediate_value} "
        f"({key_delay_ms} ms/key)"
    )

    # The autocomplete request is debounced by the website.
    autocomplete_response = await capture.wait_for(
        lambda response: is_autocomplete_response(
            response,
            location_name=location_name,
        )
    )
    suggestions = await read_json_response(
        autocomplete_response,
        description=f"autocomplete for {location_name}",
    )

    if not isinstance(suggestions, list) or not suggestions:
        raise RaymondJamesError(
            f"Autocomplete returned no suggestions for {location_name}: "
            f"{suggestions!r}"
        )

    candidates = [item for item in suggestions if isinstance(item, dict)]
    candidates.sort(
        key=lambda item: score_place_suggestion(item, location_name),
        reverse=True,
    )
    best = candidates[0]
    description = clean_text(
        best.get("Description") or best.get("description")
    )
    place_id = clean_text(best.get("placeid") or best.get("placeId"))

    if not place_id:
        raise RaymondJamesError(
            f"The best autocomplete suggestion had no Place ID: {best!r}"
        )

    suggestion_locator, controlled_list_id = await exact_autocomplete_option(
        page,
        location_input=location_input,
        location_name=location_name,
        api_description=description,
    )
    visible_option_text = clean_text(await suggestion_locator.inner_text())

    # Invoke the exact option's native click. This runs the page's own event
    # handler but avoids Playwright's pointer-actionability checks, which can
    # be blocked by the sticky header even when the option is correct.
    await suggestion_locator.evaluate(
        """
        option => {
            option.dispatchEvent(new MouseEvent('mousedown', {
                bubbles: true, cancelable: true, view: window
            }));
            option.dispatchEvent(new MouseEvent('mouseup', {
                bubbles: true, cancelable: true, view: window
            }));
            option.click();
        }
        """
    )

    # The site may keep the list element mounted but hidden. Verify the actual
    # committed state rather than requiring the list node to disappear.
    try:
        await verify_autocomplete_committed(
            page,
            location_input=location_input,
            controlled_list_id=controlled_list_id,
            location_name=location_name,
            timeout_ms=5_000,
        )
    except SelectorError:
        # If the option is still selected but not committed, Enter is the
        # widget's standard keyboard commit action.
        await location_input.evaluate("input => input.focus()")
        await location_input.press("Enter", timeout=CONTROL_TIMEOUT_MS)
        await verify_autocomplete_committed(
            page,
            location_input=location_input,
            controlled_list_id=controlled_list_id,
            location_name=location_name,
            timeout_ms=8_000,
        )

    print(f"      Autocomplete committed: {visible_option_text}")

    return {
        "place_id": place_id,
        "description": description,
        "visible_option_text": visible_option_text,
        "resolver": "raymond_james_visible_autocomplete_native_click",
    }


async def native_radius_select(page: Page, radius: int) -> tuple[Locator, str] | None:
    selects = page.locator("select")
    try:
        count = min(await selects.count(), 30)
    except Exception:
        return None

    target = str(radius)
    for index in range(count):
        select = selects.nth(index)
        try:
            if not await select.is_visible():
                continue
            options = await select.locator("option").evaluate_all(
                """
                options => options.map(option => ({
                    value: option.value,
                    text: (option.textContent || '').trim()
                }))
                """
            )
        except Exception:
            continue

        for option in options:
            value = clean_text(option.get("value"))
            text = clean_text(option.get("text"))
            if value == target or re.fullmatch(
                rf"{re.escape(target)}(?:\s*miles?)?",
                text,
                flags=re.I,
            ):
                return select, value
    return None


async def set_radius_control(page: Page, radius: int) -> bool:
    """Set the visible radius control and verify the resulting checked state."""

    if radius == 100:
        exact_radio = page.locator(RADIUS_100_SELECTOR).first
        if not await exact_radio.count():
            raise SelectorError("Could not find the exact #mileage-100 radio.")

        if await exact_radio.is_checked():
            print("      Radius already selected: 100 miles")
            return False

        # Use the radio's native click method. It applies the browser's normal
        # checked-state behavior and dispatches the site's click/change events,
        # without requiring an unobstructed pointer path to the hidden radio or
        # its label.
        await exact_radio.evaluate(
            """
            radio => {
                radio.click();
                if (!radio.checked) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('input', {bubbles: true}));
                    radio.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }
            """
        )

        await page.wait_for_function(
            "selector => document.querySelector(selector)?.checked === true",
            arg=RADIUS_100_SELECTOR,
            timeout=CONTROL_TIMEOUT_MS,
        )
        print("      Radius selected: 100 miles")
        return True

    native = await native_radius_select(page, radius)
    if native is not None:
        select, value = native
        current = clean_text(await select.input_value())
        if current == value:
            return False
        await select.select_option(value=value, timeout=CONTROL_TIMEOUT_MS)
        return True

    target_pattern = re.compile(
        rf"^\s*{re.escape(str(radius))}\s*(?:miles?)?\s*$",
        re.I,
    )
    radio = await first_visible([
        page.get_by_role("radio", name=target_pattern),
        page.get_by_label(target_pattern),
    ])
    if radio is not None:
        try:
            if await radio.is_checked():
                return False
        except Exception:
            pass
        await radio.evaluate("element => element.click()")
        return True

    raise SelectorError(f"Could not find a visible {radius}-mile radius control.")


async def pagination_control_is_enabled(locator: Locator) -> bool:
    try:
        if not await locator.is_visible():
            return False
        if await locator.is_disabled():
            return False
        if clean_text(await locator.get_attribute("aria-disabled")).casefold() == "true":
            return False
        class_name = clean_text(await locator.get_attribute("class")).casefold()
        if re.search(r"(?:^|\s)(?:disabled|inactive)(?:\s|$)", class_name):
            return False
        return True
    except Exception:
        return False


async def find_next_button(page: Page) -> Locator | None:
    # Current exact markup supplied from the live page.
    exact = page.locator(NEXT_PAGE_SELECTOR)
    try:
        if await exact.count():
            candidate = exact.first
            if await pagination_control_is_enabled(candidate):
                return candidate
    except Exception:
        pass

    # Semantic fallbacks remain for small markup changes.
    next_pattern = re.compile(
        r"^\s*Next(?:\s+(?:search results )?page)?\s*$",
        re.I,
    )

    candidates = [
        page.get_by_role(
            "button",
            name=re.compile(r"Next search results page", re.I),
        ),
        page.locator(
            'button.pagination-v2-result.next, '
            '[aria-label="Next search results page" i]'
        ),
        page.get_by_role("button", name=next_pattern),
        page.get_by_role("link", name=next_pattern),
        page.locator(
            'a[rel="next"], button[rel="next"], '
            '[aria-label="Next" i], [aria-label="Next Page" i], '
            '[title="Next" i], [title="Next Page" i]'
        ),
    ]

    for locator in candidates:
        try:
            count = min(await locator.count(), 20)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            if await pagination_control_is_enabled(candidate):
                return candidate

    return None


async def find_previous_button(page: Page) -> Locator | None:
    exact = page.locator(PREVIOUS_PAGE_SELECTOR)
    try:
        if await exact.count():
            candidate = exact.first
            if await pagination_control_is_enabled(candidate):
                return candidate
    except Exception:
        pass
    return None


async def element_is_in_viewport(locator: Locator) -> bool:
    try:
        return bool(await locator.evaluate(
            """
            element => {
                const rect = element.getBoundingClientRect();
                return rect.top >= 0 && rect.bottom <= window.innerHeight;
            }
            """
        ))
    except Exception:
        return False


async def slow_scroll_to(page: Page, locator: Locator) -> None:
    """Scroll in visible steps, similar to the successful manual test."""

    for _ in range(80):
        if await element_is_in_viewport(locator):
            break
        step = random.randint(*SCROLL_STEP_RANGE)
        await page.mouse.wheel(0, step)
        await asyncio.sleep(random.uniform(*SCROLL_DELAY_RANGE_SECONDS))

    await locator.scroll_into_view_if_needed(timeout=CONTROL_TIMEOUT_MS)


async def wait_for_results_render(page: Page) -> None:
    """Best-effort wait for the site to finish rendering after its JSON arrives."""

    # The server response can finish before cards and pagination are redrawn.
    await page.wait_for_timeout(600)

    loading_selectors = [
        '[aria-busy="true"]',
        '.loading-spinner:visible',
        '.spinner:visible',
        '.loader:visible',
        '[class*="loading" i]:visible',
    ]

    deadline = time.monotonic() + RESULT_RENDER_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        any_visible = False
        for selector in loading_selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    any_visible = True
                    break
            except Exception:
                continue
        if not any_visible:
            break
        await asyncio.sleep(0.25)

    await asyncio.sleep(random.uniform(*AFTER_RESULTS_DELAY_RANGE_SECONDS))


async def dump_ui_diagnostics(page: Page, directory: Path, label: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_") or "diagnostic"

    try:
        await page.screenshot(
            path=str(directory / f"{safe_label}.png"),
            full_page=True,
        )
    except Exception:
        pass

    try:
        (directory / f"{safe_label}.html").write_text(
            await page.content(),
            encoding="utf-8",
        )
    except Exception:
        pass

    try:
        controls = await page.locator(
            'input, select, button, a, [role="button"], '
            '[role="combobox"], [role="option"]'
        ).evaluate_all(
            """
            elements => elements.map((element, index) => ({
                index,
                tag: element.tagName,
                id: element.id || '',
                className: element.className || '',
                name: element.getAttribute('name'),
                type: element.getAttribute('type'),
                role: element.getAttribute('role'),
                text: (element.innerText || element.textContent || '').trim().slice(0, 300),
                value: element.value || '',
                placeholder: element.getAttribute('placeholder'),
                ariaLabel: element.getAttribute('aria-label'),
                title: element.getAttribute('title'),
                disabled: Boolean(element.disabled),
                checked: Boolean(element.checked),
                ariaDisabled: element.getAttribute('aria-disabled'),
                visible: Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length)
            }))
            """
        )
        atomic_write_json(directory / f"{safe_label}_controls.json", controls)
    except Exception:
        pass


# =============================================================================
# SEARCH AND PAGINATION THROUGH THE VISIBLE PAGE
# =============================================================================

async def wait_for_details_after_action(
    capture: NetworkCapture,
    action: Callable[[], Awaitable[None]],
    *,
    location_name: str,
    page_number: int,
    radius: int | None,
    place_id: str,
    action_description: str,
) -> tuple[dict[str, Any], Response]:
    capture.drain()
    await action()

    response = await capture.wait_for(
        lambda candidate: is_details_response(
            candidate,
            location_name=location_name,
            page_number=page_number,
            radius=radius,
            place_id=place_id,
        )
    )
    payload = await read_json_response(
        response,
        description=action_description,
    )
    if not isinstance(payload, dict):
        raise RaymondJamesError(
            f"Expected a JSON object during {action_description}: {payload!r}"
        )
    return payload, response


async def start_visible_search(
    page: Page,
    capture: NetworkCapture,
    *,
    location_name: str,
    radius: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    place = await type_location_and_select_suggestion(
        page,
        capture,
        location_name,
    )

    # Try to set the desired radius before submitting. If prior results are
    # already displayed, changing this control can itself issue a fresh page-1
    # request. The passive capture listener preserves that response.
    capture.drain()
    radius_changed = await set_radius_control(page, radius)

    response: Response | None = None
    payload: dict[str, Any] | None = None

    if radius_changed:
        response = await capture.wait_for_optional(
            lambda candidate: is_details_response(
                candidate,
                location_name=location_name,
                page_number=1,
                radius=radius,
                place_id=place["place_id"],
            ),
            timeout_seconds=5,
        )
        if response is not None:
            received = await read_json_response(
                response,
                description=f"visible radius change for {location_name}",
            )
            if not isinstance(received, dict):
                raise RaymondJamesError(
                    f"Expected a JSON object after changing the radius for "
                    f"{location_name}: {received!r}"
                )
            payload = received

    if payload is None:
        search_button = await find_search_button(page)

        async def click_search() -> None:
            # The Raymond James sticky header and floating navigation can
            # intercept a physical Playwright click on the submit button even
            # when the button is visible. Submit the button's own form through
            # the native browser form API instead. This changes only the
            # initial search submission; pagination below still scrolls through
            # each rendered page and physically clicks the visible Next button.
            await search_button.evaluate(
                """
                button => {
                    if (button.form &&
                        typeof button.form.requestSubmit === 'function') {
                        button.form.requestSubmit(button);
                    } else {
                        button.click();
                    }
                }
                """
            )

        payload, response = await wait_for_details_after_action(
            capture,
            click_search,
            location_name=location_name,
            page_number=1,
            radius=radius if radius_changed else None,
            place_id=place["place_id"],
            action_description=f"visible search for {location_name}",
        )

    await wait_for_results_render(page)

    assert response is not None
    actual_radius = int_or_default(
        payload.get("finalRadius", payload.get("requestedRadius")),
        int_or_default(query_first(response_query(response), "radius"), -1),
    )

    if actual_radius != radius:
        # The radius control was unavailable before the first search. Change it
        # now through the visible results-page control. The site may fetch page
        # 1 immediately on change; otherwise click Find again.
        capture.drain()
        changed = await set_radius_control(page, radius)
        if not changed:
            raise SelectorError(
                f"The results loaded at {actual_radius} miles, and the script "
                f"could not change the visible radius control to {radius} miles."
            )

        response = await capture.wait_for_optional(
            lambda candidate: is_details_response(
                candidate,
                location_name=location_name,
                page_number=1,
                radius=radius,
                place_id=place["place_id"],
            ),
            timeout_seconds=10,
        )

        if response is None:
            search_button = await find_search_button(page)

            async def click_search_again() -> None:
                # Same native form submission fallback used for the first
                # search. Visible page scrolling and Next-button clicks remain
                # unchanged.
                await search_button.evaluate(
                    """
                    button => {
                        if (button.form &&
                            typeof button.form.requestSubmit === 'function') {
                            button.form.requestSubmit(button);
                        } else {
                            button.click();
                        }
                    }
                    """
                )

            payload, response = await wait_for_details_after_action(
                capture,
                click_search_again,
                location_name=location_name,
                page_number=1,
                radius=radius,
                place_id=place["place_id"],
                action_description=(
                    f"visible {radius}-mile search for {location_name}"
                ),
            )
        else:
            received = await read_json_response(
                response,
                description=f"visible radius change for {location_name}",
            )
            if not isinstance(received, dict):
                raise RaymondJamesError(
                    f"Expected a JSON object after changing the radius for "
                    f"{location_name}: {received!r}"
                )
            payload = received

        await wait_for_results_render(page)

    final_radius = int_or_default(
        payload.get("finalRadius", payload.get("requestedRadius")),
        radius,
    )
    if final_radius != radius:
        raise RaymondJamesError(
            f"Requested {radius} miles for {location_name}, but the server "
            f"reported finalRadius={final_radius}."
        )

    return payload, place


async def click_visible_next_and_capture(
    page: Page,
    capture: NetworkCapture,
    *,
    location_name: str,
    expected_page: int,
    radius: int,
    place_id: str,
) -> dict[str, Any]:
    next_button = await find_next_button(page)
    if next_button is None:
        raise SelectorError(
            f"Could not find an enabled visible Next control for page {expected_page} "
            f"of the {location_name} search."
        )

    await slow_scroll_to(page, next_button)
    await asyncio.sleep(random.uniform(*BEFORE_NEXT_CLICK_DELAY_RANGE_SECONDS))

    async def click_next() -> None:
        await next_button.click(timeout=CONTROL_TIMEOUT_MS)

    payload, _ = await wait_for_details_after_action(
        capture,
        click_next,
        location_name=location_name,
        page_number=expected_page,
        radius=radius,
        place_id=place_id,
        action_description=(
            f"visible Next click for {location_name}, page {expected_page}"
        ),
    )
    await wait_for_results_render(page)
    return payload


# =============================================================================
# FLATTENING AND DEDUPLICATION
# =============================================================================

def make_advisor_record(
    advisor: dict[str, Any],
    location_result: dict[str, Any],
    *,
    source_location: str,
    source_seed_zip: str,
) -> dict[str, Any]:
    nested_branch = advisor.get("branch")
    branch = nested_branch if isinstance(nested_branch, dict) else location_result

    address = branch.get("address")
    if not isinstance(address, dict):
        address = location_result.get("address")
    if not isinstance(address, dict):
        address = {}

    subheaders = branch.get("subHeaders")
    if isinstance(subheaders, list):
        branch_subheaders_text = " | ".join(
            clean_text(value) for value in subheaders if clean_text(value)
        )
    else:
        branch_subheaders_text = clean_text(subheaders)

    designations = advisor.get("designations")
    if isinstance(designations, list):
        designations_text = " | ".join(
            clean_text(value) for value in designations if clean_text(value)
        )
    else:
        designations_text = clean_text(designations)

    branch_id = clean_text(branch.get("branchID"))
    branch_address = format_address(address)
    distance = float_or_none(location_result.get("distance"))

    return {
        "advisor_id": clean_text(advisor.get("advisorID")),
        "fa_number": clean_text(advisor.get("faNum")),
        "name": clean_text(advisor.get("name")),
        "designation": clean_text(advisor.get("designation")),
        "designations": designations_text,
        "title": clean_text(advisor.get("title")),
        "team_name": clean_text(advisor.get("teamName")),
        "phone": clean_text(advisor.get("phone")),
        "email": clean_text(advisor.get("emailAddress")),
        "website_url": clean_url(advisor.get("websiteUrl")),
        "advisor_profile_url": clean_url(advisor.get("advisorProfileUrl")),
        "contact_form_url": clean_url(advisor.get("contactFormUrl")),
        "photo_url": clean_url(advisor.get("photoUrl")),
        "advisor_subsidiary": clean_text(advisor.get("subsidiary")),
        "country": clean_text(advisor.get("country")),
        "role_code": clean_text(advisor.get("roleCode")),
        "position_code": clean_text(advisor.get("positionCode")),
        "class_code": clean_text(advisor.get("classCode")),
        "branch_id": branch_id,
        "branch_name": clean_text(branch.get("header")),
        "branch_subheaders": branch_subheaders_text,
        "branch_phone": clean_text(branch.get("phone")),
        "branch_email": clean_text(branch.get("emailAddress")),
        "branch_website_url": clean_url(branch.get("websiteUrl")),
        "branch_contact_form_url": clean_url(branch.get("contactFormUrl")),
        "branch_subsidiary": clean_text(branch.get("subsidiary")),
        "branch_type_code": clean_text(branch.get("typeCode")),
        "branch_subtype_code": clean_text(branch.get("subtypeCode")),
        "branch_is_alex_brown": clean_text(branch.get("isAlexBrown")),
        "address_line1": clean_text(address.get("line1")),
        "address_line2": clean_text(address.get("line2")),
        "address_line3": clean_text(address.get("line3")),
        "city": clean_text(address.get("city")),
        "state": clean_text(address.get("state")),
        "zip": clean_text(address.get("zip")),
        "latitude": float_or_none(address.get("latitude")),
        "longitude": float_or_none(address.get("longitude")),
        "min_distance_miles": distance,
        "nearest_search_center": source_location,
        "source_locations": "",
        "source_seed_zips": "",
        "all_branch_ids": "",
        "all_branch_addresses": "",
        "discovery_count": 1,
        "_source_locations": {source_location},
        "_source_seed_zips": {source_seed_zip},
        "_branch_ids": {branch_id} if branch_id else set(),
        "_branch_addresses": {branch_address} if branch_address else set(),
        "_primary_branch_address": branch_address,
    }


def extract_advisor_records(
    location_results: list[dict[str, Any]],
    *,
    source_location: str,
    source_seed_zip: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for result in location_results:
        advisors = result.get("advisors")
        if isinstance(advisors, list):
            for advisor in advisors:
                if isinstance(advisor, dict):
                    records.append(make_advisor_record(
                        advisor,
                        result,
                        source_location=source_location,
                        source_seed_zip=source_seed_zip,
                    ))
        elif result.get("advisorID"):
            records.append(make_advisor_record(
                result,
                result,
                source_location=source_location,
                source_seed_zip=source_seed_zip,
            ))
    return records


def advisor_dedup_key(record: dict[str, Any]) -> str:
    advisor_id = clean_text(record.get("advisor_id"))
    if advisor_id:
        return f"id:{advisor_id.casefold()}"
    email = clean_text(record.get("email")).casefold()
    if email:
        return f"email:{email}"
    profile = clean_text(record.get("advisor_profile_url")).casefold()
    if profile:
        return f"profile:{profile}"
    name = clean_text(record.get("name")).casefold()
    phone = re.sub(r"\D+", "", clean_text(record.get("phone")))
    address = clean_text(record.get("_primary_branch_address")).casefold()
    return f"fallback:{name}|{phone}|{address}"


def merge_advisor_record(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> None:
    existing_sources = existing.setdefault("_source_locations", set())
    incoming_sources = incoming.get("_source_locations", set())
    existing_sources.update(incoming_sources)

    existing.setdefault("_source_seed_zips", set()).update(
        incoming.get("_source_seed_zips", set())
    )
    existing.setdefault("_branch_ids", set()).update(
        incoming.get("_branch_ids", set())
    )
    existing.setdefault("_branch_addresses", set()).update(
        incoming.get("_branch_addresses", set())
    )

    old_distance = float_or_none(existing.get("min_distance_miles"))
    new_distance = float_or_none(incoming.get("min_distance_miles"))
    incoming_is_closer = (
        new_distance is not None
        and (old_distance is None or new_distance < old_distance)
    )

    if incoming_is_closer:
        existing["min_distance_miles"] = new_distance
        existing["nearest_search_center"] = incoming.get(
            "nearest_search_center", ""
        )
        for field in BRANCH_AND_ADDRESS_FIELDS:
            if not blank(incoming.get(field)):
                existing[field] = incoming[field]
        existing["_primary_branch_address"] = incoming.get(
            "_primary_branch_address", ""
        )

    excluded = {
        "min_distance_miles", "nearest_search_center", "source_locations",
        "source_seed_zips", "all_branch_ids", "all_branch_addresses",
        "discovery_count",
    }
    for field in CSV_FIELDS:
        if (
            field not in excluded
            and blank(existing.get(field))
            and not blank(incoming.get(field))
        ):
            existing[field] = incoming[field]

    # Idempotent across reruns: count distinct search centers, not repeated
    # downloads of the same page.
    existing["discovery_count"] = max(1, len(existing_sources))


def add_records(
    dataset: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
) -> tuple[int, int]:
    added = duplicates = 0
    for record in records:
        key = advisor_dedup_key(record)
        if key in dataset:
            merge_advisor_record(dataset[key], record)
            duplicates += 1
        else:
            record["discovery_count"] = max(
                1,
                len(record.get("_source_locations", set())),
            )
            dataset[key] = record
            added += 1
    return added, duplicates


def format_phone_for_excel(value: Any) -> str:
    """Return a phone number that Excel will not interpret as a formula.

    Raymond James commonly returns U.S. numbers such as +1-404-240-6700.
    A CSV opened directly in Excel may treat a leading plus sign as a formula.
    Ten-digit U.S. numbers, and eleven-digit numbers beginning with country
    code 1, are therefore written in conventional display form. Other phone
    values are preserved, except that formula-triggering leading characters
    are prefixed with a tab so spreadsheet software treats them as text.
    """
    phone = clean_text(value)
    if not phone:
        return ""

    digits = re.sub(r"\D+", "", phone)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    # Defensive CSV-injection protection for unusual non-U.S. values.
    if phone[0] in ("=", "+", "-", "@"):
        return "\t" + phone

    return phone


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    output = {field: record.get(field, "") for field in CSV_FIELDS}
    output["phone"] = format_phone_for_excel(output.get("phone"))
    output["branch_phone"] = format_phone_for_excel(
        output.get("branch_phone")
    )
    source_locations = set(record.get("_source_locations", set()))
    output["source_locations"] = " | ".join(sorted(source_locations))
    output["source_seed_zips"] = " | ".join(
        sorted(record.get("_source_seed_zips", set()))
    )
    output["all_branch_ids"] = " | ".join(
        sorted(record.get("_branch_ids", set()))
    )
    output["all_branch_addresses"] = " | ".join(
        sorted(record.get("_branch_addresses", set()))
    )
    output["discovery_count"] = max(1, len(source_locations))

    distance = float_or_none(output.get("min_distance_miles"))
    if distance is not None:
        output["min_distance_miles"] = round(distance, 4)
    for field in ("latitude", "longitude"):
        value = float_or_none(output.get(field))
        if value is not None:
            output[field] = round(value, 7)
    return output


def write_dataset_csv(
    path: Path,
    dataset: dict[str, dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    rows = [finalize_record(record) for record in dataset.values()]
    rows.sort(key=lambda row: (
        clean_text(row.get("state")).casefold(),
        clean_text(row.get("city")).casefold(),
        clean_text(row.get("name")).casefold(),
        clean_text(row.get("advisor_id")).casefold(),
    ))
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_existing_dataset(path: Path) -> dict[str, dict[str, Any]]:
    dataset: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return dataset

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            record: dict[str, Any] = {
                field: raw.get(field, "") for field in CSV_FIELDS
            }
            record["min_distance_miles"] = float_or_none(
                record.get("min_distance_miles")
            )
            record["latitude"] = float_or_none(record.get("latitude"))
            record["longitude"] = float_or_none(record.get("longitude"))
            record["_source_locations"] = {
                value.strip()
                for value in str(record.get("source_locations", "")).split("|")
                if value.strip()
            }
            record["_source_seed_zips"] = {
                value.strip()
                for value in str(record.get("source_seed_zips", "")).split("|")
                if value.strip()
            }
            record["_branch_ids"] = {
                value.strip()
                for value in str(record.get("all_branch_ids", "")).split("|")
                if value.strip()
            }
            record["_branch_addresses"] = {
                value.strip()
                for value in str(
                    record.get("all_branch_addresses", "")
                ).split("|")
                if value.strip()
            }
            record["_primary_branch_address"] = format_address({
                "line1": record.get("address_line1"),
                "line2": record.get("address_line2"),
                "line3": record.get("address_line3"),
                "city": record.get("city"),
                "state": record.get("state"),
                "zip": record.get("zip"),
            })
            record["discovery_count"] = max(
                1,
                len(record["_source_locations"]),
            )
            dataset[advisor_dedup_key(record)] = record
    return dataset


# =============================================================================
# COMMAND LINE AND MAIN WORKFLOW
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use the visible Raymond James locator UI to collect advisor "
            "records, passively capture its JSON responses, flatten branch "
            "addresses, deduplicate advisors, and save one CSV."
        )
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=DEFAULT_RADIUS_MILES,
        help="Visible search radius in miles. Default: 100.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "data" / "raw" / "firm_rosters",
        help="Default: ./data/raw/firm_rosters beneath the current working directory.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start a fresh CSV and completed-location checkpoint.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar='"CITY, ST"',
        help='Process one target center, e.g. --only "Memphis, TN". Repeatable.',
    )
    parser.add_argument(
        "--state",
        action="append",
        default=[],
        metavar="ST",
        help="Process all configured centers in one state. Repeatable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Process selected centers even if they are already in the completed "
            "checkpoint. Existing advisor rows are still deduplicated."
        ),
    )
    parser.add_argument(
        "--max-locations",
        type=int,
        default=None,
        help="Optional cap on target centers processed in this run.",
    )
    args = parser.parse_args()

    if not 1 <= args.radius <= 500:
        parser.error("--radius must be between 1 and 500.")
    if args.max_locations is not None and args.max_locations < 1:
        parser.error("--max-locations must be at least 1.")

    args.state = [clean_text(value).upper() for value in args.state]
    invalid_states = set(args.state) - EXPECTED_JURISDICTIONS
    if invalid_states:
        parser.error(
            "Unknown --state value(s): " + ", ".join(sorted(invalid_states))
        )
    return args


def selected_locations(
    names: list[str],
    states: list[str],
) -> list[dict[str, str]]:
    selected = list(TARGET_LOCATIONS)

    if names:
        wanted = {normalize_location(name) for name in names}
        selected = [
            item for item in selected
            if normalize_location(item["name"]) in wanted
        ]
        missing = wanted - {
            normalize_location(item["name"]) for item in selected
        }
        if missing:
            raise ValueError(
                "Unknown --only location(s): " + ", ".join(sorted(missing))
            )

    if states:
        wanted_states = set(states)
        selected = [
            item for item in selected
            if state_code_from_location(item["name"]) in wanted_states
        ]

    if not selected:
        raise ValueError("No target centers matched the supplied filters.")
    return selected


async def process_payload_page(
    payload: dict[str, Any],
    *,
    location_name: str,
    source_seed_zip: str,
    dataset: dict[str, dict[str, Any]],
    csv_path: Path,
) -> tuple[int, int, int, int]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise RaymondJamesError(
            f"Expected a results list for {location_name}."
        )

    records = extract_advisor_records(
        [item for item in results if isinstance(item, dict)],
        source_location=location_name,
        source_seed_zip=source_seed_zip,
    )
    added, duplicates = add_records(dataset, records)
    write_dataset_csv(csv_path, dataset)
    return len(results), len(records), added, duplicates


async def run_collection(args: argparse.Namespace) -> int:
    validate_target_locations()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / roster_path("raymond_james").name
    completed_path = output_dir / "raymond_james_completed_locations.json"
    failures_path = output_dir / "raymond_james_failures.json"
    diagnostics_dir = output_dir / "raymond_james_ui_diagnostics"

    locations = selected_locations(args.only, args.state)
    if args.max_locations is not None:
        locations = locations[: args.max_locations]

    if args.fresh:
        dataset: dict[str, dict[str, Any]] = {}
        completed_locations: set[str] = set()
        failures: dict[str, Any] = {}
    else:
        dataset = load_existing_dataset(csv_path)
        completed_data = load_json(completed_path, [])
        completed_locations = {
            clean_text(value)
            for value in completed_data
            if clean_text(value)
        }
        failures_data = load_json(failures_path, {})
        failures = failures_data if isinstance(failures_data, dict) else {}

    print(f"Output CSV: {csv_path}")
    print(f"Radius:     {args.radius} miles")
    selected_states = {
        state_code_from_location(item["name"]) for item in locations
    }
    print(f"Locations:  {len(locations)}")
    print(f"States/DC:  {len(selected_states)}")
    print(f"Existing deduplicated advisors: {len(dataset)}")
    print()

    ensure_chrome_running()
    started_at = time.monotonic()
    processed_this_run = 0

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.connect_over_cdp(
            CDP_HOST,
            timeout=30_000,
        )

        capture: NetworkCapture | None = None
        try:
            if not browser.contexts:
                raise RuntimeError(
                    "Chrome exposed no browser context through CDP."
                )

            context = browser.contexts[0]
            context.set_default_timeout(CONTROL_TIMEOUT_MS)
            page = await get_locator_page(context)
            capture = NetworkCapture(page)

            # 1. Attach a CDP session to the active page
            cdp_session = await context.new_cdp_session(page)

            for index, item in enumerate(locations, start=1):
                location_name = item["name"]
                source_seed_zip = clean_text(item.get("zip"))

                if (
                    not args.fresh
                    and not args.force
                    and location_name in completed_locations
                ):
                    print(
                        f"[{index:>3}/{len(locations)}] SKIP "
                        f"{location_name} - already completed"
                    )
                    continue

                print(f"[{index:>3}/{len(locations)}] {location_name}")

                try:
                    if await page_shows_access_denied(page):
                        raise RaymondJamesBlockedError(
                            "The active locator tab is displaying Access Denied."
                        )

                    first_payload, place = await start_visible_search(
                        page,
                        capture,
                        location_name=location_name,
                        radius=args.radius,
                    )

                    print(
                        f"      Place ID: {place['place_id']} | "
                        f"{place.get('description', '')} | "
                        f"clicked option {place.get('visible_option_text', '')!r} | "
                        f"visible UI"
                    )

                    current_page = int_or_default(first_payload.get("page"), 1)
                    total_pages = max(
                        1,
                        int_or_default(first_payload.get("totalPages"), 1),
                    )
                    total_results = int_or_default(
                        first_payload.get("totalResults"),
                        0,
                    )
                    final_radius = int_or_default(
                        first_payload.get("finalRadius"),
                        args.radius,
                    )

                    location_rows_total = 0
                    advisor_rows_seen = 0
                    new_unique_total = 0
                    duplicate_total = 0

                    payload = first_payload
                    while True:
                        results_count, advisor_count, added, duplicates = await process_payload_page(
                            payload,
                            location_name=location_name,
                            source_seed_zip=source_seed_zip,
                            dataset=dataset,
                            csv_path=csv_path,
                        )

                        location_rows_total += results_count
                        advisor_rows_seen += advisor_count
                        new_unique_total += added
                        duplicate_total += duplicates

                        print(
                            f"      Page {current_page}/{total_pages}: "
                            f"{results_count} locations | "
                            f"{advisor_count} advisor rows | "
                            f"{added} new unique | CSV saved"
                        )

                        if current_page >= total_pages:
                            break

                        expected_page = current_page + 1
                        payload = await click_visible_next_and_capture(
                            page,
                            capture,
                            location_name=location_name,
                            expected_page=expected_page,
                            radius=args.radius,
                            place_id=place["place_id"],
                        )
                        current_page = int_or_default(
                            payload.get("page"),
                            expected_page,
                        )

                    completed_locations.add(location_name)
                    failures.pop(location_name, None)
                    processed_this_run += 1

                    atomic_write_json(
                        completed_path,
                        sorted(completed_locations),
                    )
                    atomic_write_json(failures_path, failures)

                    print(
                        f"      COMPLETE: {total_results} reported locations, "
                        f"{total_pages} pages, final radius {final_radius} | "
                        f"{advisor_rows_seen} advisor rows seen | "
                        f"{new_unique_total} new unique | "
                        f"dataset total {len(dataset)}"
                    )

                    # ------------------------------------------------------------------
                    # 2. CLEANUP FOR THE TARGET URL (Runs after all pages are done)
                    # ------------------------------------------------------------------
                    from urllib.parse import urlparse
                    parsed_url = urlparse(page.url)
                    current_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

                    # Target site storage cleanup (Cookies, LocalStorage, CacheStorage, IndexedDB)
                    await cdp_session.send(
                        "Storage.clearDataForOrigin",
                        {
                            "origin": current_origin,
                            "storageTypes": "cookies,storage,cache_storage",
                        },
                    )

                    #one_hour_ago_ms = (time.time() - 3600) * 1000

                    # Clear browsing data generated in the last hour
                    #await cdp_session.send(
                    #    "BrowsingData.clear",
                    #    {
                    #        "dataTypes": [
                    #            "cacheStorage",
                    #            "fileSystems",
                    #            "indexedDB",
                    #            "localStorage",
                    #            "webSQL",
                    #            "serviceWorkers",
                    #        ],
                    #        "since": one_hour_ago_ms,
                    #    },
                    #)

                    # Navigate back to a clean state/landing page to reset state machine
                    #await page.goto("https://www.raymondjames.com/find-an-advisor", wait_until="domcontentloaded")
                    #await page.wait_for_timeout(2000)

                    # Clear general browser cookies & disk cache
                    # await cdp_session.send("Network.clearBrowserCookies")
                    # await cdp_session.send("Network.clearBrowserCache")

                    print(f"      CLEANUP: Cleared site data and cookies for {current_origin}")
                    # ------------------------------------------------------------------

                except RaymondJamesBlockedError as exc:
                    failures[location_name] = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    write_dataset_csv(csv_path, dataset)
                    atomic_write_json(
                        completed_path,
                        sorted(completed_locations),
                    )
                    atomic_write_json(failures_path, failures)
                    await dump_ui_diagnostics(
                        page,
                        diagnostics_dir,
                        f"blocked_{location_name}",
                    )
                    from urllib.parse import urlparse
                    parsed_url = urlparse(page.url)
                    current_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

                    # Target site storage cleanup (Cookies, LocalStorage, CacheStorage, IndexedDB)
                    await cdp_session.send(
                        "Storage.clearDataForOrigin",
                        {
                            "origin": current_origin,
                            "storageTypes": "cookies,storage,cache_storage",
                        },
                    )
                    print()
                    print("STOPPED ON HTTP 403 / ACCESS DENIED")
                    print(str(exc))
                    print()
                    print(
                        "Every successful page was already merged into the CSV. "
                        "The script did not reload, retry, clear cookies, or open "
                        "another page. A later run will start the incomplete city "
                        "normally at page 1 and deduplicate the repeated pages."
                    )
                    return 3

                except Exception as exc:
                    failures[location_name] = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    write_dataset_csv(csv_path, dataset)
                    atomic_write_json(failures_path, failures)
                    await dump_ui_diagnostics(
                        page,
                        diagnostics_dir,
                        f"failed_{location_name}",
                    )
                    print(f"      FAILED: {type(exc).__name__}: {exc}")

                if index < len(locations):
                    await asyncio.sleep(
                        random.uniform(*BETWEEN_LOCATIONS_DELAY_RANGE_SECONDS)
                    )

        finally:
            if capture is not None:
                capture.close()
            # Give event callbacks a brief chance to settle before Playwright
            # disconnects from the external CDP Chrome instance.
            await asyncio.sleep(0.25)
            await browser.close()

    write_dataset_csv(csv_path, dataset)
    atomic_write_json(completed_path, sorted(completed_locations))
    atomic_write_json(failures_path, failures)

    elapsed = time.monotonic() - started_at
    print()
    print("Finished.")
    print(f"  Processed this run:       {processed_this_run}")
    print(f"  Completed checkpoint total: {len(completed_locations)}")
    print(f"  Deduplicated advisors:    {len(dataset)}")
    print(f"  Failed target centers:    {len(failures)}")
    print(f"  Elapsed seconds:          {elapsed:.2f}")
    print(f"  CSV:                      {csv_path}")

    return 0 if not failures else 4


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_collection(args))
    except KeyboardInterrupt:
        print("\nStopped by user. Every completed page was already saved to CSV.")
        return 130
    except Exception as exc:
        print(f"\nFATAL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

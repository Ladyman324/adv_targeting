"""Fast, resumable Edward Jones advisor discovery and email extraction.
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once


The public search API may require browser session cookies.  The ``discover``
command therefore accepts an optional Cookie header.  The ``enrich`` command
can consume either its output or the JSON downloaded by the browser-console
script and fetch profile pages with bounded concurrency and rate limiting.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import logging
import json
import math
import random
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from lxml import etree, html


ROOT = Path(__file__).parents[1]
# Scraped firm rosters are INPUTS, not pipeline output: they live in
# data/raw/firm_rosters/ so a rebuild of data/output/ cannot overwrite them.
DEFAULT_OUTPUT = ROOT / "data" / "raw" / "firm_rosters"
API_URL = "https://www.edwardjones.com/api/v3/financial-advisor/results"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
URL_RE = re.compile(r"^https?://", re.I)


class RateLimiter:
    """Global request pacing with a shared backoff gate for all workers."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / requests_per_second
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = self.next_start - now
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_start = max(self.next_start, time.monotonic()) + self.interval

    async def penalize(self, seconds: float) -> None:
        """Pause every worker after rate limiting or a transient server failure."""
        async with self.lock:
            self.next_start = max(self.next_start, time.monotonic() + seconds)


def retry_delay(response: Optional[httpx.Response], attempt: int) -> float:
    if response is not None:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
    if response is not None and response.status_code == 429:
        return min(300.0, 60.0 * float(2 ** attempt)) + random.random()
    return min(60.0, float(2 ** attempt)) + random.random() * 0.25

def headers(cookie: Optional[str] = None) -> Dict[str, str]:
    values = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
        "Referer": "https://www.edwardjones.com/us-en/find-a-financial-advisor",
    }
    if cookie:
        values["Cookie"] = cookie
    return values


def read_cookie(args: argparse.Namespace) -> Optional[str]:
    if args.cookie and args.cookie_file:
        raise ValueError("Use either --cookie or --cookie-file, not both")
    if args.cookie_file:
        return Path(args.cookie_file).read_text(encoding="utf-8").strip()
    return args.cookie


async def request_with_retries(
    client: httpx.AsyncClient,
    url: str,
    limiter: RateLimiter,
    retries: int,
) -> httpx.Response:
    last_error: Optional[BaseException] = None
    response: Optional[httpx.Response] = None
    for attempt in range(retries + 1):
        await limiter.wait()
        try:
            response = await client.get(url)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_error = None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            response = None
        if attempt < retries:
            delay = retry_delay(response, attempt)
            logging.warning("Transient request failure for %s; global backoff %.1fs (attempt %s/%s)", url, delay, attempt + 1, retries + 1)
            await limiter.penalize(delay)
    if last_error:
        raise last_error
    assert response is not None
    return response

def api_url(location: str, distance: int, page: int) -> str:
    query = urlencode(
        {
            "q": location,
            "distance": distance,
            "distance_unit": "mi",
            "matchblock": "undefined",
            "searchtype": 2,
            "city-state-template": "true",
            "page": page,
        }
    )
    return "%s?%s" % (API_URL, query)


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_discovery_checkpoint(path: Path, fingerprint: str) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    pages: Dict[int, Dict[str, Any]] = {}
    metadata: Dict[str, Any] = {}
    if not path.exists():
        return pages, metadata
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("_meta"):
                metadata = value["_meta"]
            elif isinstance(value.get("page"), int) and isinstance(value.get("data"), dict):
                pages[value["page"]] = value["data"]
    if metadata.get("fingerprint") != fingerprint:
        raise RuntimeError("Discovery checkpoint belongs to a different query; rerun with --no-resume")
    return pages, metadata


def advisor_identity(record: Dict[str, Any]) -> Optional[str]:
    for field in ("faEntityId", "fid", "faUrl"):
        value = record.get(field)
        if value is not None and str(value).strip():
            return "%s:%s" % (field, value)
    return None


def validate_discovery(records: List[Dict[str, Any]], expected: int) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    unidentified = 0
    missing_urls = 0
    for record in records:
        identity = advisor_identity(record)
        if identity is None:
            unidentified += 1
            continue
        unique.setdefault(identity, record)
        if not find_profile_url(record, None):
            missing_urls += 1
    duplicates = len(records) - len(unique) - unidentified
    if unidentified or duplicates or len(unique) != expected or missing_urls:
        raise RuntimeError(
            "Discovery integrity check failed: expected=%s collected=%s unique=%s duplicates=%s "
            "unidentified=%s missing_profile_urls=%s. Checkpoint retained for retry."
            % (expected, len(records), len(unique), duplicates, unidentified, missing_urls)
        )
    return list(unique.values())


async def cdp_is_available(url: str) -> bool:
    endpoint = url.rstrip("/") + "/json/version"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(endpoint)
            return response.is_success
    except httpx.HTTPError:
        return False


def find_chrome_executable(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Chrome executable not found; pass --chrome-path")


async def connect_debug_chrome(playwright: Any, args: argparse.Namespace) -> Tuple[Any, Any, bool]:
    """Attach to local debugging Chrome, launching the persistent profile if absent."""
    cdp_url = getattr(args, "cdp_url", "http://127.0.0.1:9222")
    launched = False
    if not await cdp_is_available(cdp_url):
        if not getattr(args, "launch_chrome", True):
            raise RuntimeError("No Chrome debugging endpoint at %s and automatic launch is disabled" % cdp_url)
        executable = find_chrome_executable(getattr(args, "chrome_path", None))
        user_data = Path(getattr(args, "chrome_user_data_dir", Path(r"C:\SeleniumChrome")))
        user_data.mkdir(parents=True, exist_ok=True)
        command = [
            str(executable),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%s" % debug_port,
            "--user-data-dir=%s" % user_data,
            "--no-first-run",
            "--no-default-browser-check",
            getattr(args, "bootstrap_url", "https://www.edwardjones.com/us-en/find-a-financial-advisor"),
        ]
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        launched = True
        deadline = time.monotonic() + getattr(args, "chrome_start_timeout", 30.0)
        while time.monotonic() < deadline:
            if await cdp_is_available(cdp_url):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Chrome launched but CDP endpoint did not become available at %s" % cdp_url)

    browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=int(args.timeout * 1000))
    if not browser.contexts:
        raise RuntimeError("Attached Chrome has no usable browser context")
    context = browser.contexts[0]
    print(("Launched and attached to" if launched else "Attached to existing") + " Chrome at " + cdp_url)
    logging.info("CDP Chrome connection: url=%s launched=%s", cdp_url, launched)
    return browser, context, launched

async def discover_in_browser(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Bootstrap a browser session and resume/checkpoint all advisor API pages."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Automatic discovery requires Playwright: pip install playwright") from exc

    fingerprint = stable_fingerprint({"location": args.location, "distance": args.distance, "api": API_URL})
    cached_pages: Dict[int, Dict[str, Any]] = {}
    cached_meta: Dict[str, Any] = {}
    if args.resume:
        cached_pages, cached_meta = load_discovery_checkpoint(args.discovery_checkpoint, fingerprint)
    args.discovery_checkpoint.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser, context, _ = await connect_debug_chrome(playwright, args)
        page = await context.new_page()
        limiter = RateLimiter(args.discovery_rate)
        api_requests = 0
        checkpoint_lock = asyncio.Lock()
        session_lock = asyncio.Lock()
        try:
            await page.goto(args.bootstrap_url, wait_until="domcontentloaded", timeout=int(args.timeout * 1000))
            await page.wait_for_timeout(1500)

            async def refresh_session() -> None:
                async with session_lock:
                    logging.warning("Refreshing browser session after authorization failure")
                    await page.goto(args.bootstrap_url, wait_until="domcontentloaded", timeout=int(args.timeout * 1000))
                    await page.wait_for_timeout(1000)

            async def fetch_page(page_number: int, page_size: Optional[int] = None, retry: bool = True) -> Dict[str, Any]:
                nonlocal api_requests
                attempts = args.retries + 1 if retry else 1
                last_message = "unknown browser request failure"
                for attempt in range(attempts):
                    await limiter.wait()
                    api_requests += 1
                    try:
                        result = await page.evaluate(
                            """async url => {
                                const response = await fetch(url, {credentials: "include"});
                                const text = await response.text();
                                if (!response.ok) return {status: response.status, error: text.slice(0, 500)};
                                return {status: response.status, data: JSON.parse(text)};
                            }""",
                            api_url(args.location, args.distance, page_number)
                            + ("&pageSize=%s" % page_size if page_size else ""),
                        )
                        status = int(result["status"])
                        if status == 200:
                            return result["data"]
                        if status == 429:
                            last_message = "HTTP 429 (rate limited / Cloudflare challenge)"
                        elif status == 503:
                            last_message = "HTTP 503 (temporarily unavailable)"
                        else:
                            last_message = "HTTP %s: %s" % (status, result.get("error", "")[:200])
                        if status in {401, 403} and attempt < attempts - 1:
                            await refresh_session()
                        elif status == 429 and attempt < attempts - 1:
                            delay = min(300.0, 60.0 * float(2 ** attempt)) + random.random()
                            logging.warning("Cloudflare throttled discovery page %s; global cooldown %.1fs", page_number, delay)
                            await limiter.penalize(delay)
                            continue
                        elif status not in {429, 500, 502, 503, 504}:
                            break
                    except Exception as exc:
                        last_message = "%s: %s" % (type(exc).__name__, exc)
                    if attempt < attempts - 1:
                        delay = min(60.0, float(2 ** attempt)) + random.random() * 0.25
                        logging.warning("Discovery page %s failed; global backoff %.1fs (%s)", page_number, delay, last_message)
                        await limiter.penalize(delay)
                raise RuntimeError("Browser search page %s failed after %s attempts: %s" % (page_number, attempts, last_message))

            first_data = await fetch_page(1)
            per_page = int(first_data.get("itemsPerPage") or 16)
            result_count = int(first_data.get("resultCount") or len(first_data.get("results", [])))
            total_pages = max(1, math.ceil(result_count / per_page))
            if cached_meta and (
                int(cached_meta.get("result_count", -1)) != result_count
                or int(cached_meta.get("per_page", -1)) != per_page
            ):
                raise RuntimeError("Discovery result count changed since checkpoint; rerun with --no-resume")

            pages: Dict[int, Dict[str, Any]] = dict(cached_pages)
            pages[1] = first_data
            mode = "a" if args.resume and args.discovery_checkpoint.exists() else "w"
            with args.discovery_checkpoint.open(mode, encoding="utf-8", buffering=1) as checkpoint:
                if mode == "w":
                    checkpoint.write(json.dumps({"_meta": {"fingerprint": fingerprint, "result_count": result_count, "per_page": per_page}}) + "\n")
                checkpoint.write(json.dumps({"page": 1, "data": first_data}, ensure_ascii=False) + "\n")

                if (
                    args.bulk_discovery
                    and result_count <= args.bulk_max_results
                    and result_count > len(first_data.get("results", []))
                ):
                    try:
                        bulk_data = await fetch_page(1, result_count, retry=False)
                    except RuntimeError as exc:
                        print("Bulk discovery unavailable (%s); falling back to resumable pagination" % exc)
                    else:
                        if len(bulk_data.get("results", [])) == result_count:
                            pages = {1: bulk_data}
                            total_pages = 1
                        else:
                            print("Bulk discovery incomplete; falling back to resumable pagination")

                queue: asyncio.Queue = asyncio.Queue()
                for page_number in range(2, total_pages + 1):
                    if page_number not in pages:
                        queue.put_nowait(page_number)
                cached_count = total_pages - queue.qsize()
                if cached_count > 1:
                    print("Resuming discovery with %s/%s pages already checkpointed" % (cached_count, total_pages))

                async def worker() -> None:
                    while True:
                        try:
                            page_number = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        try:
                            data = await fetch_page(page_number)
                            pages[page_number] = data
                            async with checkpoint_lock:
                                checkpoint.write(json.dumps({"page": page_number, "data": data}, ensure_ascii=False) + "\n")
                        finally:
                            queue.task_done()

                worker_count = min(args.browser_concurrency, max(1, queue.qsize()))
                await asyncio.gather(*(worker() for _ in range(worker_count)))

            cookie_values = await context.cookies()
            if not args.cookie and not args.cookie_file:
                args.cookie = "; ".join("%s=%s" % (item["name"], item["value"]) for item in cookie_values)
        finally:
            await page.close()

    advisors: List[Dict[str, Any]] = []
    for page_number in sorted(pages):
        values = pages[page_number].get("results", [])
        if isinstance(values, list):
            advisors.extend(value for value in values if isinstance(value, dict))
    advisors = validate_discovery(advisors, result_count)
    args.discovery_checkpoint.unlink(missing_ok=True)
    print("Discovered %s unique advisors with %s API requests; integrity checks passed" % (len(advisors), api_requests))
    logging.info("Discovery complete: advisors=%s api_requests=%s", len(advisors), api_requests)
    return advisors

async def run_all(args: argparse.Namespace) -> None:
    records = await discover_in_browser(args)
    await enrich(args, records)

async def discover(args: argparse.Namespace) -> None:
    cookie = read_cookie(args)
    limiter = RateLimiter(args.rate)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        headers=headers(cookie), follow_redirects=True, timeout=args.timeout, limits=limits
    ) as client:
        first = await request_with_retries(
            client, api_url(args.location, args.distance, 1), limiter, args.retries
        )
        if first.status_code == 401:
            raise RuntimeError(
                "Search API returned 401. Supply a current browser Cookie header with "
                "--cookie-file, or run the console script and use its JSON with enrich."
            )
        first.raise_for_status()
        first_data = first.json()
        per_page = int(first_data.get("itemsPerPage") or 16)
        result_count = int(first_data.get("resultCount") or len(first_data.get("results", [])))
        total_pages = max(1, math.ceil(result_count / per_page))
        pages: Dict[int, Dict[str, Any]] = {1: first_data}

        queue: asyncio.Queue = asyncio.Queue()
        for page in range(2, total_pages + 1):
            queue.put_nowait(page)

        async def worker() -> None:
            while True:
                try:
                    page = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    response = await request_with_retries(
                        client, api_url(args.location, args.distance, page), limiter, args.retries
                    )
                    response.raise_for_status()
                    pages[page] = response.json()
                finally:
                    queue.task_done()

        await asyncio.gather(*(worker() for _ in range(min(args.concurrency, total_pages))))

    advisors: List[Dict[str, Any]] = []
    for page in sorted(pages):
        values = pages[page].get("results", [])
        if isinstance(values, list):
            advisors.extend(values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(advisors, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Saved %s advisors from %s pages to %s" % (len(advisors), total_pages, args.output))


def nested_values(value: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = "%s.%s" % (path, key) if path else key
            yield from nested_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from nested_values(child, "%s[%s]" % (path, index))
    elif isinstance(value, str):
        yield path, value


def get_field(record: Dict[str, Any], dotted_path: str) -> Any:
    value: Any = record
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def find_profile_url(record: Dict[str, Any], url_field: Optional[str]) -> Optional[str]:
    if url_field:
        value = get_field(record, url_field)
        return value if isinstance(value, str) else None

    candidates: List[Tuple[int, str]] = []
    for path, value in nested_values(record):
        is_absolute = bool(URL_RE.match(value))
        is_relative = value.startswith("/") and "url" in path.lower()
        if not (is_absolute or is_relative):
            continue
        score = 0
        lowered = (path + " " + value).lower()
        field_name = path.rsplit(".", 1)[-1].lower()
        if field_name in {"faurl", "profileurl", "profile_url"}:
            score += 50
        if "/financial-advisor/" in value.lower():
            score += 20
        if "contact" in field_name:
            score -= 20
        if "edwardjones.com" in lowered:
            score += 10
        if any(word in lowered for word in ("advisor", "financial-advisor", "profile", "url")):
            score += 5
        candidates.append((score, value))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def load_advisors(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("results", data.get("advisors"))
    if not isinstance(data, list):
        raise ValueError("Input JSON must be an advisor array or contain results/advisors")
    return [row for row in data if isinstance(row, dict)]


def extract_emails(page_html: str) -> Tuple[List[str], str]:
    try:
        tree = html.fromstring(page_html)
    except (ValueError, etree.ParserError):
        return [], "invalid_html"
    nodes = tree.xpath('//input[@name="field_email_override"]/@value')
    if not nodes:
        nodes = tree.xpath('//input[@data-drupal-selector="edit-field-email-override"]/@value')
    if not nodes:
        return [], "field_missing"
    emails: Set[str] = set()
    for raw in nodes:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            values = [raw]
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            emails.update(str(value).strip().lower() for value in values if EMAIL_RE.match(str(value).strip()))
    return sorted(emails), "ok" if emails else "field_empty"


def load_enrichment_checkpoint(path: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    metadata: Dict[str, Any] = {}
    if not path.exists():
        return results, metadata
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("_meta"):
                metadata = value["_meta"]
            elif value.get("profile_url"):
                results[value["profile_url"]] = value
    return results, metadata


NICKNAMES = {
    "bill": {"william"}, "will": {"william"}, "bob": {"robert"},
    "rob": {"robert"}, "dick": {"richard"}, "rick": {"richard"},
    "jim": {"james"}, "jimmy": {"james"}, "tom": {"thomas"},
    "tony": {"anthony"}, "mike": {"michael"}, "dave": {"david"},
    "dan": {"daniel"}, "danny": {"daniel"}, "joe": {"joseph"},
    "steve": {"stephen", "steven"}, "chris": {"christopher"},
    "matt": {"matthew"}, "nick": {"nicholas"}, "ted": {"edward", "theodore"},
    "ed": {"edward"}, "greg": {"gregory"}, "jeff": {"jeffrey"},
    "ken": {"kenneth"}, "larry": {"lawrence"}, "tim": {"timothy"},
    "ron": {"ronald"}, "don": {"donald"}, "pat": {"patrick", "patricia"},
    "sue": {"susan"}, "beth": {"elizabeth"}, "liz": {"elizabeth"},
    "kate": {"katherine", "kathryn"}, "katie": {"katherine", "kathryn"},
    "peggy": {"margaret"}, "meg": {"margaret"}, "cathy": {"catherine"},
    "jen": {"jennifer"}, "jenny": {"jennifer"}, "andy": {"andrew"},
    "charlie": {"charles"}, "chuck": {"charles"}, "frank": {"francis"},
    "hank": {"henry"}, "jack": {"john"}, "johnny": {"john"},
}


def name_tokens(text: str) -> List[str]:
    cleaned = re.sub(r"[^a-z ]", " ", str(text or "").lower())
    drop = {"jr", "sr", "ii", "iii", "iv", "cfp", "aams", "chfc", "clu", "crpc",
            "cfa", "cpa", "aif", "cima", "crps", "adpa", "cepa", "mba"}
    return [t for t in cleaned.split() if len(t) > 1 and t not in drop]


def own_email(name: str, candidates: List[str]) -> Optional[str]:
    """The address belonging to THIS advisor, out of the branch's list.

    Every Edward Jones profile publishes the whole branch's addresses, and this
    file used to store `emails[0]`. The list is SORTED, so first meant
    alphabetically first, not the advisor's own: Paul Harrison shipped
    christopher.tavel@edwardjones.com while paul.harrison@edwardjones.com sat
    one element away in the same list. 16,329 of the 19,657 records carry more
    than one address, so this was not a rare edge.

    Surname is the test, not position. Where no candidate carries this
    advisor's surname the answer is None rather than a colleague's address --
    which is the whole point of the function.

    build_contacts.py applies the same rule when it reads the `emails` column,
    so the map was already right; this fixes the roster itself, which is read
    directly often enough to matter.
    """
    if not candidates:
        return None
    tokens = name_tokens(name)
    if not tokens:
        return None
    given, surname = tokens[0], tokens[-1]
    forms = {given} | NICKNAMES.get(given, set())

    def parts(address: str) -> set:
        return {t for t in re.split(r"[._\-]+", address.split("@")[0].lower()) if t}

    # Surname AND given name is the strongest signal, and settles the branches
    # that are FAMILY -- a father and son at one desk share both a surname and
    # a mailbox list.
    for address in candidates:
        piece = parts(address)
        if surname in piece and forms & piece:
            return address
    # An initial stands for the name: j.mitchell@ is James Mitchell.
    for address in candidates:
        piece = parts(address)
        if surname in piece and any(len(t) == 1 and t == given[0] for t in piece):
            return address
    matches = [a for a in candidates if surname in parts(a)]
    return matches[0] if len(matches) == 1 else None


def branch_key(record: Dict[str, Any]) -> str:
    address = re.sub(r"[^a-z0-9]+", " ", str(record.get("address") or "").lower()).strip()
    zipcode = str(record.get("faZipCode") or "").strip()[:5]
    return f"{address}|{zipcode}" if address else ""


def assign_branches(records: List[Dict[str, Any]]) -> None:
    """Give every advisor a branch id, from what the emails and the address say.

    Edward Jones publishes a formal practice -- `faBranchTeam` -- for only
    1,000 of 19,657 advisors. For everyone else the roster has no grouping at
    all, and two advisors at one desk look as unrelated as two in different
    states.

    The email list is the lever, but NOT in the obvious way. It is the advisor
    plus their branch support staff, not other advisors, which is why 19,656 of
    the 19,657 lists are unique and grouping on the list itself yields nothing.
    What it does give is an edge: two advisors sharing a branch office
    administrator's address are at the same branch. James Herndon and Kim
    Hudson both list meghan.greenwalt@ and michal.duchon@.

    Address alone is broader but coarser, so both are used as edges of one
    graph and the connected components are the branches.

    CHECKED against Edward Jones's own answer: of the 573 published practices,
    569 fall wholly inside a single derived branch and only 3 branches contain
    more than one practice. So the grouping does not contradict the firm where
    the firm states it.

    A BRANCH IS NOT A PRACTICE, and the two are kept in separate columns. A
    branch is who shares a door; a practice is who shares a book. Merging them
    would repeat the mistake this project just unwound at Merrill, where an
    office code sat in the team column and 134 people appeared to be one team.
    """
    parent: Dict[Any, Any] = {}

    def find(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for record in records:
        me = ("advisor", str(record.get("faEntityId") or id(record)))
        for address in record.get("emails") or []:
            union(me, ("email", str(address).lower()))
        key = branch_key(record)
        if key:
            union(me, ("branch", key))

    members: Dict[Any, List[Dict[str, Any]]] = {}
    for record in records:
        root = find(("advisor", str(record.get("faEntityId") or id(record))))
        members.setdefault(root, []).append(record)

    for root, group in members.items():
        # A branch of one is not a grouping; leaving it blank keeps the column
        # honest and stops the map drawing 9,263 single-person "branches".
        if len(group) < 2:
            for record in group:
                record["branch_id"] = ""
                record["branch_size"] = 1
            continue
        # Named for where it is, never for who is in it: an advisor leaves and
        # a name built from surnames changes underneath a stable id.
        sample = group[0]
        city = str(sample.get("faCity") or "").strip()
        state = str(sample.get("faState") or "").strip()
        street = str(sample.get("address") or "").split(",")[0].strip()
        label = f"Edward Jones - {city}, {state}" + (f" ({street})" if street else "")
        ident = "ej-" + hashlib.sha1(
            branch_key(sample).encode("utf-8")).hexdigest()[:10]
        for record in group:
            record["branch_id"] = ident
            record["branch_name"] = label
            record["branch_size"] = len(group)

    for record in records:
        record.setdefault("branch_id", "")
        record.setdefault("branch_name", "")
        record.setdefault("branch_size", 1)
        # The published practice, unpacked from the JSON blob it arrives in so
        # the column is readable without parsing it again downstream.
        practice = record.get("faBranchTeam")
        name = ident = ""
        if isinstance(practice, dict):
            name, ident = practice.get("team_name") or "", practice.get("entity_id") or ""
        elif isinstance(practice, str) and practice.startswith("{"):
            try:
                blob = json.loads(practice)
                name, ident = blob.get("team_name") or "", blob.get("entity_id") or ""
            except ValueError:
                pass
        record["practice_name"] = name
        record["practice_id"] = ident


def write_enriched_records(records: List[Dict[str, Any]], results: Dict[str, Dict[str, Any]], output_path: Path, url_field: Optional[str]) -> None:
    base = "https://www.edwardjones.com"
    enriched: List[Dict[str, Any]] = []
    fieldnames: List[str] = []
    seen_fields: Set[str] = set()
    for original in records:
        record = dict(original)
        found_url = find_profile_url(original, url_field)
        profile_url = urljoin(base, found_url) if found_url else None
        result = results.get(profile_url, {}) if profile_url else {}
        emails = result.get("emails") or []
        record["email"] = own_email(original.get("faName"), emails)
        record["emails"] = emails
        record["email_status"] = result.get("status", "profile_url_missing" if not profile_url else "not_processed")
        record["email_source_url"] = profile_url
        if result.get("http_status") is not None:
            record["email_http_status"] = result["http_status"]
        if result.get("error"):
            record["email_error"] = result["error"]
        enriched.append(record)

    assign_branches(enriched)
    for record in enriched:
        for field in record:
            if field not in seen_fields:
                seen_fields.add(field)
                fieldnames.append(field)

    def csv_value(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return value

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in enriched:
            writer.writerow({field: csv_value(value) for field, value in record.items()})
    temporary.replace(output_path)


async def browser_cookie(args: argparse.Namespace) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Session renewal requires Playwright") from exc
    async with async_playwright() as playwright:
        browser, context, _ = await connect_debug_chrome(playwright, args)
        page = await context.new_page()
        try:
            await page.goto(
                getattr(args, "bootstrap_url", "https://www.edwardjones.com/us-en/find-a-financial-advisor"),
                wait_until="domcontentloaded",
                timeout=int(args.timeout * 1000),
            )
            await page.wait_for_timeout(1000)
            values = await context.cookies()
            return "; ".join("%s=%s" % (item["name"], item["value"]) for item in values)
        finally:
            await page.close()

async def enrich(args: argparse.Namespace, records: Optional[List[Dict[str, Any]]] = None) -> None:
    if records is None:
        records = load_advisors(args.input)
    base = "https://www.edwardjones.com"
    urls = {
        urljoin(base, value)
        for record in records
        for value in [find_profile_url(record, args.url_field)]
        if value
    }
    if not urls:
        raise RuntimeError("No profile URLs found. Inspect the JSON and pass --url-field FIELD.NAME")

    fingerprint = stable_fingerprint({"urls": sorted(urls), "extractor_version": 2})
    prior_results: Dict[str, Dict[str, Any]] = {}
    prior_meta: Dict[str, Any] = {}
    if args.resume:
        prior_results, prior_meta = load_enrichment_checkpoint(args.checkpoint)
        if prior_meta and prior_meta.get("fingerprint") != fingerprint:
            raise RuntimeError("Email checkpoint belongs to a different advisor set; rerun with --no-resume")
        if args.checkpoint.exists() and not prior_meta:
            raise RuntimeError("Legacy email checkpoint has no fingerprint; rerun with --no-resume")

    terminal_statuses = {"ok", "field_missing", "field_empty", "not_found"}
    done = {url for url, result in prior_results.items() if result.get("status") in terminal_statuses}
    pending = sorted(urls - done)
    print("Found %s unique profile URLs; %s already complete; %s pending" % (len(urls), len(done), len(pending)))

    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    cookie = read_cookie(args)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    output_lock = asyncio.Lock()
    refresh_lock = asyncio.Lock()
    session_generation = 0
    completed = 0
    mode = "a" if args.resume and args.checkpoint.exists() else "w"

    with args.checkpoint.open(mode, encoding="utf-8", buffering=1) as output:
        if mode == "w":
            output.write(json.dumps({"_meta": {"fingerprint": fingerprint, "created_at": datetime.now(timezone.utc).isoformat()}}) + "\n")
        async with httpx.AsyncClient(
            headers=headers(cookie), follow_redirects=True, timeout=args.timeout, limits=limits
        ) as client:

            async def refresh_session(observed_generation: int) -> None:
                nonlocal session_generation
                async with refresh_lock:
                    if session_generation != observed_generation:
                        return
                    logging.warning("Refreshing profile-session cookies after 401/403")
                    refreshed = await browser_cookie(args)
                    if refreshed:
                        client.headers["Cookie"] = refreshed
                        args.cookie = refreshed
                    session_generation += 1

            async def process_batch(batch: List[str], limiter: RateLimiter, label: str) -> None:
                nonlocal completed
                queue: asyncio.Queue = asyncio.Queue()
                for url in batch:
                    queue.put_nowait(url)

                async def worker() -> None:
                    nonlocal completed
                    while True:
                        try:
                            url = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        row: Dict[str, Any] = {"profile_url": url, "emails": []}
                        try:
                            generation = session_generation
                            response = await request_with_retries(client, url, limiter, args.retries)
                            if response.status_code in {401, 403}:
                                await refresh_session(generation)
                                response = await request_with_retries(client, url, limiter, 1)
                            row["http_status"] = response.status_code
                            if response.is_success:
                                row["emails"], row["status"] = extract_emails(response.text)
                            elif response.status_code == 404:
                                row["status"] = "not_found"
                            else:
                                row["status"] = "http_error"
                        except Exception as exc:
                            row.update(status="request_error", error="%s: %s" % (type(exc).__name__, exc))
                        async with output_lock:
                            output.write(json.dumps(row, ensure_ascii=False) + "\n")
                            completed += 1
                            if completed % args.progress_every == 0:
                                print("%s: completed %s requests" % (label, completed))
                        queue.task_done()

                workers = min(args.concurrency, max(1, len(batch)))
                await asyncio.gather(*(worker() for _ in range(workers)))

            await process_batch(pending, RateLimiter(args.rate), "Primary pass")
            current_results, _ = load_enrichment_checkpoint(args.checkpoint)
            retryable = sorted(
                url for url in pending
                if current_results.get(url, {}).get("status") in {"http_error", "request_error", "invalid_html"}
            )
            if retryable:
                print("Starting targeted slower retry pass for %s profiles" % len(retryable))
                await process_batch(retryable, RateLimiter(max(0.1, args.rate / 2.0)), "Retry pass")

    results, _ = load_enrichment_checkpoint(args.checkpoint)
    write_enriched_records(records, results, args.output, args.url_field)
    statuses = Counter(results.get(url, {}).get("status", "not_processed") for url in urls)
    missing_profile_urls = sum(1 for record in records if not find_profile_url(record, args.url_field))
    email_count = sum(1 for url in urls if results.get(url, {}).get("emails"))
    unfinished = urls - {url for url, result in results.items() if result.get("status") in terminal_statuses}

    print("\nRun summary")
    print("  Advisor records:      %s" % len(records))
    print("  Unique profile URLs:  %s" % len(urls))
    print("  Emails extracted:     %s" % email_count)
    print("  Missing profile URLs: %s" % missing_profile_urls)
    for status, count in sorted(statuses.items()):
        print("  %-20s %s" % (status + ":", count))
    print("  Final CSV:            %s" % args.output)
    logging.info("Enrichment complete: records=%s urls=%s emails=%s unfinished=%s statuses=%s", len(records), len(urls), email_count, len(unfinished), dict(statuses))
    if not unfinished:
        args.checkpoint.unlink(missing_ok=True)
    else:
        print("  Resume checkpoint:    %s (%s retryable profiles)" % (args.checkpoint, len(unfinished)))

def add_network_args(parser: argparse.ArgumentParser, default_rate: float) -> None:
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rate", type=float, default=default_rate, help="Global request starts per second")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--cookie", help="Browser Cookie header (visible in process listings)")
    parser.add_argument("--cookie-file", help="Preferred: UTF-8 file containing the Cookie header")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_OUTPUT / "superseded" / "edward_jones_run.log")


def add_cdp_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bootstrap-url", default="https://www.edwardjones.com/us-en/find-a-financial-advisor")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--chrome-path", help="Chrome executable; auto-detected by default")
    parser.add_argument("--chrome-user-data-dir", type=Path, default=Path(r"C:\SeleniumChrome"))
    parser.add_argument("--chrome-start-timeout", type=float, default=30.0)
    parser.set_defaults(launch_chrome=True)
    parser.add_argument("--launch-chrome", dest="launch_chrome", action="store_true", help="Launch visible debug Chrome when port 9222 is unavailable (default)")
    parser.add_argument("--no-launch-chrome", dest="launch_chrome", action="store_false")
    parser.add_argument("--show-browser", action="store_true", help=argparse.SUPPRESS)

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discovery = subparsers.add_parser("discover", help="Download advisor records from the search API")
    discovery.add_argument("--location", required=True, help='For example: "Atlanta, Georgia"')
    discovery.add_argument("--distance", type=int, default=10000)
    discovery.add_argument("--output", type=Path,
                       default=scratch_path("edward_jones", "discovery"))
    add_network_args(discovery, default_rate=3.0)

    combined = subparsers.add_parser("run", help="Discover advisors, enrich emails, and write one final CSV")
    combined.add_argument("--location", required=True, help='For example: "Atlanta, Georgia"')
    combined.add_argument("--distance", type=int, default=10000)
    combined.add_argument("--output", type=Path, default=roster_path("edward_jones"))
    combined.add_argument("--url-field", help="Dotted path of profile URL if auto-detection is wrong")
    combined.add_argument("--checkpoint", type=Path, default=DEFAULT_OUTPUT / ".edward_jones_email_checkpoint.jsonl", help=argparse.SUPPRESS)
    combined.add_argument("--discovery-checkpoint", type=Path, default=DEFAULT_OUTPUT / ".edward_jones_discovery_checkpoint.jsonl", help=argparse.SUPPRESS)
    combined.add_argument("--browser-concurrency", type=int, default=1)
    combined.add_argument("--discovery-rate", type=float, default=1.0)
    combined.add_argument("--bulk-max-results", type=int, default=500, help="Skip one-shot bulk discovery above this result count")
    combined.set_defaults(bulk_discovery=True)
    combined.add_argument("--bulk-discovery", dest="bulk_discovery", action="store_true", help="Fetch all discovered advisors in one bulk response (default)")
    combined.add_argument("--no-bulk-discovery", dest="bulk_discovery", action="store_false", help="Use the API's standard 16-record pagination")
    combined.set_defaults(resume=True)
    combined.add_argument("--resume", dest="resume", action="store_true")
    combined.add_argument("--no-resume", dest="resume", action="store_false")
    combined.add_argument("--progress-every", type=int, default=100)
    add_cdp_args(combined)
    add_network_args(combined, default_rate=2.0)
    enrichment = subparsers.add_parser("enrich", help="Extract hidden email fields from profile pages")
    enrichment.add_argument("--input", type=Path, required=True, help="Advisor JSON array")
    enrichment.add_argument("--url-field", help="Dotted path of profile URL if auto-detection is wrong")
    enrichment.add_argument("--output", type=Path, default=roster_path("edward_jones"), help="Single complete advisor CSV with email fields added")
    enrichment.add_argument("--checkpoint", type=Path, default=DEFAULT_OUTPUT / ".edward_jones_email_checkpoint.jsonl", help=argparse.SUPPRESS)
    enrichment.set_defaults(resume=True)
    enrichment.add_argument("--resume", dest="resume", action="store_true")
    enrichment.add_argument("--no-resume", dest="resume", action="store_false")
    enrichment.add_argument("--progress-every", type=int, default=100)
    add_cdp_args(enrichment)
    add_network_args(enrichment, default_rate=2.0)
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    for name in ("concurrency", "rate", "timeout"):
        if getattr(args, name) <= 0:
            raise ValueError("--%s must be greater than zero" % name.replace("_", "-"))
    if args.command == "run":
        if args.browser_concurrency <= 0 or args.discovery_rate <= 0:
            raise ValueError("--browser-concurrency and --discovery-rate must be greater than zero")
    if args.retries < 0:
        raise ValueError("--retries cannot be negative")


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        validate_args(args)
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handler = logging.FileHandler(str(args.log_file), encoding="utf-8")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[log_handler],
        )
        logging.info("Starting command=%s", args.command)
        if args.command == "discover":
            asyncio.run(discover(args))
        elif args.command == "enrich":
            asyncio.run(enrich(args))
        else:
            asyncio.run(run_all(args))
        return 0
    except KeyboardInterrupt:
        print("Interrupted safely. Checkpoints were flushed; rerun the same command to resume.", file=sys.stderr)
        logging.warning("Interrupted by user")
        return 130
    except (ValueError, RuntimeError, httpx.HTTPError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

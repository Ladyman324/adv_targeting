import asyncio
import csv
import os
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention

import re
import io
import random
from urllib.parse import urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm_asyncio 

# Define output path: data/raw/firm_rosters/wells_fargo_advisors.csv
# Scraped firm rosters are INPUTS, not pipeline output: they live in
# data/raw/firm_rosters/ so a rebuild of data/output/ cannot overwrite them.
OUTPUT_DIR = os.path.join("data", "raw", "firm_rosters")
OUTPUT_FILE = str(roster_path("wells_fargo"))

FIELDNAMES = ["url", "name", "title", "emails", "phone_numbers", "address", "team_name", "team_url"]


def _contact_link(card, scheme):
    """Use a link from this person's card, never another team member."""
    link = card.select_one(f'.card--contact-info a[href^="{scheme}:"]')
    if not link:
        return ""
    value = link.get("href", "").split(":", 1)[-1].split("?", 1)[0].strip()
    if scheme == "tel":
        return value if len(re.sub(r"\D", "", value)) >= 10 else ""
    return value if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) else ""


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    address_elem = soup.select_one('a[href*="maps"], .address, [class*="address"]')
    if address_elem:
        address = " ".join(address_elem.get_text(separator=" ").split())
    else:
        match = re.search(
            r"\d+\s+[\w\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way)\.?,?\s+[\w\s]+,\s+[A-Z]{2}\s+\d{5}",
            soup.get_text(),
        )
        address = match.group(0) if match else ""

    # Team pages publish person-scoped cards. Never mix colleagues' links.
    cards = soup.select(".section--team .card--profile")
    if cards and not soup.select_one("div.page--title h1"):
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        parts = [part.strip() for part in page_title.split("|")]
        team_name = (parts[1] if len(parts) > 1 else parts[0]).split(",", 1)[0].strip()
        rows = []
        for card in cards:
            name_elem = card.select_one(".container--card-title h2.name .name-data")
            if not name_elem:
                continue
            name = name_elem.get_text(" ", strip=True).strip(" ,")
            if not name:
                continue
            title_elem = card.select_one(".container--card-title > p.title")
            rows.append({
                "url": url, "name": name,
                "title": title_elem.get_text(" ", strip=True) if title_elem else "",
                "emails": _contact_link(card, "mailto"),
                "phone_numbers": _contact_link(card, "tel"),
                "address": address, "team_name": team_name, "team_url": url,
            })
        if rows:
            return rows

    # Some pages use the h1 for a slogan, while an h2.name can identify a
    # teammate rather than the page owner. Prefer a document-title name only
    # when a published personal email corroborates its given/surname order.
    title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
    candidate = re.split(r"\s[-|]\s|,", title_text, maxsplit=1)[0].strip()
    parts = re.findall(r"[a-z]+", candidate.lower())
    mailto = [
        link.get("href", "").split(":", 1)[-1].split("@", 1)[0].lower()
        for link in soup.select('a[href^="mailto:"]')
    ]
    title_matches_mail = (
        2 <= len(parts) <= 4
        and not {"group", "advisors", "wealth", "management"} & set(parts)
        and any(re.search(rf"\b{re.escape(parts[0])}\b.*\b{re.escape(parts[-1])}\b",
                          re.sub(r"[^a-z]+", " ", local))
                for local in mailto)
    )
    person_headings = [
        heading for heading in soup.select("h2.name")
        if not heading.find_parent(class_="card--profile")
    ]
    # Several person headings mean this is another team layout. Taking the
    # first while collecting page-wide phones would misattribute a teammate.
    name_elem = person_headings[0] if len(person_headings) == 1 else None
    heading = soup.select_one("div.page--title h1") or soup.find("h1")
    name = (candidate if title_matches_mail else
            name_elem.get_text(" ", strip=True) if name_elem else
            heading.get_text(" ", strip=True) if heading else "")
    title_elem = soup.select_one("div.page--title p.title")
    emails = sorted({link["href"].replace("mailto:", "").split("?")[0].strip()
                     for link in soup.select('a[href^="mailto:"]')})
    phones = sorted({link["href"].replace("tel:", "").split("?")[0].strip()
                     for link in soup.select('a[href^="tel:"]')
                     if "fax" not in (link.parent.get_text(" ", strip=True) if link.parent else "").lower()})
    return [{
        "url": url,
        "name": name,
        "title": title_elem.get_text(" ", strip=True) if title_elem else "",
        "emails": "; ".join(emails), "phone_numbers": "; ".join(phones),
        "address": address, "team_name": "", "team_url": "",
    }]


async def parse_advisor_page(session, url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            if response.status != 200:
                # We will log the error but let the retry logic handle it
                tqdm_asyncio.write(f"HTTP {response.status} Error scraping {url}")
                return None
            
            html = await response.text()

        return parse_page(html, url)
    except Exception as e:
        tqdm_asyncio.write(f"Exception scraping {url}: {e}")
        return None


async def process_sitemap(sitemap_url, max_concurrent=25, max_retries=5):
    # Ensure directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    partial_file = OUTPUT_FILE + ".partial"

    async with aiohttp.ClientSession() as session:
        # Fetch sitemap and extract all <loc> URLs
        print("Fetching sitemap...")
        async with session.get(sitemap_url, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as response:
            response.raise_for_status()
            sitemap_xml = await response.text()

        soup = BeautifulSoup(sitemap_xml, "xml")
        urls_to_scrape = list(dict.fromkeys(
            loc.get_text(strip=True) for loc in soup.find_all("loc")
            if loc.text and urlparse(loc.get_text(strip=True)).hostname == "fa.wellsfargoadvisors.com"
        ))
        if len(urls_to_scrape) < 1000:
            raise ValueError(f"Wells Fargo sitemap is unexpectedly small: {len(urls_to_scrape)} URLs")
        
        total_urls = len(urls_to_scrape)
        print(f"Found {total_urls} URLs to scrape.")

        # Prepare CSV File and write header once
        rows_written = 0
        team_rows = 0
        async with aiofiles.open(partial_file, mode="w", newline="", encoding="utf-8") as f:
            await f.write(",".join(FIELDNAMES) + "\n")

            # Loop through initial attempt + retries
            for attempt in range(max_retries + 1):
                if not urls_to_scrape:
                    print("\nAll URLs scraped successfully!")
                    break

                if attempt > 0:
                    print(f"\n[Attempt {attempt + 1}/{max_retries + 1}] Retrying {len(urls_to_scrape)} failed URLs.")
                    print("Waiting 10 seconds before restarting...")
                    await asyncio.sleep(10)
                else:
                    print(f"\n[Attempt 1/{max_retries + 1}] Starting initial scrape...")

                semaphore = asyncio.Semaphore(max_concurrent)

                async def worker(url):
                    nonlocal rows_written, team_rows
                    async with semaphore:
                        await asyncio.sleep(random.uniform(0.1, 0.7))
                        data = await parse_advisor_page(session, url)
                        
                        if data is not None:
                            output = io.StringIO()
                            writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
                            writer.writerows(data)
                            await f.write(output.getvalue())
                            rows_written += len(data)
                            team_rows += sum(bool(row["team_url"]) for row in data)
                            return None
                        else:
                            # Failure: return the URL so we can try it again
                            return url

                # Run current batch of tasks
                tasks = [worker(url) for url in urls_to_scrape]
                results = await tqdm_asyncio.gather(*tasks, desc=f"Scraping")
                
                # Filter results to only keep the URLs that failed (returned themselves instead of None)
                urls_to_scrape = [url for url in results if url is not None]

    # A tiny number of sitemap entries can become permanent redirect loops.
    # Record them for review, but do not discard an otherwise complete census.
    max_failed = max(1, int(total_urls * 0.001))
    if len(urls_to_scrape) > max_failed or rows_written < total_urls * 0.8:
        raise RuntimeError(
            f"Wells Fargo scrape incomplete: {len(urls_to_scrape)} failed URLs, "
            f"{rows_written} rows from {total_urls} sitemap URLs. "
            f"Partial file retained at {partial_file}; prior roster remains current."
        )
    failed_file = scratch_path("wells_fargo", "failed", ext="csv")
    with failed_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url"])
        writer.writerows((url,) for url in urls_to_scrape)
    if urls_to_scrape:
        print(f"Warning: {len(urls_to_scrape)} unresolved URLs recorded in {failed_file}")
    os.replace(partial_file, OUTPUT_FILE)
    print(f"Results saved to {OUTPUT_FILE}: {rows_written} rows, {team_rows} team members")


if __name__ == "__main__":
    sitemap_url = "https://fa.wellsfargoadvisors.com/sitemap.xml"
    # Modest parallelism across the public sitemap; failed pages are retried.
    asyncio.run(process_sitemap(sitemap_url, max_concurrent=20, max_retries=5))

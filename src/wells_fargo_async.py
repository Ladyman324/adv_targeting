import asyncio
import csv
import os
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path  # one naming convention, defined once

import re
import io
import random

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm_asyncio 

# Define output path: data/raw/firm_rosters/wells_fargo_advisors.csv
# Scraped firm rosters are INPUTS, not pipeline output: they live in
# data/raw/firm_rosters/ so a rebuild of data/output/ cannot overwrite them.
OUTPUT_DIR = os.path.join("data", "raw", "firm_rosters")
OUTPUT_FILE = str(roster_path("wells_fargo"))


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

        soup = BeautifulSoup(html, "html.parser")

        # 1. Name & Title
        name_elem = soup.select_one("div.page--title h1")
        if not name_elem:
            name_elem = soup.find("h1")
        name = name_elem.get_text(strip=True) if name_elem else None

        title_elem = soup.select_one("div.page--title p.title")
        title = title_elem.get_text(strip=True) if title_elem else None

        # 2. Email Addresses
        email_links = soup.select('a[href^="mailto:"]')
        emails = list(
            set(
                link["href"].replace("mailto:", "").split("?")[0].strip()
                for link in email_links
            )
        )

        # 3. Phone Numbers (Excluding Fax)
        phone_links = soup.select('a[href^="tel:"]')
        phones = []
        for link in phone_links:
            num = link.get_text(strip=True)
            parent_text = link.parent.get_text() if link.parent else ""
            if "fax" not in parent_text.lower():
                phones.append(num)
        phones = list(set(phones))

        # 4. Address Extraction
        address_elem = soup.select_one(
            'a[href*="maps"], .address, [class*="address"]'
        )
        if address_elem:
            address = " ".join(address_elem.get_text(separator=" ").split())
        else:
            address_match = re.search(
                r"\d+\s+[\w\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way)\.?,?\s+[\w\s]+,\s+[A-Z]{2}\s+\d{5}",
                soup.get_text(),
            )
            address = address_match.group(0) if address_match else None

        return {
            "url": url,
            "name": name,
            "title": title,
            "emails": "; ".join(emails) if emails else None,
            "phone_numbers": "; ".join(phones) if phones else None,
            "address": address,
        }
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

    fieldnames = ["url", "name", "title", "emails", "phone_numbers", "address"]

    async with aiohttp.ClientSession() as session:
        # Fetch sitemap and extract all <loc> URLs
        print("Fetching sitemap...")
        async with session.get(sitemap_url, headers=headers) as response:
            sitemap_xml = await response.text()

        soup = BeautifulSoup(sitemap_xml, "xml")
        urls_to_scrape = [
            loc.get_text(strip=True) for loc in soup.find_all("loc") if loc.text
        ]
        
        total_urls = len(urls_to_scrape)
        print(f"Found {total_urls} URLs to scrape.")

        # Prepare CSV File and write header once
        async with aiofiles.open(
            OUTPUT_FILE, mode="w", newline="", encoding="utf-8"
        ) as f:
            await f.write(",".join(fieldnames) + "\n")

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
                    async with semaphore:
                        await asyncio.sleep(random.uniform(0.1, 0.7))
                        data = await parse_advisor_page(session, url)
                        
                        if data:
                            # Success: write to CSV and return None (meaning no error)
                            output = io.StringIO()
                            writer = csv.DictWriter(output, fieldnames=fieldnames)
                            writer.writerow(data)
                            await f.write(output.getvalue())
                            return None
                        else:
                            # Failure: return the URL so we can try it again
                            return url

                # Run current batch of tasks
                tasks = [worker(url) for url in urls_to_scrape]
                results = await tqdm_asyncio.gather(*tasks, desc=f"Scraping")
                
                # Filter results to only keep the URLs that failed (returned themselves instead of None)
                urls_to_scrape = [url for url in results if url is not None]

    if urls_to_scrape:
        print(f"\nScraping finished. {len(urls_to_scrape)} URLs failed completely after {max_retries} retries.")
    
    print(f"Results saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    sitemap_url = "https://fa.wellsfargoadvisors.com/sitemap.xml"
    # Keep concurrency at 10. The script will automatically retry up to 5 times.
    asyncio.run(process_sitemap(sitemap_url, max_concurrent=10, max_retries=5))
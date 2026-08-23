import asyncio
import json
import os
import re
import time
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path  # one naming convention, defined once

import urllib.parse
import urllib.request
import uuid
import httpx
import pandas as pd
from playwright.sync_api import sync_playwright

# --- Configuration & Constants ---
CDP_HOST = "http://127.0.0.1:9222"
TARGET_DOMAIN = "https://advisor.morganstanley.com/"
BASE_URL = "https://prod-cdn.us.yextapis.com/v2/accounts/me/search/vertical/query"
DEFAULT_API_KEY = "a0c911dfe81f6f0026255868407b6713"

# Regional grid points to bypass the 10,000 deep-offset API limit
REGION_POINTS = [
    {"name": "Southeast", "lat": 33.75, "lng": -84.38, "radius": 1500000},
    {"name": "Northeast", "lat": 40.71, "lng": -74.00, "radius": 1500000},
    {"name": "Midwest", "lat": 41.87, "lng": -87.62, "radius": 1500000},
    {"name": "South / Texas", "lat": 31.96, "lng": -99.90, "radius": 1500000},
    {"name": "West Coast / North", "lat": 47.60, "lng": -122.33, "radius": 1500000},
    {"name": "West Coast / South", "lat": 34.05, "lng": -118.24, "radius": 1500000},
]


# =====================================================================
# Phase 1: Native Chrome HTTP / CDP Utilities (Playbook #3, #4, #8)
# =====================================================================

def get_open_tabs():
    """Polls Chrome's native HTTP endpoint without CDP instrumentation."""
    try:
        with urllib.request.urlopen(f"{CDP_HOST}/json") as res:
            return json.loads(res.read().decode())
    except Exception:
        return []


def close_tab_by_id(tab_id):
    """Playbook #8: Close stale tabs over plain HTTP before starting."""
    try:
        urllib.request.urlopen(f"{CDP_HOST}/json/close/{tab_id}")
    except Exception:
        pass


def open_native_tab(url):
    """Opens a plain native tab via PUT request without attaching any debugger."""
    endpoint = f"{CDP_HOST}/json/new?{urllib.parse.quote(url)}"
    req = urllib.request.Request(endpoint, method="PUT")
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())


def get_chrome_user_agent():
    """Extracts the exact native User-Agent from Chrome binary over HTTP."""
    try:
        with urllib.request.urlopen(f"{CDP_HOST}/json/version") as res:
            data = json.loads(res.read().decode())
            return data.get("User-Agent")
    except Exception:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def extract_live_api_key():
    """Dynamically extracts the Yext API key from target page if static key fails."""
    try:
        req = urllib.request.Request(
            TARGET_DOMAIN,
            headers={"User-Agent": get_chrome_user_agent()}
        )
        with urllib.request.urlopen(req) as res:
            html = res.read().decode("utf-8")
            match = re.search(r'api_key[=\":]\s*[\"\']?([a-f0-9]{32})[\"\'&]?', html)
            if match:
                extracted_key = match.group(1)
                print(f"🔑 Dynamically extracted active API Key: {extracted_key}")
                return extracted_key
    except Exception as e:
        print(f"⚠️ Dynamic API Key extraction fallback failed: {e}")
    
    return DEFAULT_API_KEY


# =====================================================================
# Phase 2: Native Clearance & Token Inheritance (Playbook #4, #5, #6)
# =====================================================================

def obtain_native_session():
    """Solves CF in a native, un-instrumented tab and extracts dynamic tokens."""
    # 1. Cleanup old target tabs (Playbook #8)
    for tab in get_open_tabs():
        if "morganstanley" in tab.get("url", ""):
            close_tab_by_id(tab["id"])

    print("🚀 [Step 1] Opening native Chrome tab for Cloudflare & token clearance...")
    new_tab = open_native_tab(TARGET_DOMAIN)
    tab_id = new_tab["id"]

    # 2. Passive observation loop (Playbook #4)
    start = time.time()
    while time.time() - start < 30:
        tabs = get_open_tabs()
        target_tab = next((t for t in tabs if t.get("id") == tab_id), None)

        if target_tab:
            title = target_tab.get("title", "")

            # Playbook #6: Fail fast on hard WAF IP block
            if any(b in title for b in ["Attention Required!", "Access Denied", "403 Forbidden"]):
                raise PermissionError("🚨 Hard Cloudflare WAF Block! Back off and wait for IP reputation to decay.")

            # Cleared
            if "Just a moment" not in title and title != "":
                print(f"✅ [Step 1] Cleared natively! Title: '{title}'")
                break

        time.sleep(1)

    # 3. Reconnect Playwright briefly ONLY to grab refreshed cookies & tokens (Playbook #5 & #10)
    print("🔑 [Step 2] Extracting dynamic cookies & session tokens...")
    cookies_dict = {}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_HOST)
        context = browser.contexts[0]
        cookies = context.cookies(TARGET_DOMAIN)
        cookies_dict = {c["name"]: c["value"] for c in cookies}
        browser.close()

    user_agent = get_chrome_user_agent()
    return user_agent, cookies_dict


# =====================================================================
# Phase 3: High-Speed Async Extraction (Playbook #7, #9, #11)
# =====================================================================

async def fetch_chunk(client, semaphore, region, offset, api_key, session_id, limit=50):
    """Fetches a single regional chunk concurrently over HTTP."""
    async with semaphore:
        filters = {
            "builtin.location": {
                "$near": {
                    "lat": region["lat"],
                    "lng": region["lng"],
                    "radius": region["radius"]
                }
            }
        }

        params = {
            "experienceKey": "ms-search-locator",
            "api_key": api_key,
            "v": "20220511",
            "version": "PRODUCTION",
            "locale": "en",
            "verticalKey": "locations",
            "filters": json.dumps(filters),
            "limit": str(limit),
            "offset": str(offset),
            "retrieveFacets": "false",
            "skipSpellCheck": "false",
            "session_id": session_id,
            "sessionTrackingEnabled": "true",
            "source": "STANDARD"
        }

        try:
            response = await client.get(BASE_URL, params=params)
            if response.status_code == 200:
                return response.json().get("response", {})
            elif response.status_code in [401, 403]:
                print(f"⚠️ Token/Key Invalidation ({response.status_code}) at offset {offset}. Triggering refresh...")
                return "REFRESH_NEEDED"
            elif response.status_code == 429:
                print("⚠️ Rate limit encountered. Backing off briefly...")
                await asyncio.sleep(2)
                return await fetch_chunk(client, semaphore, region, offset, api_key, session_id, limit)
        except Exception as e:
            print(f"❌ Network error at offset {offset}: {e}")
            return None


async def run_async_extraction(user_agent, cookies_dict, api_key):
    """Orchestrates concurrent regional sweeps with automatic token refresh."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": TARGET_DOMAIN,
        "client-sdk": "ANSWERS_CORE=2.5.4, ANSWERS_HEADLESS=2.5.2",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site"
    }

    # Playbook #7: Limit concurrency to 10 connections to avoid traffic flags
    semaphore = asyncio.Semaphore(10)
    advisor_map = {}
    session_id = f"01{uuid.uuid4().hex[:24].upper()}"

    async with httpx.AsyncClient(headers=headers, cookies=cookies_dict, timeout=15.0) as client:
        print("\n⚡ [Step 3] Dispatching async regional fetches...")

        for region in REGION_POINTS:
            initial_data = await fetch_chunk(client, semaphore, region, offset=0, api_key=api_key, session_id=session_id)

            # Handle dynamic token or API key refresh
            if initial_data == "REFRESH_NEEDED":
                print("🔄 Refetching native session tokens and dynamic API key...")
                user_agent, cookies_dict = obtain_native_session()
                api_key = extract_live_api_key()
                return await run_async_extraction(user_agent, cookies_dict, api_key)

            if not initial_data:
                continue

            total_records = initial_data.get("resultsCount", 0)
            print(f"📍 Region '{region['name']}': {total_records} records. Parallelizing...")

            tasks = [
                fetch_chunk(client, semaphore, region, offset, api_key, session_id)
                for offset in range(0, total_records, 50)
            ]

            results_list = await asyncio.gather(*tasks)

            for res in results_list:
                if not res or res == "REFRESH_NEEDED":
                    continue
                for item in res.get("results", []):
                    d = item.get("data", {})
                    if d.get("c_profileType") == "FA":
                        uid = d.get("uid") or d.get("c_faNumber") or d.get("id")
                        if uid and uid not in advisor_map:
                            addr = d.get("address", {})
                            advisor_map[uid] = {
                                "Name": d.get("c_pagesName") or d.get("name", ""),
                                "Primary Title": d.get("c_primaryTitle", ""),
                                "Secondary Titles": " | ".join(d.get("c_secondaryTitles", [])),
                                "Team Name": d.get("c_teamEntityName", ""),
                                "Branch Name": d.get("c_branchName", ""),
                                "Office Number": d.get("c_officeNumber", ""),
                                "FA Number": d.get("c_faNumber", ""),
                                "Complex ID": d.get("c_complexID", ""),
                                "Main Phone": d.get("mainPhone", ""),
                                "Branch Phone": d.get("c_branchPhone", ""),
                                "Email": (d.get("emails", [""])[0] if d.get("emails") else ""),
                                "Certifications": " | ".join(d.get("c_listOfCertifications", [])),
                                "Street Line 1": addr.get("line1", ""),
                                "Street Line 2": addr.get("line2", ""),
                                "City": addr.get("city", ""),
                                "State": addr.get("region", ""),
                                "Postal Code": addr.get("postalCode", ""),
                                "Profile URL": d.get("c_pagesURL") or d.get("c_locatorURL", "")
                            }

            print(f"   ✓ Captured {len(advisor_map)} unique Financial Advisors so far...")

    return list(advisor_map.values())


# =====================================================================
# Main Orchestrator
# =====================================================================

def main():
    # 1. Obtain cleared session state and dynamic tokens
    user_agent, cookies_dict = obtain_native_session()
    api_key = DEFAULT_API_KEY

    # 2. Run High-Speed Async Extraction
    start_time = time.time()
    advisors = asyncio.run(run_async_extraction(user_agent, cookies_dict, api_key))

    # 3. Export CSV to "../data/raw/firm_rosters" -- an input, not pipeline output
    if advisors:
        df = pd.DataFrame(advisors)
        
        # Path resolution: locate this script folder and resolve ../data/raw/firm_rosters
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.abspath(os.path.join(script_dir, "..", "data", "raw", "firm_rosters"))
        os.makedirs(output_dir, exist_ok=True)
        
        filename = roster_path("morgan_stanley").name
        output_path = os.path.join(output_dir, filename)
        
        df.to_csv(output_path, index=False)
        elapsed = round(time.time() - start_time, 2)
        print(f"\n🎉 SUCCESS! Saved {len(df)} unique Financial Advisors to '{output_path}' in {elapsed} seconds.")


if __name__ == "__main__":
    main()
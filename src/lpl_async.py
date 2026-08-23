import os
import time
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path  # one naming convention, defined once

import pandas as pd
from playwright.sync_api import sync_playwright

CDP_HOST = "http://127.0.0.1:9222"
TARGET_URL = "https://www.lpl.com/investors/find-an-advisor.html"

US_STATES = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"), ("California", "CA"),
    ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"), ("Florida", "FL"), ("Georgia", "GA"),
    ("Hawaii", "HI"), ("Idaho", "ID"), ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"),
    ("Kansas", "KS"), ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
    ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"), ("Mississippi", "MS"), ("Missouri", "MO"),
    ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"), ("New Hampshire", "NH"), ("New Jersey", "NJ"),
    ("New Mexico", "NM"), ("New York", "NY"), ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"),
    ("Oklahoma", "OK"), ("Oregon", "OR"), ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"),
    ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"), ("Vermont", "VT"),
    ("Virginia", "VA"), ("Washington", "WA"), ("West Virginia", "WV"), ("Wisconsin", "WI"), ("Wyoming", "WY")
]


def extract_all_lpl_advisors():
    advisor_map = {}

    with sync_playwright() as p:
        print("🚀 Connecting to Chrome CDP and opening LPL Find an Advisor...")
        browser = p.chromium.connect_over_cdp(CDP_HOST)
        context = browser.contexts[0]
        page = context.new_page()

        # Intercept background API responses
        def handle_response(response):
            if "findAnAdvisorApi.json" in response.url and response.status == 200:
                try:
                    data = response.json()
                    advisors = data.get("advisors", [])
                    if advisors:
                        print(f"   ⚡ Intercepted API payload with {len(advisors)} advisors!")
                        for adv in advisors:
                            email = adv.get("email_address") or ""
                            disp_name = adv.get("display_name") or f"{adv.get('first_name', '')} {adv.get('last_name', '')}"
                            address = adv.get("address") or ""
                            key = email.lower() if email else f"{disp_name}_{address}".lower()

                            if key and key not in advisor_map:
                                advisor_map[key] = {
                                    "Display Name": disp_name,
                                    "First Name": adv.get("first_name", ""),
                                    "Middle Name": adv.get("middle_name", ""),
                                    "Last Name": adv.get("last_name", ""),
                                    "Email": email,
                                    "Phone": adv.get("phone", ""),
                                    "Address": address,
                                    "City": adv.get("city", ""),
                                    "State": adv.get("state", ""),
                                    "Zipcode": adv.get("zipcode", ""),
                                    "Latitude": adv.get("latitude", ""),
                                    "Longitude": adv.get("longitude", ""),
                                    "Website URL": adv.get("web_site_url", ""),
                                    "Quote": adv.get("quote", "")
                                }
                except Exception:
                    pass

        page.on("response", handle_response)

        # Fix: Use 'domcontentloaded' instead of 'networkidle' to avoid timeouts on streaming analytics
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"⚠️ Page load notice (proceeding anyway): {e}")

        time.sleep(3)

        # Target Google Places input box directly
        search_input = page.locator("input#autocomplete")

        for state_name, state_code in US_STATES:
            print(f"📍 Triggering State: {state_name} ({state_code})...")
            try:
                if search_input.is_visible():
                    search_input.click()
                    search_input.fill("")
                    
                    # Type full state name to trigger Google Places Autocomplete dropdown
                    search_input.type(state_name, delay=80)
                    time.sleep(1.2)

                    # Click Google Places dropdown suggestion if visible, or fallback to key presses
                    pac_item = page.locator(".pac-container .pac-item").first
                    if pac_item.is_visible():
                        pac_item.click()
                    else:
                        page.keyboard.press("ArrowDown")
                        time.sleep(0.2)
                        page.keyboard.press("Enter")

                    # Allow time for Turnstile/Captcha clearance & response stream
                    time.sleep(3.5)

            except Exception as e:
                print(f"   ⚠️ Interaction error for {state_name}: {e}")

            print(f"   ✓ Unique LPL Advisors captured so far: {len(advisor_map)}")

        page.close()
        browser.close()

    return list(advisor_map.values())


def main():
    start_time = time.time()
    advisors = extract_all_lpl_advisors()

    if advisors:
        df = pd.DataFrame(advisors)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.abspath(os.path.join(script_dir, "..", "data", "raw", "firm_rosters"))
        os.makedirs(output_dir, exist_ok=True)

        filename = roster_path("lpl").name
        output_path = os.path.join(output_dir, filename)

        df.to_csv(output_path, index=False)
        elapsed = round(time.time() - start_time, 2)
        print(f"\n🎉 SUCCESS! Saved {len(df)} unique LPL Advisors to '{output_path}' in {elapsed} seconds.")


if __name__ == "__main__":
    main()
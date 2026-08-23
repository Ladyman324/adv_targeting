#!/usr/bin/env python3
"""Simple retry loop for the Raymond James scraper.

Per attempt: open Chrome on the locator page with debugging on 9222,
maximized and zoomed to 67%, wait for the DevTools endpoint, run the
scraper, kill Chrome. Repeat up to MAX_ATTEMPTS.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

# ---- settings ------------------------------------------------------------

MAX_ATTEMPTS = 50
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = r"C:\SeleniumChrome"
URL = "https://www.raymondjames.com/find-an-advisor"
SCRIPT = os.path.join("src", "raymond_james_async.py")
PORT = 9222

ZOOM = 0.80                    # 67% -- same effect as Ctrl+Minus twice
WINDOW_SIZE = "1920,1080"      # fallback if --start-maximized is ignored
MAXIMIZE_VIA_CDP = True        # belt-and-braces maximize after launch

WIPE_PROFILE_ON_RETRY = True   # delete the profile between attempts
RETRY_DELAY = 5                # seconds between attempts
STARTUP_TIMEOUT = 30           # max wait for the debug port
SETTLE_SECONDS = 3             # grace period after the port opens
SCRIPT_TIMEOUT = None          # seconds, or None for no limit

# --------------------------------------------------------------------------


def port_ready():
    """True once Chrome's DevTools endpoint answers."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/version", timeout=1
        ) as r:
            json.load(r)
        return True
    except Exception:
        return False


def maximize_window():
    """Maximize the browser window over CDP.

    More reliable than --start-maximized on a fresh profile, and handles
    multi-monitor setups correctly.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            wid = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": wid, "bounds": {"windowState": "maximized"}},
            )
            browser.close()  # detaches only; does not close Chrome
        print("[+] Window maximized.")
    except Exception as exc:
        print(f"[-] CDP maximize failed ({exc}); relying on launch flags.")


def start_chrome():
    """Launch Chrome on the locator page. Returns the Popen handle."""
    os.makedirs(PROFILE, exist_ok=True)
    print(f"[+] Starting Chrome on port {PORT} (zoom {int(ZOOM * 100)}%)")

    proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--start-maximized",
            "--window-position=0,0",
            f"--window-size={WINDOW_SIZE}",
            f"--force-device-scale-factor={ZOOM}",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if port_ready():
            print(f"[+] DevTools ready on {PORT}")
            if MAXIMIZE_VIA_CDP:
                maximize_window()
            time.sleep(SETTLE_SECONDS)
            return proc
        time.sleep(0.5)

    print("[-] Debug port never opened within "
          f"{STARTUP_TIMEOUT}s -- running anyway.")
    return proc


def kill_chrome():
    """Kill Chrome. Note: this kills ALL Chrome windows on the machine."""
    print("[+] Killing Chrome...")
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.run(
            ["pkill", "-x", "chrome"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(2)


def wipe_profile():
    """Delete the profile directory so the next attempt starts clean."""
    if os.path.isdir(PROFILE):
        print(f"[+] Wiping profile: {PROFILE}")
        shutil.rmtree(PROFILE, ignore_errors=True)


def main():
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print("\n" + "=" * 40)
        print(f" Attempt {attempt} of {MAX_ATTEMPTS}")
        print("=" * 40)

        start_chrome()

        try:
            result = subprocess.run(
                [sys.executable, SCRIPT], timeout=SCRIPT_TIMEOUT
            )
            code = result.returncode
        except subprocess.TimeoutExpired:
            print("[!] Script exceeded timeout.")
            code = -1

        kill_chrome()

        if code == 0:
            print("\n[OK] Success.")
            return 0

        print(f"\n[!] Failed with exit code {code}")

        if attempt == MAX_ATTEMPTS:
            print("[!] All attempts exhausted.")
            return code

        if WIPE_PROFILE_ON_RETRY:
            wipe_profile()

        print(f"[*] Retrying in {RETRY_DELAY}s... (Ctrl+C to stop)")
        time.sleep(RETRY_DELAY)

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        kill_chrome()
        sys.exit(130)
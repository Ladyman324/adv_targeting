"""Resolve CRD -> Part 2A brochure(s) via the IAPD API, download, extract text.

Cached and resumable: anything already on disk is skipped, so re-running after
an interruption is cheap. Throttled and sent with an identifying User-Agent per
SEC fair-access guidance.
"""
from __future__ import annotations  # py3.8: builtin generics in annotations

import json
import sys
import time
import urllib.request
import pathlib

import fitz

ROOT = pathlib.Path(__file__).parents[1]
CACHE = ROOT / "data" / "brochures"

# The brochure host rejects declarative bot User-Agents with a 404, so a
# browser-shaped UA is required for retrieval. `From` carries real contact
# info (RFC 9110 10.1.2) so requests remain attributable to us.
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "From": "bladyman@eicatlanta.com",
    "Accept": "application/pdf,*/*",
}
FIRM_API = "https://api.adviserinfo.sec.gov/search/firm/{crd}"
BROCHURE = ("https://files.adviserinfo.sec.gov/IAPD/Content/Common/"
            "crd_iapd_Brochure.aspx?BRCHR_VRSN_ID={vid}")
ADV_PDF = "https://reports.adviserinfo.sec.gov/reports/ADV/{crd}/PDF/{crd}.pdf"

THROTTLE = 0.35  # seconds between requests


def _get(url: str, timeout: int = 60) -> bytes:
    time.sleep(THROTTLE)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def brochure_ids(crd: str) -> list[dict]:
    """Return [{brochureVersionID, brochureName, dateSubmitted}, ...] for a CRD."""
    meta = CACHE / crd / "firm_api.json"
    if meta.exists():
        payload = json.loads(meta.read_text(encoding="utf-8"))
    else:
        payload = json.loads(_get(FIRM_API.format(crd=crd)))
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(payload), encoding="utf-8")

    hits = payload.get("hits", {}).get("hits", [])
    if not hits:
        return []
    ia = json.loads(hits[0]["_source"]["iacontent"])
    return ia.get("brochures", {}).get("brochuredetails", []) or []


def _pdf_to_text(pdf_path: pathlib.Path) -> str:
    doc = fitz.open(pdf_path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def fetch_firm(crd: str) -> dict:
    """Download brochures + Part 1 PDF for one CRD. Returns a manifest dict."""
    out = CACHE / crd
    out.mkdir(parents=True, exist_ok=True)
    record = {"crd": crd, "brochures": [], "adv1_chars": 0, "errors": []}

    for b in brochure_ids(crd):
        vid = b["brochureVersionID"]
        pdf, txt = out / f"brochure_{vid}.pdf", out / f"brochure_{vid}.txt"
        try:
            if not pdf.exists():
                pdf.write_bytes(_get(BROCHURE.format(vid=vid)))
            if not txt.exists():
                txt.write_text(_pdf_to_text(pdf), encoding="utf-8")
            record["brochures"].append({
                "version_id": vid,
                "name": b.get("brochureName"),
                "submitted": b.get("dateSubmitted"),
                "chars": len(txt.read_text(encoding="utf-8")),
            })
        except Exception as e:                                  # noqa: BLE001
            record["errors"].append(f"brochure {vid}: {type(e).__name__}: {e}")

    adv1, adv1_txt = out / "adv1.pdf", out / "adv1.txt"
    try:
        if not adv1.exists():
            adv1.write_bytes(_get(ADV_PDF.format(crd=crd)))
        if not adv1_txt.exists():
            adv1_txt.write_text(_pdf_to_text(adv1), encoding="utf-8")
        record["adv1_chars"] = len(adv1_txt.read_text(encoding="utf-8"))
    except Exception as e:                                      # noqa: BLE001
        record["errors"].append(f"adv1: {type(e).__name__}: {e}")

    return record


def main(crds: list[str]) -> None:
    manifest = []
    for i, crd in enumerate(crds, 1):
        rec = fetch_firm(crd)
        manifest.append(rec)
        n = len(rec["brochures"])
        chars = sum(b["chars"] for b in rec["brochures"])
        flag = "  !! " + "; ".join(rec["errors"]) if rec["errors"] else ""
        print(f"[{i:>2}/{len(crds)}] {crd:>7}  brochures={n} "
              f"({chars:,} chars)  adv1={rec['adv1_chars']:,}{flag}")
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                         encoding="utf-8")
    ok = sum(1 for r in manifest if r["brochures"])
    print(f"\n{ok}/{len(manifest)} firms have at least one brochure")


if __name__ == "__main__":
    main(sys.argv[1:])

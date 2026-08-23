"""Download the map libraries into webapp/vendor/ and record their hashes.

WHY THIS EXISTS
---------------
index.html loaded Leaflet, MarkerCluster and Leaflet.heat from unpkg.com on
every page view. Three problems with that, in increasing order of seriousness:

  * the app is signed-in and internal, but it announced itself to a third-party
    CDN every time a rep opened it;
  * a version served from a CDN is whatever the CDN serves. `@1.9.4` is a tag,
    and a compromised or re-pointed tag executes with full access to the page --
    which here means the contact file and the call log;
  * a rep in the field with a poor connection watched the map fail on a
    dependency that has nothing to do with our data.

So the libraries are vendored: downloaded once, hashed, committed, and served
from the same origin as everything else. That also lets index.html carry a
Content-Security-Policy that names no external script host at all.

Run:  python src/vendor_assets.py [--check]

--check re-hashes what is on disk against the manifest and exits non-zero on a
mismatch. It does NOT reach the network, so it is safe in an audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).parents[1]
VENDOR = ROOT / "webapp" / "vendor"
MANIFEST = VENDOR / "manifest.json"

# Pinned by exact version. Bumping one means editing this list, re-running, and
# committing a changed hash -- which is the point: the upgrade is a reviewable
# diff rather than something that happens to the app overnight.
BASE = "https://unpkg.com"
FILES = [
    ("leaflet@1.9.4/dist/leaflet.css",                                "leaflet.css"),
    ("leaflet@1.9.4/dist/leaflet.js",                                 "leaflet.js"),
    ("leaflet@1.9.4/dist/images/marker-icon.png",                     "images/marker-icon.png"),
    ("leaflet@1.9.4/dist/images/marker-icon-2x.png",                  "images/marker-icon-2x.png"),
    ("leaflet@1.9.4/dist/images/marker-shadow.png",                   "images/marker-shadow.png"),
    ("leaflet@1.9.4/dist/images/layers.png",                          "images/layers.png"),
    ("leaflet@1.9.4/dist/images/layers-2x.png",                       "images/layers-2x.png"),
    ("leaflet.markercluster@1.5.3/dist/MarkerCluster.css",            "MarkerCluster.css"),
    ("leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css",    "MarkerCluster.Default.css"),
    ("leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js",     "leaflet.markercluster.js"),
    ("leaflet.heat@0.2.0/dist/leaflet-heat.js",                       "leaflet-heat.js"),
]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fetch() -> dict:
    VENDOR.mkdir(parents=True, exist_ok=True)
    (VENDOR / "images").mkdir(exist_ok=True)
    man = {}
    for remote, local in FILES:
        url = f"{BASE}/{remote}"
        with urllib.request.urlopen(url, timeout=60) as r:
            body = r.read()
        path = VENDOR / local
        path.write_bytes(body)
        man[local] = {"url": url, "sha256": sha256(body), "bytes": len(body)}
        print(f"[*] {local:<32} {len(body):>9,} bytes  {man[local]['sha256'][:16]}")
    MANIFEST.write_text(json.dumps(man, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    return man


def check() -> int:
    if not MANIFEST.exists():
        print("[!] no vendor manifest -- run without --check to download")
        return 1
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    for local, meta in sorted(man.items()):
        path = VENDOR / local
        if not path.exists():
            print(f"[!] MISSING {local}")
            bad += 1
            continue
        got = sha256(path.read_bytes())
        if got != meta["sha256"]:
            print(f"[!] CHANGED {local}\n    manifest {meta['sha256']}\n    on disk  {got}")
            bad += 1
    print(f"[*] {len(man) - bad} of {len(man)} vendored files match the manifest"
          if bad else f"[*] all {len(man)} vendored files match the manifest")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="verify on-disk hashes against the manifest; no network")
    args = ap.parse_args()
    sys.exit(check() if args.check else (fetch() and 0))


if __name__ == "__main__":
    main()

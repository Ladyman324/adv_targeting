"""Run the tiered geocoder across every state, then export the web-map data.

Designed to be left running unattended:
  * resumable  -- a state whose parquet already exists is skipped, so you can
                  stop the job (Ctrl-C, reboot, Census outage) and re-run it
  * isolated   -- one state failing does not abort the rest; failures are
                  listed in the final summary and can be retried on a re-run
  * polite     -- a pause between states, on top of the per-chunk retry and
                  backoff already in geocode.py

Usage (PowerShell):
    python src\\run_national.py                    # all states, resume
    python src\\run_national.py -States GA,FL,TX   # just these
    python src\\run_national.py -Force             # re-geocode even if done
    python src\\run_national.py -NoExport          # skip the GeoJSON step
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import traceback

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

import geocode          # noqa: E402
import export_geojson   # noqa: E402

INTERIM = ROOT / "data" / "interim"
OUTPUT = ROOT / "data" / "output"
PAUSE_SECONDS = 3


def states_with_addresses() -> list:
    b = pd.read_parquet(OUTPUT / "advisor_branches.parquet")
    b = b[b["branch_street1"].notna() & b["branch_state"].notna()]
    codes = sorted({s for s in b["branch_state"].unique()
                    if isinstance(s, str) and len(s) == 2 and s.isalpha()})
    return codes


def main() -> None:
    ap = argparse.ArgumentParser()
    # dash-prefixed aliases so the PowerShell-style flags in the docstring work too
    ap.add_argument("--states", "-States", default="", help="comma-separated codes")
    ap.add_argument("--force", "-Force", action="store_true", help="redo completed states")
    ap.add_argument("--no-export", "-NoExport", action="store_true", help="skip GeoJSON export")
    args = ap.parse_args()

    todo = ([s.strip().upper() for s in args.states.split(",") if s.strip()]
            if args.states else states_with_addresses())

    done, skipped, failed = [], [], []
    t0 = time.time()
    print(f"national geocode: {len(todo)} jurisdictions -> {', '.join(todo)}\n")

    for i, st in enumerate(todo, 1):
        target = INTERIM / f"branch_geocoded_{st}.parquet"
        if target.exists() and not args.force:
            print(f"[{i}/{len(todo)}] {st}: already done, skipping")
            skipped.append(st)
            continue

        print(f"[{i}/{len(todo)}] {st}  ({time.strftime('%H:%M:%S')})")
        try:
            geocode.geocode_state(st)
            if not args.no_export:
                export_geojson.export(st)
            done.append(st)
        except KeyboardInterrupt:
            print("\ninterrupted -- re-run to resume from here")
            break
        except Exception:
            print(f"  !! {st} FAILED\n{traceback.format_exc()}")
            failed.append(st)
        print()
        time.sleep(PAUSE_SECONDS)

    mins = (time.time() - t0) / 60
    print("=" * 60)
    print(f"finished in {mins:.1f} min | geocoded {len(done)} | skipped {len(skipped)} | failed {len(failed)}")
    if failed:
        print("failed: " + ", ".join(failed) + "   (re-run the script to retry these)")

    # roll-up across everything on disk
    files = sorted(INTERIM.glob("branch_geocoded_*.parquet"))
    if files:
        tot = placed = 0
        prec = {}
        advisors = set()
        for f in files:
            d = pd.read_parquet(f, columns=["advisor_crd", "lat", "geocode_precision"])
            tot += len(d)
            ok = d[d["lat"].notna()]
            placed += len(ok)
            advisors.update(ok["advisor_crd"].astype(str))
            for k, v in ok["geocode_precision"].value_counts().items():
                prec[k] = prec.get(k, 0) + int(v)
        print(f"\nacross {len(files)} state files: {placed:,}/{tot:,} rows placed "
              f"({placed / max(tot, 1):.1%}) | {len(advisors):,} distinct advisors")
        for k, v in sorted(prec.items(), key=lambda x: -x[1]):
            print(f"  {k:12s} {v:>9,}")


if __name__ == "__main__":
    main()

"""Enrich the latest Morgan Stanley FA roster from its public team pages.

This is intentionally a second stage. The advisor directory remains the source
for FAs; team pages add published people and richer role/contact facts without
claiming that every team member is an SEC-registered advisor.

Run with Chrome in remote-debug mode on port 9222:
    python src/morgan_team_async.py
    python src/morgan_team_async.py --limit 25 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time
from typing import List

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import ROSTER_DIR, latest, roster_path, scratch_path
from morgan_team_pages import (
    BASE_COLUMNS, audit_team_emails, compare_rosters, crawl_pages,
    merge_members, page_seeds, write_coverage_report,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_STAGE = "team_pages_v2"


def read_rows(path: pathlib.Path) -> List[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_cache(path: pathlib.Path) -> List[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} is not a list")
    return payload


def save_cache(path: pathlib.Path, pages: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(pages, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)


def needs_retry(page: dict | None) -> bool:
    """Retry transport/server throttles; retain permanent page outcomes."""
    status = int((page or {}).get("status") or 0)
    return status == 0 or status in {408, 425, 429} or status >= 500


def previous_roster(current: pathlib.Path) -> pathlib.Path | None:
    candidates = sorted(
        path for path in ROSTER_DIR.glob("morgan_stanley_*.csv")
        if path.resolve() != current.resolve())
    return candidates[-1] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cdp-host", default="http://127.0.0.1:9222")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--delay-ms", type=int, default=1500,
                        help="pause after each page request in each worker")
    parser.add_argument("--limit", type=int, default=0,
                        help="crawl only this many pages (0 means every page)")
    parser.add_argument("--dry-run", action="store_true",
                        help="crawl and report without writing roster or cache")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore today's resumable team-page cache")
    parser.add_argument("--replace-today", action="store_true",
                        help="allow replacement of today's completed roster")
    parser.add_argument("--publish-checkpoint", action="store_true",
                        help="publish verified cached pages without crawling unresolved pages")
    parser.add_argument("--wave-size", type=int, default=400,
                        help="pages per bounded crawl wave (0 disables waves)")
    parser.add_argument("--cooldown-seconds", type=int, default=300,
                        help="pause between waves to avoid Morgan's cumulative request ceiling")
    args = parser.parse_args()
    if args.wave_size < 0:
        raise SystemExit("--wave-size cannot be negative")
    if args.cooldown_seconds < 0:
        raise SystemExit("--cooldown-seconds cannot be negative")

    source = latest("morgan_stanley")
    if source is None:
        raise SystemExit("No Morgan Stanley roster is available")
    rows = read_rows(source)
    baseline_path = previous_roster(source)
    baseline_rows = read_rows(baseline_path) if baseline_path else rows
    directory = [row for row in rows
                 if str(row.get("Entity Type") or "ADVISOR").upper() != "TEAM_MEMBER"]
    team_rows = [row for row in directory if str(row.get("Team Name") or "").strip()]
    team_url_rows = [row for row in team_rows
                     if str(row.get("Team Page URL") or "").strip()]
    if team_rows and not team_url_rows:
        raise SystemExit(
            "Morgan directory rows contain team names but no Team Page URL values. "
            "Run src/morgan_stanley_async.py first; do not crawl individual profiles.")
    team_index_path = scratch_path("morgan_stanley", "teams", ext="csv")
    team_index = read_rows(team_index_path) if team_index_path.exists() else []
    seeds = page_seeds([*directory, *team_index])
    if args.limit:
        seeds = seeds[:args.limit]
        if not args.dry_run:
            raise SystemExit("--limit is a dry-run diagnostic; add --dry-run")
    print(f"[*] source {source.name}: {len(directory):,} directory rows")
    print(f"[*] comparison baseline: "
          f"{baseline_path.name if baseline_path else source.name}")
    print(f"[*] {len(team_url_rows):,}/{len(team_rows):,} team-associated rows "
          "publish c_teamPagesURL")
    print(f"[*] {len(team_index):,} branch-associated team index rows")
    print(f"[*] {len(seeds):,} distinct team-associated pages to inspect")

    # v1 was keyed by individual Profile URL and cannot prove team coverage.
    # A new cache namespace prevents those superficially successful responses
    # from being reused by the corrected crawler.
    cache_path = scratch_path("morgan_stanley", CACHE_STAGE)
    cached = [] if args.fresh or args.dry_run else load_cache(cache_path)
    by_url = {str(page.get("url") or ""): page for page in cached}
    pending = [seed for seed in seeds if needs_retry(by_url.get(seed.url))]
    if cached:
        print(f"[*] resumed {len(by_url):,} cached pages; {len(pending):,} remain")

    def progress(done: int, total: int, members: int, failures: int) -> None:
        print(f"    batch {done:>4}/{total:<4} pages  "
              f"{members:>5,} cards  {failures:>3} failures", flush=True)

    def checkpoint(pages: List[dict]) -> None:
        for page in pages:
            by_url[str(page.get("url") or "")] = page
        if not args.dry_run:
            save_cache(cache_path, list(by_url.values()))
            print(f"    checkpoint {len(by_url):,}/{len(seeds):,} pages", flush=True)

    if pending and not args.publish_checkpoint:
        wave_size = args.wave_size or len(pending)
        waves = [pending[start:start + wave_size]
                 for start in range(0, len(pending), wave_size)]
        for wave_number, wave in enumerate(waves, 1):
            print(f"[*] crawl wave {wave_number}/{len(waves)}: "
                  f"{len(wave):,} pages")
            crawl_pages(
                wave, cdp_host=args.cdp_host, concurrency=args.concurrency,
                batch_size=args.batch_size, delay_ms=args.delay_ms,
                progress=progress, checkpoint=checkpoint,
            )
            if wave_number < len(waves) and args.cooldown_seconds:
                remaining = args.cooldown_seconds
                while remaining:
                    wait = min(60, remaining)
                    print(f"[*] host cooldown: {remaining}s remaining", flush=True)
                    time.sleep(wait)
                    remaining -= wait
    unresolved = [seed.url for seed in seeds
                  if needs_retry(by_url.get(seed.url))]
    if unresolved and not args.publish_checkpoint:
        raise SystemExit(
            f"{len(unresolved):,} team pages remain unresolved; checkpoint retained, "
            "roster not replaced")
    selected_pages = [by_url[seed.url] for seed in seeds
                      if seed.url in by_url and not needs_retry(by_url[seed.url])]
    merged, summary = merge_members(directory, selected_pages)
    email_audit, email_audit_summary = audit_team_emails(
        team_index, selected_pages, merged)
    summary.update({
        "teamPagesInDirectory": len(seeds),
        "pagesResolved": len(selected_pages),
        "pagesUnresolved": len(unresolved),
        "crawlComplete": not unresolved,
        "publishedFromCheckpoint": bool(args.publish_checkpoint and unresolved),
        "cacheStage": CACHE_STAGE,
        "teamAssociatedDirectoryRows": len(team_rows),
        "teamUrlDirectoryRows": len(team_url_rows),
        "branchAssociatedTeamIndexRows": len(team_index),
        **email_audit_summary,
    })
    prior_team_members = sum(
        str(row.get("Entity Type") or "").upper() == "TEAM_MEMBER"
        for row in baseline_rows)
    if summary["teamMembersAdded"] < prior_team_members:
        raise SystemExit(
            f"refusing to reduce team-member coverage from {prior_team_members:,} "
            f"to {summary['teamMembersAdded']:,}")
    print("[*] coverage")
    for key, value in summary.items():
        if isinstance(value, bool):
            print(f"    {key}: {str(value).lower()}")
        else:
            print(f"    {key}: {value:,}" if isinstance(value, int)
                  else f"    {key}: {value}")

    if args.dry_run:
        print("[*] dry run: no files written")
        return

    target = roster_path("morgan_stanley", dt.date.today())
    if target.exists() and target.resolve() != source.resolve() and not args.replace_today:
        raise SystemExit(f"{target} already exists; pass --replace-today to replace it")
    pd.DataFrame(merged, columns=BASE_COLUMNS).to_csv(target, index=False)

    report = ROOT / "data" / "output" / "morgan_stanley_team_coverage.json"
    write_coverage_report(report, merged, summary)
    changes, change_summary = compare_rosters(baseline_rows, merged)
    change_report = (ROOT / "data" / "output" /
                     "morgan_stanley_refresh_comparison.csv")
    pd.DataFrame(changes).to_csv(change_report, index=False)
    change_summary_path = (ROOT / "data" / "output" /
                           "morgan_stanley_refresh_comparison.json")
    change_summary_path.write_text(
        json.dumps({
            "baseline": baseline_path.name if baseline_path else source.name,
            "current": target.name,
            **change_summary,
        }, indent=2) + "\n",
        encoding="utf-8")
    email_audit_path = (ROOT / "data" / "output" /
                        "morgan_stanley_team_email_audit.csv")
    pd.DataFrame(email_audit).to_csv(email_audit_path, index=False)
    added = pd.DataFrame([row for row in merged
                          if row.get("Entity Type") == "TEAM_MEMBER"],
                         columns=BASE_COLUMNS)
    added.to_csv(ROOT / "data" / "output" /
                 "morgan_stanley_team_members.csv", index=False)
    print(f"    roster -> {target}")
    print(f"    report -> {report}")
    print(f"    comparison -> {change_report}")
    print(f"    comparison summary -> {change_summary_path}")
    print(f"    email audit -> {email_audit_path}")
    print(f"    review -> {ROOT / 'data' / 'output' / 'morgan_stanley_team_members.csv'}")


if __name__ == "__main__":
    main()

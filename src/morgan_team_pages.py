"""Morgan Stanley team-page discovery and safe roster enrichment.

The public advisor-search API is an FA/office directory. Team pages are a
different source: they publish analysts, registered associates, schedulers and
client-service staff alongside the FA who owns the page. This module keeps the
two claims separate and only adds published person facts. It never assigns an
advisor CRD.

Pages are fetched in a temporary, cookie-free context inside the user's
existing Chrome debug process. Morgan Stanley rejects ordinary HTTP clients,
while same-origin browser fetches work. The crawler neither reads nor modifies
the user's normal browser context, tabs, or cookies.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, MutableMapping, Sequence

from playwright.sync_api import sync_playwright


ALLOWED_HOSTS = {
    "advisor.morganstanley.com",
    "graystone.morganstanley.com",
}
EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
KEY_RE = re.compile(r"[^a-z0-9]+")
PUBLISHED_EMAIL_DOMAINS = {
    "morganstanley.com", "morganstanleypwm.com", "ms.com", "msgraystone.com",
}
BOOTSTRAP_URLS = {
    "advisor.morganstanley.com": "https://advisor.morganstanley.com/the-condron-team",
    "graystone.morganstanley.com": (
        "https://graystone.morganstanley.com/global-institutional-advisory-solutions"),
}

# Parse inside the browser so hundreds of megabytes of HTML do not cross CDP.
# Schema.org attributes are preferred; Morgan CSS classes locate each card.
EXTRACT_BATCH_JS = r"""
async ({urls, concurrency, delayMs}) => {
  let cursor = 0;
  const results = [];
  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const one = async url => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        cache: "no-store", credentials: "same-origin", redirect: "follow",
        signal: controller.signal
      });
      if (!response.ok) return {url, status: response.status, members: []};
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const team = clean(doc.querySelector("#location-name")?.textContent);
      const seen = new Set();
      const members = [];
      for (const slide of doc.querySelectorAll(".BiosCarousel-slide")) {
        const name = clean(slide.querySelector('[itemprop="name"]')?.textContent);
        if (!name) continue;
        const titles = [...slide.querySelectorAll('[itemprop="jobTitle"]')]
          .map(node => clean(node.textContent)).filter(Boolean);
        const phone = clean(slide.querySelector('a[href^="tel:"]')?.getAttribute("href"))
          .replace(/^tel:/i, "");
        const linkedin = [...slide.querySelectorAll('a[itemprop="sameAs"]')]
          .map(node => node.href).find(href => /linkedin\.com/i.test(href)) || "";
        const personId = clean(slide.getAttribute("data-goto-id") ||
          slide.querySelector("article")?.getAttribute("data-yext-id"));
        const email = personId.includes("@") ? personId.toLowerCase() : "";
        const key = email || [name.toLowerCase(), titles.join("|").toLowerCase(), phone].join("|");
        if (seen.has(key)) continue;
        seen.add(key);
        members.push({name, titles, phone, linkedin, personId, email});
      }
      return {url, finalUrl: response.url, status: response.status, team, members};
    } catch (error) {
      return {url, status: 0, error: clean(error?.message || error), members: []};
    } finally {
      clearTimeout(timer);
    }
  };
  const worker = async () => {
    while (cursor < urls.length) {
      const index = cursor++;
      results[index] = await one(urls[index]);
      if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  };
  await Promise.all(Array.from({length: Math.max(1, concurrency)}, worker));
  return results;
}
"""


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def email(value: object) -> str:
    candidate = clean(value).lower()
    if not EMAIL_RE.fullmatch(candidate):
        return ""
    return candidate if candidate.rsplit("@", 1)[-1] in PUBLISHED_EMAIL_DOMAINS else ""


def name_key(value: object) -> str:
    return KEY_RE.sub("", clean(value).lower())


def canonical_page_url(value: object) -> str:
    raw = clean(value)
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


@dataclass(frozen=True)
class PageSeed:
    url: str
    team: str
    branch: str
    office_number: str
    complex_id: str
    street1: str
    street2: str
    city: str
    state: str
    postal: str


def _mode(values: Iterable[object]) -> str:
    tidy = [clean(value) for value in values if clean(value)]
    if not tidy:
        return ""
    counts = collections.Counter(tidy)
    best = max(counts.values())
    return next(value for value in tidy if counts[value] == best)


def page_seeds(rows: Sequence[Mapping[str, object]]) -> List[PageSeed]:
    """One trustworthy same-origin seed for every published team-page URL.

    Never fall back to Profile URL here. Morgan's directory uses that field
    for an FA's individual profile, while Team Page URL is c_teamPagesURL.
    Conflating the two silently drops associates and support professionals.
    """
    grouped: MutableMapping[str, List[Mapping[str, object]]] = collections.defaultdict(list)
    for row in rows:
        if not clean(row.get("Team Name")):
            continue
        url = canonical_page_url(row.get("Team Page URL"))
        if url:
            grouped[url].append(row)
    out = []
    for url, group in sorted(grouped.items()):
        get = lambda column: _mode(row.get(column) for row in group)
        out.append(PageSeed(
            url=url, team=get("Team Name"), branch=get("Branch Name"),
            office_number=get("Office Number"), complex_id=get("Complex ID"),
            street1=get("Street Line 1"), street2=get("Street Line 2"),
            city=get("City"), state=get("State"), postal=get("Postal Code"),
        ))
    return out

def blocked_batch(results: Sequence[Mapping[str, object]]) -> bool:
    """A whole batch of transport/throttle failures means stopless retry is harmful."""
    if not results:
        return False
    def blocked(result: Mapping[str, object]) -> bool:
        status = int(result.get("status") or 0)
        return status == 0 or status in {403, 408, 425, 429} or status >= 500
    return all(blocked(result) for result in results)


def open_bootstrap(context, url: str, attempts: int = 10,
                   cooldown_seconds: int = 60):
    """Open a same-origin page, riding out Morgan's temporary connection reset."""
    if attempts < 1:
        raise ValueError("bootstrap attempts must be positive")
    last_error = None
    for attempt in range(1, attempts + 1):
        page = context.new_page()
        try:
            page.goto(url, wait_until="commit", timeout=30_000)
            return page
        except Exception as error:
            last_error = error
            try:
                page.close()
            except Exception:
                pass
            if attempt == attempts:
                break
            print(f"[*] bootstrap attempt {attempt}/{attempts} failed; "
                  f"retrying in {cooldown_seconds}s", flush=True)
            if cooldown_seconds:
                time.sleep(cooldown_seconds)
    raise RuntimeError(
        f"Morgan bootstrap failed after {attempts} attempts") from last_error


def crawl_pages(
    seeds: Sequence[PageSeed], cdp_host: str = "http://127.0.0.1:9222",
    concurrency: int = 1, batch_size: int = 20, delay_ms: int = 1_500,
    progress: Callable[[int, int, int, int], None] | None = None,
    checkpoint: Callable[[List[dict]], None] | None = None,
) -> List[dict]:
    """Fetch and parse public team pages without exporting browser state."""
    if concurrency < 1 or concurrency > 12:
        raise ValueError("concurrency must be between 1 and 12")
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    if delay_ms < 0 or delay_ms > 5_000:
        raise ValueError("delay_ms must be between 0 and 5000")
    by_origin: MutableMapping[str, List[PageSeed]] = collections.defaultdict(list)
    for seed in seeds:
        parsed = urllib.parse.urlsplit(seed.url)
        by_origin[f"{parsed.scheme}://{parsed.netloc}"].append(seed)

    found: List[dict] = []
    done = failures = members = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_host)
        # A temporary context protects user tabs/cookies and resets Morgan's
        # cumulative per-session request budget at each bounded crawl wave.
        context = browser.new_context()
        try:
            for origin, origin_seeds in sorted(by_origin.items()):
                bootstrap = BOOTSTRAP_URLS.get(
                    urllib.parse.urlsplit(origin).hostname or "",
                    origin_seeds[0].url)
                page = open_bootstrap(context, bootstrap)
                try:
                    for start in range(0, len(origin_seeds), batch_size):
                        batch = origin_seeds[start:start + batch_size]
                        results = page.evaluate(
                            EXTRACT_BATCH_JS,
                            {"urls": [seed.url for seed in batch],
                             "concurrency": concurrency, "delayMs": delay_ms},
                        )
                        for seed, result in zip(batch, results):
                            result["seed"] = seed.__dict__
                            found.append(result)
                            done += 1
                            failures += int(int(result.get("status") or 0) != 200)
                            members += len(result.get("members") or [])
                        if checkpoint:
                            checkpoint(found[-len(batch):])
                        if progress:
                            progress(done, len(seeds), members, failures)
                        if blocked_batch(results):
                            raise RuntimeError(
                                "Morgan team-page host blocked the entire batch; "
                                "checkpoint retained, stop and resume later")
                finally:
                    page.close()
        finally:
            context.close()
        # This browser belongs to the user. Never call browser.close().
    return found


BASE_COLUMNS = [
    "Name", "Primary Title", "Secondary Titles", "Team Name", "Branch Name",
    "Office Number", "FA Number", "Complex ID", "Main Phone", "Branch Phone",
    "LinkedIn", "Email", "Certifications", "Street Line 1", "Street Line 2", "City", "State",
    "Postal Code", "Profile URL", "Entity Type", "Source", "Team Page URL",
    "Yext ID",
]


def merge_members(directory_rows: Sequence[Mapping[str, object]],
                  pages: Sequence[Mapping[str, object]]) -> tuple[List[dict], dict]:
    """Merge published people while keeping directory FAs authoritative."""
    output = [{column: clean(row.get(column)) for column in BASE_COLUMNS}
              for row in directory_rows]
    for row in output:
        row["Entity Type"] = row["Entity Type"] or "ADVISOR"
        row["Source"] = row["Source"] or "directory"

    by_email: Dict[str, int] = {}
    by_page_name: Dict[tuple[str, str], int] = {}
    for index, row in enumerate(output):
        if email(row["Email"]):
            by_email.setdefault(email(row["Email"]), index)
        page = canonical_page_url(row["Team Page URL"])
        if page and name_key(row["Name"]):
            by_page_name.setdefault((page, name_key(row["Name"])), index)

    directory_count = len(output)
    added = existing = invalid_email = duplicate_cards = 0
    added_keys = set()
    for page in pages:
        if int(page.get("status") or 0) != 200:
            continue
        seed = page.get("seed") or {}
        page_url = canonical_page_url(page.get("finalUrl") or page.get("url"))
        if not page_url:
            continue
        team = clean(page.get("team")) or clean(seed.get("team"))
        for member in page.get("members") or []:
            member_email = email(member.get("email"))
            if clean(member.get("email")) and not member_email:
                invalid_email += 1
            member_name = clean(member.get("name"))
            if not member_name:
                continue
            existing_index = by_email.get(member_email) if member_email else None
            if existing_index is None:
                existing_index = by_page_name.get((page_url, name_key(member_name)))
            if existing_index is not None:
                row = output[existing_index]
                titles = [clean(value) for value in member.get("titles") or []
                          if clean(value)]
                fills = {
                    "Primary Title": ", ".join(dict.fromkeys(titles)),
                    "Team Name": team, "Main Phone": clean(member.get("phone")),
                    "LinkedIn": clean(member.get("linkedin")),
                    "Email": member_email, "Profile URL": page_url,
                    "Team Page URL": page_url,
                    "Yext ID": clean(member.get("personId")),
                }
                for column, value in fills.items():
                    if value and not row[column]:
                        row[column] = value
                if existing_index < directory_count:
                    existing += 1
                else:
                    duplicate_cards += 1
                continue

            identity = member_email or (page_url, name_key(member_name))
            if identity in added_keys:
                duplicate_cards += 1
                continue
            added_keys.add(identity)
            titles = [clean(value) for value in member.get("titles") or []
                      if clean(value)]
            row = {column: "" for column in BASE_COLUMNS}
            row.update({
                "Name": member_name,
                "Primary Title": ", ".join(dict.fromkeys(titles)),
                "Team Name": team,
                "Branch Name": clean(seed.get("branch")),
                "Office Number": clean(seed.get("office_number")),
                "Complex ID": clean(seed.get("complex_id")),
                "Main Phone": clean(member.get("phone")),
                "LinkedIn": clean(member.get("linkedin")),
                "Email": member_email,
                "Street Line 1": clean(seed.get("street1")),
                "Street Line 2": clean(seed.get("street2")),
                "City": clean(seed.get("city")),
                "State": clean(seed.get("state")),
                "Postal Code": clean(seed.get("postal")),
                "Profile URL": page_url,
                "Entity Type": "TEAM_MEMBER", "Source": "team_page",
                "Team Page URL": page_url,
                "Yext ID": clean(member.get("personId")),
            })
            output.append(row)
            index = len(output) - 1
            if member_email:
                by_email.setdefault(member_email, index)
            by_page_name.setdefault((page_url, name_key(member_name)), index)
            added += 1

    summary = {
        "directoryRows": len(directory_rows), "outputRows": len(output),
        "pagesAttempted": len(pages),
        "pagesSucceeded": sum(int(page.get("status") or 0) == 200 for page in pages),
        "pagesWithMembers": sum(bool(page.get("members")) for page in pages),
        "publishedCards": sum(len(page.get("members") or []) for page in pages),
        "directoryCardsConfirmed": existing, "teamMembersAdded": added,
        "invalidPublishedEmails": invalid_email,
        "duplicateCardsSuppressed": duplicate_cards,
        "addedWithEmail": sum(row["Entity Type"] == "TEAM_MEMBER" and
                              bool(row["Email"]) for row in output),
    }
    return output, summary


def write_coverage_report(path: pathlib.Path, rows: Sequence[Mapping[str, object]],
                          summary: Mapping[str, object]) -> None:
    added = [row for row in rows if row.get("Entity Type") == "TEAM_MEMBER"]
    title_labels: Dict[str, str] = {}
    by_title = collections.Counter()
    for row in added:
        label = clean(row.get("Primary Title")) or "(blank)"
        key = label.casefold()
        title_labels.setdefault(key, label)
        by_title[key] += 1
    payload = dict(summary)
    payload["addedByTitle"] = {
        title_labels[key]: count for key, count in by_title.most_common()}
    payload["addedWithPhone"] = sum(bool(clean(row.get("Main Phone")))
                                    for row in added)
    payload["addedWithTeam"] = sum(bool(clean(row.get("Team Name")))
                                   for row in added)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


COMPARE_FIELDS = (
    "Name", "Primary Title", "Team Name", "Main Phone", "Email", "LinkedIn",
    "Street Line 1", "City", "State", "Profile URL", "Team Page URL",
)


def roster_identity(row: Mapping[str, object]) -> str:
    """Stable comparison key; it is not an advisor-identity assertion."""
    entity = clean(row.get("Entity Type") or "ADVISOR").upper()
    fa_number = clean(row.get("FA Number"))
    if entity == "ADVISOR" and fa_number:
        return f"advisor_fa:{fa_number.lower()}"
    published_email = email(row.get("Email"))
    if published_email:
        return f"email:{published_email}"
    person = name_key(row.get("Name"))
    team_page = canonical_page_url(row.get("Team Page URL"))
    if person and team_page:
        return f"team_name:{team_page}|{person}"
    profile = canonical_page_url(row.get("Profile URL"))
    if person and profile:
        return f"profile_name:{profile}|{person}"
    return "|".join((
        "fallback", entity, person, name_key(row.get("Team Name")),
        name_key(row.get("City")), clean(row.get("State")).lower(),
    ))


def compare_rosters(before: Sequence[Mapping[str, object]],
                    after: Sequence[Mapping[str, object]]) -> tuple[List[dict], dict]:
    """Describe coverage changes without using the result as a match source."""
    grouped_before: MutableMapping[str, List[Mapping[str, object]]] = (
        collections.defaultdict(list))
    grouped_after: MutableMapping[str, List[Mapping[str, object]]] = (
        collections.defaultdict(list))
    for row in before:
        grouped_before[roster_identity(row)].append(row)
    for row in after:
        grouped_after[roster_identity(row)].append(row)

    details: List[dict] = []
    statuses = collections.Counter()
    all_keys = sorted(set(grouped_before) | set(grouped_after))
    for identity in all_keys:
        old = grouped_before.get(identity, [])
        new = grouped_after.get(identity, [])
        sample = (new or old)[0]
        if len(old) > 1 or len(new) > 1:
            status = "ambiguous_comparison_key"
            improved = changed = []
        elif not old:
            status = "added"
            improved = changed = []
        elif not new:
            status = "removed"
            improved = changed = []
        else:
            prior, current = old[0], new[0]
            improved = [
                field for field in COMPARE_FIELDS
                if not clean(prior.get(field)) and clean(current.get(field))]
            changed = [
                field for field in COMPARE_FIELDS
                if clean(prior.get(field)) and clean(current.get(field))
                and clean(prior.get(field)).casefold()
                != clean(current.get(field)).casefold()]
            status = ("changed" if changed else
                      "improved" if improved else "unchanged")
        statuses[status] += 1
        details.append({
            "status": status,
            "identity_key": identity,
            "entity_type": clean(sample.get("Entity Type") or "ADVISOR"),
            "name": clean(sample.get("Name")),
            "team_name": clean(sample.get("Team Name")),
            "email": email(sample.get("Email")),
            "improved_fields": " | ".join(improved),
            "changed_fields": " | ".join(changed),
            "before_rows": len(old),
            "after_rows": len(new),
        })
    summary = {
        "beforeRows": len(before),
        "afterRows": len(after),
        **{status: statuses[status] for status in (
            "added", "removed", "improved", "changed", "unchanged",
            "ambiguous_comparison_key")},
    }
    return details, summary


def audit_team_emails(
    team_index: Sequence[Mapping[str, object]],
    pages: Sequence[Mapping[str, object]],
    roster_rows: Sequence[Mapping[str, object]] = (),
) -> tuple[List[dict], dict]:
    """Cross-check branch-associated email sets against parsed team cards."""
    expected: MutableMapping[str, set[str]] = collections.defaultdict(set)
    team_names: Dict[str, str] = {}
    for row in team_index:
        url = canonical_page_url(row.get("Team Page URL"))
        if not url:
            continue
        team_names[url] = clean(row.get("Team Name"))
        for value in clean(row.get("Published Team Emails")).split(" | "):
            published = email(value)
            if published:
                expected[url].add(published)

    extracted: MutableMapping[str, set[str]] = collections.defaultdict(set)
    for page in pages:
        if int(page.get("status") or 0) != 200:
            continue
        url = canonical_page_url(page.get("finalUrl") or page.get("url"))
        for member in page.get("members") or []:
            published = email(member.get("email"))
            if url and published:
                extracted[url].add(published)

    roster_emails: MutableMapping[str, set[str]] = collections.defaultdict(set)
    for row in roster_rows:
        url = canonical_page_url(row.get("Team Page URL"))
        published = email(row.get("Email"))
        if url and published:
            roster_emails[url].add(published)

    known_person = collections.defaultdict(set)
    for url in set(extracted) | set(roster_emails):
        known_person[url].update(extracted[url])
        known_person[url].update(roster_emails[url])

    details = []
    statuses = collections.Counter()
    for url in sorted(set(expected) | set(known_person)):
        for published in sorted(expected[url] | known_person[url]):
            in_branch = published in expected[url]
            in_person = published in known_person[url]
            status = ("confirmed" if in_branch and in_person else
                      "branch_only" if in_branch else "person_only")
            statuses[status] += 1
            details.append({
                "status": status, "team_name": team_names.get(url, ""),
                "team_page_url": url, "email": published,
            })
    return details, {
        "branchPublishedEmails": sum(len(values) for values in expected.values()),
        "pageExtractedEmails": sum(len(values) for values in extracted.values()),
        "rosterEmailsOnTeamPages": sum(
            len(values) for values in roster_emails.values()),
        "confirmedEmails": statuses["confirmed"],
        "branchOnlyEmails": statuses["branch_only"],
        "personOnlyEmails": statuses["person_only"],
    }

"""Measure how published Morgan team people reach the CRD-keyed application."""
from __future__ import annotations

import collections
import csv
import json
import pathlib
import re
from typing import Iterable, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
MORGAN_CRD = "149777"


def norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def grouped_index(items: Iterable[tuple[object, Mapping[str, object]]], key) -> dict:
    grouped = collections.defaultdict(list)
    for item_id, item in items:
        value = key(item)
        if value and all(value):
            grouped[value].append((str(item_id), item))
    return dict(grouped)


def classify(team_rows: list[dict], advisors: Mapping[str, dict]) -> tuple[list[dict], dict]:
    morgan = [(crd, row) for crd, row in advisors.items()
              if str(row.get("fc") or "") == MORGAN_CRD]
    by_email = grouped_index(morgan, lambda row: (str(row.get("e") or "").lower(),))
    by_name_team = grouped_index(
        morgan, lambda row: (norm(row.get("n")), norm(row.get("tn"))))

    output = []
    methods = collections.Counter()
    for source in team_rows:
        published_email = str(source.get("Email") or "").strip().lower()
        name_team = (norm(source.get("Name")), norm(source.get("Team Name")))
        candidates = by_email.get((published_email,), []) if published_email else []
        reason = ""
        if len(candidates) == 1:
            found = candidates[0]
            method = "exact_email"
        elif len(candidates) > 1:
            found = None
            method = "ambiguous_email"
            reason = "published_email_links_to_multiple_app_crds"
        else:
            candidates = by_name_team.get(name_team, [])
            if len(candidates) == 1:
                found = candidates[0]
                method = "unique_exact_name_team"
            elif len(candidates) > 1:
                found = None
                method = "ambiguous_name_team"
                reason = "name_and_team_link_to_multiple_app_crds"
            else:
                found = None
                method = "unresolved"
                reason = ("published_email_not_linked_to_app_crd"
                          if published_email
                          else "no_email_or_unique_name_team_link")
        crd, advisor = found if found else ("", {})
        row = dict(source)
        row.update({
            "app_match_method": method,
            "app_crd": crd,
            "app_name": str(advisor.get("n") or ""),
            "app_tier": str(advisor.get("t") or ""),
            "unresolved_reason": reason,
        })
        output.append(row)
        methods[method] += 1

    unresolved = [row for row in output if not row["app_crd"]]
    summary = {
        "publishedTeamMembers": len(team_rows),
        "publishedWithEmail": sum(bool(str(row.get("Email") or "").strip())
                                  for row in team_rows),
        "appMorganContacts": len(morgan),
        "presentByExactEmail": methods["exact_email"],
        "presentByUniqueExactNameTeam": methods["unique_exact_name_team"],
        "presentTotal": sum(bool(row["app_crd"]) for row in output),
        "ambiguousEmail": methods["ambiguous_email"],
        "ambiguousNameTeam": methods["ambiguous_name_team"],
        "unresolved": len(unresolved),
        "unresolvedWithPublishedEmail": sum(
            bool(str(row.get("Email") or "").strip()) for row in unresolved),
        "unresolvedWithoutPublishedEmail": sum(
            not bool(str(row.get("Email") or "").strip()) for row in unresolved),
        "linkedByTitle": dict(collections.Counter(
            str(row.get("Primary Title") or "(blank)")
            for row in output if row["app_crd"]).most_common()),
    }
    return output, summary


def main() -> None:
    source = ROOT / "data" / "output" / "morgan_stanley_team_members.csv"
    contacts = ROOT / "webapp" / "data" / "contacts.json"
    with source.open(encoding="utf-8-sig", newline="") as handle:
        team_rows = list(csv.DictReader(handle))
    advisors = json.loads(contacts.read_text(encoding="utf-8"))["advisors"]
    rows, summary = classify(team_rows, advisors)

    report = ROOT / "data" / "output" / "morgan_stanley_team_app_coverage.json"
    report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    fields = list(team_rows[0]) + [
        "app_match_method", "app_crd", "app_name", "app_tier", "unresolved_reason"]
    crosswalk = ROOT / "data" / "output" / "morgan_stanley_team_app_crosswalk.csv"
    with crosswalk.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    review = ROOT / "data" / "output" / "morgan_stanley_team_app_review.csv"
    with review.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row for row in rows if not row["app_crd"])
    console = {key: value for key, value in summary.items()
               if key != "linkedByTitle"}
    console["topLinkedTitles"] = dict(
        list(summary["linkedByTitle"].items())[:15])
    print(json.dumps(console, indent=2))
    print(f"[*] report -> {report}")
    print(f"[*] crosswalk -> {crosswalk}")
    print(f"[*] unresolved review -> {review}")


if __name__ == "__main__":
    main()

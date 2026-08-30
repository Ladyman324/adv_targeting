"""Compare two contact/recipient releases and emit local drill-down reports."""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import pathlib
import re
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import FIRMS
from forbes_match import split_name
from sec_names import load_advisor_aliases, surname_key, value_tokens

ROOT = pathlib.Path(__file__).resolve().parents[1]
NICOLE_EMAIL, NICOLE_CRD = "nicole.tesoriero@ubs.com", "4784023"


def _text(value) -> str:
    return str(value or "").strip()


def _email(value) -> str:
    value = _text(value).lower()
    return value if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) else ""


def _json(path: pathlib.Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_contacts(path: pathlib.Path) -> list[dict]:
    rows = []
    for crd, raw in (_json(path).get("advisors") or {}).items():
        email = _email(raw.get("e"))
        phone = re.sub(r"\D", "", _text(raw.get("w") or raw.get("c")))[-10:]
        source, name = _text(raw.get("src")), _text(raw.get("n"))
        key = (f"email:{email}" if email else
               f"contact:{source.lower()}|{name.lower()}|{phone}")
        rows.append({
            "key": key, "crd": _text(crd), "email": email, "name": name,
            "phone": phone, "source": source, "tier": _text(raw.get("t")).lower(),
            "score": raw.get("ms", ""), "source_firm": _text(raw.get("fc")),
            "roster_family": "|".join(str(x) for x in raw.get("rf", []) or []),
        })
    return rows


def load_firms(path: pathlib.Path) -> dict[str, set[str]]:
    frame = pd.read_parquet(path, columns=["advisor_crd", "firm_crd"]).fillna("")
    out: dict[str, set[str]] = collections.defaultdict(set)
    for row in frame.itertuples(index=False):
        if _text(row.advisor_crd) and _text(row.firm_crd):
            out[_text(row.advisor_crd)].add(_text(row.firm_crd))
    return dict(out)


def load_sec_names(advisors_path: pathlib.Path,
                   aliases_path: pathlib.Path) -> dict[str, list[dict]]:
    """Legal and person-specific SEC name rows, keyed by CRD."""
    frame = pd.read_parquet(
        advisors_path,
        columns=["advisor_crd", "first_name", "middle_name", "last_name",
                 "suffix", "used_first_name"]).fillna("")
    aliases = load_advisor_aliases(aliases_path)
    out: dict[str, list[dict]] = collections.defaultdict(list)
    for row in frame.itertuples(index=False):
        crd = _text(row.advisor_crd)
        out[crd].append({
            "first_name": " ".join(x for x in
                                   (_text(row.first_name),
                                    _text(row.used_first_name)) if x),
            "middle_name": _text(row.middle_name),
            "last_name": _text(row.last_name),
            "suffix": _text(row.suffix),
        })
        out[crd].extend(aliases.get(crd, []))
    return dict(out)


def sec_published_name_exact(contact: dict, crd: str,
                             names: dict[str, list[dict]]) -> bool:
    """True only for a literal non-initial given name on one SEC name row."""
    given, last = split_name(contact.get("name", ""))
    published_given = {token for token in given if len(token) > 1}
    if not last or not published_given:
        return False
    return any(
        surname_key(row.get("last_name")) == last and
        bool(published_given & value_tokens(row.get("first_name")))
        for row in names.get(_text(crd), []))


def review_email_moves(moves: list[dict], before: list[dict], after: list[dict],
                       before_firms: dict[str, set[str]],
                       after_firms: dict[str, set[str]],
                       sec_names: dict[str, list[dict]]) -> list[dict]:
    """Explain only moves where the new CRD has stronger literal SEC identity."""
    old_by_email = _group([row for row in before if row.get("email")])
    new_by_email = _group([row for row in after if row.get("email")])
    # _group keys contacts by stable key; email keys are deterministic here.
    reviewed = []
    for move in moves:
        key = "email:" + move["email"]
        old_rows, new_rows = old_by_email.get(key, []), new_by_email.get(key, [])
        old = old_rows[0] if len(old_rows) == 1 else None
        new = new_rows[0] if len(new_rows) == 1 else None
        old_exact = bool(old and sec_published_name_exact(
            old, move["baseline_crds"], sec_names))
        new_exact = bool(new and sec_published_name_exact(
            new, move["candidate_crds"], sec_names))
        new_relation = relationship(new, after_firms) if new else "missing_contact"
        explained = bool(new_exact and not old_exact and new_relation in
                         {"exact_agree", "family_agree"})
        reviewed.append({
            **move,
            "baseline_name": _text((old or {}).get("name")),
            "candidate_name": _text((new or {}).get("name")),
            "baseline_sec_name_exact": old_exact,
            "candidate_sec_name_exact": new_exact,
            "baseline_relation": relationship(old, before_firms) if old else
                                 "missing_contact",
            "candidate_relation": new_relation,
            "explained": explained,
            "explanation": ("new_crd_has_literal_sec_name_and_current_firm_family"
                            if explained else "manual_review_required"),
        })
    return reviewed


def source_families() -> dict[str, set[str]]:
    return {meta["label"]: {str(x) for x in meta["crds"]}
            for meta in FIRMS.values()}


def relationship(row: dict, firms: dict[str, set[str]]) -> str:
    family = set(filter(None, row.get("roster_family", "").split("|")))
    family = family or source_families().get(row.get("source"), set())
    if not family:
        return "not_roster"
    current = firms.get(row.get("crd", ""), set())
    if not current:
        return "unknown_sec_firm"
    if row.get("source_firm") in current:
        return "exact_agree"
    return "family_agree" if family & current else "cross_family"


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        out[row["key"]].append(row)
    return dict(out)


def compare_contacts(before: list[dict], after: list[dict],
                     before_firms: dict[str, set[str]],
                     after_firms: dict[str, set[str]]) -> tuple[pd.DataFrame, dict]:
    old, new = _group(before), _group(after)
    changes, detail = collections.Counter(), []
    for key in sorted(set(old) | set(new)):
        left, right = old.get(key, []), new.get(key, [])
        if len(left) != 1 or len(right) != 1:
            kind = "ambiguous_key" if left and right else "added" if right else "removed"
            changes[kind] += max(len(left), len(right))
            detail.append({"key": key, "change": kind,
                           "baseline_crds": "|".join(sorted(x["crd"] for x in left)),
                           "candidate_crds": "|".join(sorted(x["crd"] for x in right))})
            continue
        a, b = left[0], right[0]
        if a["crd"] != b["crd"]:
            kind = "crd_changed"
        elif a["tier"] != b["tier"]:
            kind = "tier_changed"
        elif any(a[field] != b[field] for field in ("email", "name", "phone", "source")):
            kind = "contact_changed"
        else:
            kind = "unchanged"
        changes[kind] += 1
        if kind != "unchanged":
            detail.append({
                "key": key, "change": kind,
                "baseline_crds": a["crd"], "candidate_crds": b["crd"],
                "baseline_name": a["name"], "candidate_name": b["name"],
                "baseline_source": a["source"], "candidate_source": b["source"],
                "baseline_tier": a["tier"], "candidate_tier": b["tier"],
                "baseline_relation": relationship(a, before_firms),
                "candidate_relation": relationship(b, after_firms),
            })

    def counts(rows, firms):
        return {
            "total": len(rows),
            "tiers": dict(sorted(collections.Counter(r["tier"] for r in rows).items())),
            "sources": dict(sorted(collections.Counter(r["source"] for r in rows).items())),
            "firmRelationships": dict(sorted(collections.Counter(
                relationship(r, firms) for r in rows).items())),
        }
    return pd.DataFrame(detail), {
        "baseline": counts(before, before_firms),
        "candidate": counts(after, after_firms),
        "changes": dict(sorted(changes.items())),
        "duplicateKeys": {"baseline": sum(len(v) > 1 for v in old.values()),
                          "candidate": sum(len(v) > 1 for v in new.values())},
    }


def compare_registries(before: dict, after: dict) -> tuple[pd.DataFrame, dict]:
    def rows(payload):
        return {str(crd): row for crd, row in (payload.get("recipients") or {}).items()}
    old, new = rows(before), rows(after)
    detail, counts = [], collections.Counter()
    for crd in sorted(set(old) | set(new)):
        a, b = old.get(crd), new.get(crd)
        kind = "added" if a is None else "removed" if b is None else "unchanged"
        if a is not None and b is not None and any(
                a.get(field) != b.get(field) for field in
                ("email", "name", "source", "tier", "routingHash", "actContactId")):
            kind = "changed"
        counts[kind] += 1
        if kind != "unchanged":
            detail.append({"event": kind, "crd": crd,
                           "baseline_email": _email((a or {}).get("email")),
                           "candidate_email": _email((b or {}).get("email")),
                           "baseline_source": _text((a or {}).get("source")),
                           "candidate_source": _text((b or {}).get("source"))})
    def by_email(records):
        out: dict[str, set[str]] = collections.defaultdict(set)
        for crd, row in records.items():
            if _email(row.get("email")):
                out[_email(row.get("email"))].add(crd)
        return out
    old_email, new_email = by_email(old), by_email(new)
    moved = []
    for email in sorted(set(old_email) & set(new_email)):
        if old_email[email] != new_email[email]:
            moved.append({"event": "email_crd_moved", "crd": "", "email": email,
                          "baseline_crds": "|".join(sorted(old_email[email])),
                          "candidate_crds": "|".join(sorted(new_email[email]))})
    detail.extend(moved); counts["email_crd_moved"] = len(moved)
    return pd.DataFrame(detail), {
        "baseline": len(old), "candidate": len(new),
        "changes": dict(sorted(counts.items())),
        "ineligible": {"baseline": len(before.get("ineligible") or {}),
                       "candidate": len(after.get("ineligible") or {})},
        "emailMoves": moved,
    }


def review_added_routes(before: dict, after: dict, contacts: list[dict],
                        firms: dict[str, set[str]]) -> tuple[pd.DataFrame, dict]:
    """Audit every newly outbound-authorized CRD, not only moved emails."""
    old = before.get("recipients") or {}
    new = after.get("recipients") or {}
    by_crd = {row["crd"]: row for row in contacts}
    email_counts = collections.Counter(
        _email(row.get("email")) for row in new.values()
        if _email(row.get("email")))
    source_family = source_families()
    detail = []
    for crd in sorted(set(new) - set(old)):
        recipient = new[crd]
        contact = by_crd.get(str(crd))
        tier = _text(recipient.get("tier")).lower()
        source = _text(recipient.get("source"))
        email = _email(recipient.get("email"))
        try:
            score = float(recipient.get("matchScore") or 0)
        except (TypeError, ValueError):
            score = 0.0
        relation = relationship(contact, firms) if contact else "missing_contact"
        issues = []
        if tier not in {"confirmed", "high"}:
            issues.append("unauthorized_tier")
        if not email:
            issues.append("missing_or_invalid_email")
        elif email_counts[email] != 1:
            issues.append("email_not_unique")
        if tier == "high" and score <= 0:
            issues.append("high_route_missing_score")
        if not contact:
            issues.append("missing_contact")
        elif source in source_family and relation not in {"exact_agree", "family_agree"}:
            issues.append("roster_current_firm_disagrees")
        detail.append({
            "crd": crd, "email": email, "name": _text(recipient.get("name")),
            "source": source, "tier": tier, "match_score": score,
            "firm_relation": relation, "safe": not issues,
            "issues": "|".join(issues),
        })
    counts = collections.Counter(
        issue for row in detail for issue in row["issues"].split("|") if issue)
    return pd.DataFrame(detail), {
        "total": len(detail),
        "safe": sum(bool(row["safe"]) for row in detail),
        "unsafe": sum(not bool(row["safe"]) for row in detail),
        "byTier": dict(sorted(collections.Counter(
            row["tier"] for row in detail).items())),
        "bySource": dict(sorted(collections.Counter(
            row["source"] for row in detail).items())),
        "issueCounts": dict(sorted(counts.items())),
    }


def build_report(baseline_dir: pathlib.Path, candidate_contacts: pathlib.Path,
                 candidate_registry: pathlib.Path, candidate_employments: pathlib.Path,
                 output: pathlib.Path) -> dict:
    paths = {
        "baselineContacts": baseline_dir / "contacts.json.gz",
        "baselineRegistry": baseline_dir / "approved_recipients.json.gz",
        "baselineEmployments": baseline_dir / "advisor_employments.parquet",
        "candidateContacts": candidate_contacts,
        "candidateRegistry": candidate_registry,
        "candidateEmployments": candidate_employments,
    }
    before, after = load_contacts(paths["baselineContacts"]), load_contacts(candidate_contacts)
    old_firms, new_firms = load_firms(paths["baselineEmployments"]), load_firms(candidate_employments)
    contact_diff, contacts = compare_contacts(before, after, old_firms, new_firms)
    baseline_registry = _json(paths["baselineRegistry"])
    current_registry = _json(candidate_registry)
    registry_diff, registry = compare_registries(
        baseline_registry, current_registry)
    added_diff, added_review = review_added_routes(
        baseline_registry, current_registry, after, new_firms)
    advisors_path = candidate_employments.parent / "advisors.parquet"
    aliases_path = candidate_employments.parent / "advisor_other_names.parquet"
    sec_names = load_sec_names(advisors_path, aliases_path)
    reviewed_moves = review_email_moves(
        registry["emailMoves"], before, after, old_firms, new_firms, sec_names)
    nicole_contacts = [r for r in after if r["email"] == NICOLE_EMAIL]
    nicole_routes = [str(crd) for crd, row in
                     (current_registry.get("recipients") or {}).items()
                     if _email(row.get("email")) == NICOLE_EMAIL]
    nicole_ok = (len(nicole_contacts) == 1 and nicole_contacts[0]["crd"] == NICOLE_CRD
                 and relationship(nicole_contacts[0], new_firms) in
                 {"exact_agree", "family_agree"} and nicole_routes == [NICOLE_CRD])
    cross = contacts["candidate"]["firmRelationships"].get("cross_family", 0)
    unexplained = [move for move in reviewed_moves if not move["explained"]]
    summary = {
        "inputs": {key: {"path": str(path), "sha256": _hash(path),
                         "bytes": path.stat().st_size} for key, path in paths.items()},
        "contacts": contacts,
        "outbound": {**{k: v for k, v in registry.items()
                         if k != "emailMoves"},
                     "addedRouteReview": added_review},
        "nicoleSentinel": {"pass": nicole_ok,
                           "contactCrds": [r["crd"] for r in nicole_contacts],
                           "routeCrds": nicole_routes},
        "safety": {"zeroCrossFamilyContacts": cross == 0,
                   "noUnexplainedOutboundMoves": not unexplained,
                   "noUnsafeAddedOutboundRoutes": added_review["unsafe"] == 0,
                   "nicoleCorrect": nicole_ok},
    }
    output.mkdir(parents=True, exist_ok=True)
    contact_diff.to_csv(output / "contact_changes.csv", index=False, encoding="utf-8-sig")
    registry_diff.to_csv(output / "outbound_changes.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(reviewed_moves).to_csv(
        output / "outbound_email_moves.csv", index=False, encoding="utf-8-sig")
    added_diff.to_csv(output / "outbound_added_routes.csv", index=False,
                      encoding="utf-8-sig")
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True),
                                          encoding="utf-8")
    lines = ["# Contact identity comparison", "",
             f"Baseline contacts: {contacts['baseline']['total']:,}",
             f"Candidate contacts: {contacts['candidate']['total']:,}",
             f"Baseline cross-family: {contacts['baseline']['firmRelationships'].get('cross_family', 0):,}",
             f"Candidate cross-family: {cross:,}",
             f"Baseline outbound routes: {registry['baseline']:,}",
             f"Candidate outbound routes: {registry['candidate']:,}",
             f"Explained email-to-CRD corrections: {len(reviewed_moves) - len(unexplained):,}",
             f"Unexplained email-to-CRD moves: {len(unexplained):,}",
             f"New outbound routes reviewed: {added_review['total']:,}",
             f"Unsafe new outbound routes: {added_review['unsafe']:,}",
             f"Nicole sentinel: {'PASS' if nicole_ok else 'FAIL'}", "",
             "## Contact changes", ""]
    lines += [f"- {key}: {value:,}" for key, value in contacts["changes"].items()]
    lines += ["", "## Safety", ""] + [
        f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in summary["safety"].items()]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-dir", type=pathlib.Path, required=True)
    parser.add_argument("--candidate-contacts", type=pathlib.Path,
                        default=ROOT / "webapp/data/contacts.json.gz")
    parser.add_argument("--candidate-registry", type=pathlib.Path,
                        default=ROOT / "data/identity/approved_recipients.json.gz")
    parser.add_argument("--candidate-employments", type=pathlib.Path,
                        default=ROOT / "data/output/advisor_employments.parquet")
    parser.add_argument("--output", type=pathlib.Path,
                        default=ROOT / "data/output/contact_identity_comparison")
    args = parser.parse_args()
    summary = build_report(args.baseline_dir, args.candidate_contacts,
                           args.candidate_registry, args.candidate_employments,
                           args.output)
    print(f"[+] {args.output / 'summary.md'}")
    print(f"[*] safety: {summary['safety']}")
    return 0 if all(summary["safety"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())

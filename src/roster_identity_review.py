"""Keep explicitly held roster CRD changes out of contact authority."""
import csv
import hashlib
import json
from pathlib import Path
import re

from firm_rosters import FIRMS
from identity_normalize import normalize_email

HOLD_REASON = "roster_crd_change_requires_review"
HOLD_FILE = "data/identity/roster_identity_holds.json"
REPORT_COLUMNS = ["source_slug", "source_file", "name", "email", "prior_crds", "proposed_crd", "reason"]


def _key(row):
    return (str(row.get("source_slug") or ""), str(row.get("source_file") or ""),
            str(row.get("name") or "").strip().casefold(), normalize_email(row.get("email")))


def load_holds(root):
    root = Path(root)
    path = root / HOLD_FILE
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Roster identity hold symlink refused")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("holds"), list):
        raise ValueError("Invalid roster identity hold schema")
    result, verified = {}, {}
    for hold in payload["holds"]:
        if not isinstance(hold, dict):
            raise ValueError("Invalid roster identity hold")
        slug, filename = hold.get("source_slug"), hold.get("source_file")
        prior = hold.get("prior_crd")
        digest = hold.get("source_sha256", "")
        if (not isinstance(slug, str) or slug not in FIRMS or not isinstance(filename, str)
                or Path(filename).name != filename or "/" in filename or "\\" in filename
                or not filename.startswith(slug + "_") or not filename.endswith(".csv")
                or not isinstance(prior, str) or not re.fullmatch(r"[1-9][0-9]*", prior)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(hold.get("name"), str) or not hold["name"].strip()
                or not isinstance(hold.get("email"), str)):
            raise ValueError("Invalid roster identity hold evidence")
        if filename not in verified:
            source = root / "data/raw/firm_rosters" / filename
            if not source.is_file() or source.is_symlink():
                raise ValueError("Roster identity hold source missing or unsafe")
            verified[filename] = hashlib.sha256(source.read_bytes()).hexdigest()
        if verified[filename] != digest:
            raise ValueError("Roster identity hold source bytes changed")
        result.setdefault(_key(hold), set()).add(prior)
    return result


def apply_roster_identity_holds(people, root):
    holds = load_holds(root)
    if not holds:
        return people
    out = people.copy()
    email_priors = {}
    for key, prior in holds.items():
        if key[3]:
            email_priors.setdefault(key[3], set()).update(prior)
    report = []
    for idx, row in out.iterrows():
        if (str(row.get("source") or "") in {"CRM", "EIC"}
                or str(row.get("source_slug") or "") not in FIRMS):
            continue
        key = _key(row)
        # Duplicate roster witnesses must not route a disputed address around
        # its held source record. Existing prior owners retain their rules.
        prior = email_priors.get(key[3]) or holds.get(key)
        candidate = str(row.get("advisor_crd") or "")
        if not prior or not candidate or candidate in prior:
            continue
        report.append(dict(source_slug=row.get("source_slug", ""), source_file=row.get("source_file", ""),
                           name=row.get("name", ""), email=row.get("email", ""),
                           prior_crds="|".join(sorted(prior)), proposed_crd=candidate, reason=HOLD_REASON))
        out.at[idx, "proposed_crd"] = candidate
        out.at[idx, "advisor_crd"] = ""
        out.at[idx, "tier"] = "review"
        out.at[idx, "match_reason"] = HOLD_REASON
    out.attrs["roster_identity_review"] = report
    return out


def write_review_report(path, rows):
    def safe(value):
        return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows({key: safe(value) for key, value in row.items()} for row in rows)

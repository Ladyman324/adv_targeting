"""Address evidence for approved ACT identities; never an identity matcher."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import re

from identity_normalize import normalize_crd, normalize_email

from geography_integrity import USPS_REGIONS

IDENTITY_FIELDS = ("firstName", "middleName", "lastName", "emailAddress")
ADDRESS_FIELDS = ("line1", "line2", "line3", "city", "state", "postalCode")


def text(value):
    return " ".join(str(value or "").split())


def address(value):
    value = value or {}
    result = {key: text(value.get(key)) for key in ADDRESS_FIELDS}
    result["state"] = result["state"].upper()
    return result


def usable(value):
    row = address(value)
    return bool(row["line1"] and row["city"] and row["state"] in USPS_REGIONS
                and re.fullmatch(r"\d{5}(?:-\d{4})?", row["postalCode"]))


def from_roster(row):
    # The normalized loader preserves suites separately from the street.
    from contact_work_locations import clean_street
    city, state, postal = (text(row.get(k)) for k in ("city", "state", "location_zip"))
    return address({"line1": clean_street(row.get("location_street"), city, state, postal),
                    "line2": row.get("location_street2"), "line3": row.get("location_street3"),
                    "city": city, "state": state, "postalCode": postal})


def build_address_reviews(mapping, contacts, act_rows, roster_rows, employment):
    """Only ledger-approved, one-to-one ACT links and person-specific offices."""
    ids = Counter(str(row.get("id") or "").lower() for row in act_rows)
    by_id = {str(row.get("id") or "").lower(): row for row in act_rows}
    links = Counter(str(value).lower() for value in mapping.values())
    email_crds = defaultdict(set)
    names = defaultdict(set)
    by_email = defaultdict(list)
    for crd, contact in contacts.items():
        email_crds[text(contact.get("e")).lower()].add(str(crd))
    for row in roster_rows:
        email, firm = text(row.get("email")).lower(), text(row.get("firm_crd"))
        names[(email, firm)].add(text(row.get("name")).lower())
        by_email[email].append(row)
    result = {}
    for crd, contact_id in mapping.items():
        key = str(contact_id).lower()
        raw, contact = by_id.get(key), contacts.get(str(crd), {})
        if not raw or raw.get("isPrivate") or raw.get("isActive") is False or ids[key] != 1 or links[key] != 1:
            continue
        if not text(raw.get("lastName")) or (contact.get("aid") and text(contact["aid"]).lower() != key):
            continue
        email, firm = text(contact.get("e")).lower(), text(contact.get("fc"))
        options = {}
        # A roster alternative is actionable only when the selected contact
        # metadata ties that email to the same approved ACT record.
        if (str(contact.get("aid") or "").lower() == key and contact.get("t") == "confirmed"
                and email and len(email_crds[email]) == 1 and (str(crd), firm) in employment):
            for row in by_email[email]:
                if text(row.get("firm_crd")) != firm or len(names[(email, firm)]) != 1:
                    continue
                office = from_roster(row)
                if not usable(office):
                    continue
                source_file = text(row.get("source_file"))
                candidate_id = hashlib.sha256(json.dumps([office, source_file], sort_keys=True).encode()).hexdigest()[:24]
                options[candidate_id] = {"id": candidate_id, "address": office,
                    "source": text(row.get("company") or row.get("source") or "Firm roster"),
                    "sourceFile": source_file,
                    "url": text(row.get("profile_url") or row.get("team_url"))}
        result[str(crd)] = {"contactId": str(contact_id),
            "identity": {k: text(raw.get(k)) for k in IDENTITY_FIELDS},
            "primary": address(raw.get("businessAddress")),
            "candidates": list(options.values())}
    return result


def build_crm_directory(mapping, rows, zips):
    """Project shared, active CRM contacts separately from the advisor universe."""
    from contact_work_locations import source_record
    linked = {text(value).lower() for value in mapping.values()}
    counts = Counter(text(row.get("id")).lower() for row in rows)
    contacts, claimed, email_hashes = [], set(), set()
    for row in rows:
        custom = row.get("customFields") or {}
        crd = normalize_crd(custom.get("crd") or custom.get("sec_crd"))
        if crd:
            claimed.add(crd)
        for field in ("emailAddress", "altEmailAddress", "personalEmailAddress"):
            email = normalize_email(row.get(field))
            if email:
                email_hashes.add(hashlib.sha256(email.encode()).hexdigest())
        key = text(row.get("id")).lower()
        primary = normalize_email(row.get("emailAddress"))
        name = text(row.get("fullName")) or text(f"{row.get('firstName') or ''} {row.get('lastName') or ''}")
        if not (key and counts[key] == 1 and primary and name) or key in linked:
            continue
        if row.get("isPrivate") or row.get("isUser") or row.get("isActive") is False:
            continue
        raw = row.get("businessAddress") or {}
        office = address(raw)
        point = source_record({"city": office["city"], "state": office["state"],
            "location_street": office["line1"], "location_zip": office["postalCode"],
            "location_lat": raw.get("latitude"), "location_lon": raw.get("longitude")}, "ACT", zips)
        coords = [point["lat"], point["lon"]] if point and all(
            math.isfinite(point[k]) for k in ("lat", "lon")) else None
        contacts.append({"name": name, "email": primary, "company": text(row.get("company")),
                         "primary": office, "coordinates": coords})
    contacts.sort(key=lambda row: (row["name"].casefold(), row["email"]))
    return {"contacts": contacts, "claimedCrds": sorted(claimed), "emailHashes": sorted(email_hashes)}

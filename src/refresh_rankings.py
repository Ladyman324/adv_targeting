"""Merge immutable ranking editions without deleting historical recognition.

No acquisition and no changes outside the explicitly supplied staging root.
Legacy Forbes snapshots without an edition retain unknown (null) years.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import hashlib
import re
from urllib.parse import urlsplit


def reuse_barrons_identifiers(records, previous):
    """Retain a durable prior ranking ID only for an exact unchanged record.

    This is not person matching or email authorization. Broken publisher links
    must not erase an established ranking association, but a different name,
    firm, location, list/state, conflicting CRD or future edition prevents reuse.
    Raw captures are never modified; provenance travels with resolved rows.
    """
    def url(value):
        try:
            parsed=urlsplit(str(value or ''))
            if parsed.scheme not in {'http','https'} or parsed.netloc.lower() not in {'www.barrons.com','barrons.com'}:
                return ''
            return parsed.path.rstrip('/') if re.fullmatch(r'/advisor/finder/[A-Za-z0-9_-]+/?',parsed.path) else ''
        except ValueError:return ''
    def text(value):return ' '.join(str(value or '').split()).casefold()
    def key(row):return (url(row.get('barrons_url')),*(text(row.get(k)) for k in ('advisor','firm','location','ranking','state')))
    by_url={};by_record={}
    for row in previous:
        crd=str(row.get('crd') or '').strip();person_url=url(row.get('barrons_url'))
        if not person_url or not re.fullmatch(r'[1-9]\d*',crd):continue
        by_url.setdefault(person_url,set()).add(crd)
        by_record.setdefault(key(row),[]).append(row)
    out=[];audit=[]
    for original in records:
        row=dict(original);k=key(row)
        if not row.get('crd') and k[0] and all(k[1:5]) and len(by_url.get(k[0],set()))==1:
            year=row.get('year')
            candidates=[p for p in by_record.get(k,[]) if isinstance(year,int) and not isinstance(year,bool)
                and isinstance(p.get('year'),int) and not isinstance(p['year'],bool) and p['year']<=year]
            if candidates:
                prior=max(candidates,key=lambda p:p['year'])
                proof=hashlib.sha256(json.dumps(prior,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
                row.update(crd=str(prior['crd']).strip(),crd_origin='prior_exact_publisher_record',crd_prior_record_sha256=proof)
                audit.append({'advisor':row['advisor'],'crd':row['crd'],'ranking':row['ranking'],'state':row.get('state'),
                    'year':year,'priorYear':prior['year'],'barrons_url':row['barrons_url'],'priorRecordSha256':proof})
        out.append(row)
    return out,audit


def merge_rankings(stage_root, provider, edition_files, include_existing=True):
    """Merge paths or {path, year/edition, categories} descriptors into stage.

    Categories filter Barron's ranking names or Forbes list names. Conflicting
    explicit edition metadata raises rather than silently relabeling records.
    Later input corrects the same edition/person/category, never erases another.
    """
    if provider not in ("barrons", "forbes"):
        raise ValueError("Ranking provider must be barrons or forbes")
    root = pathlib.Path(stage_root)
    raw = root / "data/raw" / f"{provider}_rankings.json"
    sources = ([raw] if include_existing and raw.exists() else []) + list(edition_files)
    merged, input_rows = {}, 0
    for descriptor in sources:
        options = descriptor if isinstance(descriptor, dict) else {"path": descriptor}
        path = pathlib.Path(options["path"])
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Ranking snapshot must be a nonempty record list: {path.name}")
        edition = options.get("year", options.get("edition"))
        if edition is not None:
            if isinstance(edition, bool) or not str(edition).isdigit() or not 1900 <= int(edition) <= 2200:
                raise ValueError("Ranking edition must be a four-digit publication year")
            edition = int(edition)
        requested = set(options.get("categories") or [])
        encountered = set()
        for original in rows:
            if not isinstance(original, dict):
                raise ValueError("Ranking records must be objects")
            row = dict(original)
            category = row.get("ranking" if provider == "barrons" else "list")
            if not category:
                raise ValueError("Ranking record lacks list/category")
            if requested and category not in requested:
                continue
            encountered.add(category)
            year = row.get("year")
            if year is not None:
                if isinstance(year, bool) or not str(year).isdigit() or not 1900 <= int(year) <= 2200:
                    raise ValueError("Invalid ranking record year")
                year = int(year)
            if edition is not None and year is not None and year != edition:
                raise ValueError("Ranking record year disagrees with explicit edition")
            row["year"] = edition if edition is not None else year
            identity = row.get("crd") or row.get("barrons_url") or row.get("advisor") if provider == "barrons" else row.get("uri") or row.get("name")
            if not identity or not row.get("advisor" if provider == "barrons" else "name"):
                raise ValueError("Ranking record lacks person identity/name")
            scope = row.get("state") if provider == "barrons" else row.get("category")
            key = (category, str(scope or ""), row["year"], str(identity))
            merged[key] = row
            input_rows += 1
        if requested - encountered:
            raise ValueError("Requested ranking categories absent: " + ", ".join(sorted(requested - encountered)))
    if not merged:
        raise FileNotFoundError(f"No {provider} ranking snapshot supplied or staged")
    records = [merged[key] for key in sorted(merged, key=lambda key: tuple(str(v) for v in key))]
    # Parse before touching staged destinations so invalid input cannot leave
    # a half-updated JSON/parquet pair.
    import tempfile
    from parse_barrons import load as load_barrons
    from parse_forbes import load as load_forbes
    raw.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=raw.parent) as temp:
        pending = pathlib.Path(temp) / "rankings.json"
        pending.write_text(json.dumps(records, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        table = (load_barrons if provider == "barrons" else load_forbes)(pending)
        pending_parquet = pathlib.Path(temp) / "rankings.parquet"
        table.to_parquet(pending_parquet, index=False)
        target = root / "data/interim" / f"{provider}_rankings.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        pending_parquet.replace(target)
        pending.replace(raw)
    return {"provider": provider, "rows": len(records), "duplicates": input_rows - len(records),
            "years": sorted({row["year"] for row in records if row["year"] is not None}),
            "unknownYear": sum(row["year"] is None for row in records),
            "categories": sorted({row.get("ranking" if provider == "barrons" else "list") for row in records})}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--provider", required=True, choices=["barrons", "forbes"])
    parser.add_argument("--edition", type=int)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--immutable-only", action="store_true", help="Use pinned edition files only")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    print(merge_rankings(args.root, args.provider, [
        {"path": path, "edition": args.edition, "categories": args.category} for path in args.files], include_existing=not args.immutable_only))

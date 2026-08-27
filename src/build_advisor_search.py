"""National advisor search for the desk view, as prefix-sharded static files.

THE PROBLEM. advisor_index.json is 21.7 MB (6.9 MB gzipped) over 412,567
advisors, and the desk app will not start fetching it until the search box takes
focus -- deliberately, because it is far too large to sit on the first-paint
path. So the first search of a session waits for the whole file. On a phone on
cellular that is five to fifteen seconds of a box that looks broken.

Every other facet of that search box is already small and already instant:

    ZIP / city / state   geo_index.json   298 KB gzipped, loaded early
    firm                 NAT.firms        arrives with national detail
    advisor name / CRD   advisor_index    6.9 MB gzipped, on focus

Only the facet with 412,567 keys pays, which is the one worth sharding.

HOW. The same scheme src/build_name_index.py uses for the field app, which has
been in production and is the reason this is a port rather than an invention:
shard on the first two letters of ANY name token, split the heavy prefixes to
three letters, and expand given names through NICKNAMES at build time so the
client stays a plain substring match with no name logic in it. Indexing
surnames alone would mean typing "john" never finds John Smith.

That expansion also makes desk search BETTER, not merely faster: today's
`row[1].toLowerCase().includes(q)` cannot get from "Bill" to William Kaiser.

CRDs shard too, on their first three digits. This changes CRD matching from
substring to prefix -- typing 2525 stops matching 12525099. Nobody searches the
middle of a registration number, and the whole point is to avoid holding all
412,567 of them in memory to find out.

WHAT THIS DOES NOT REPLACE. advisor_index.json also answers advisorRow(crd) for
card rendering, the list view, owner lookup and history -- four of its five
uses, none of them search. Those still read the full file, which now loads in
the background instead of blocking the first keystroke. Retiring it entirely
means denormalising two rarely-populated columns (filed-as name, and the places
array present on 1,208 of 412,567 rows) into the records those call sites
already hold. That is a separate change.

Run:  python src/build_advisor_search.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from build_name_index import tokens_for
from web_assets import write_json_gz

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp" / "data"
SEARCH = WEB / "search"

PREFIX = 2
# Above this a two-letter shard is split into three-letter children. The field
# app splits 36 of 437 at 2,500; this file is 3.4x larger, so more prefixes
# cross the line -- ma, ja, jo, br, da each carry over 20,000 advisors, where
# two letters separate almost nobody.
SPLIT_ABOVE = 2500
# Where splitting stops. Past four letters a rep is typing most of a name
# before anything appears, and the remaining shards are surnames so common
# (Smith, Jones) that no prefix separates them -- those stay large on purpose.
MAX_PREFIX = 4
CRD_PREFIX = 3

# Exactly the columns renderNationalSearch() reads, in the order it reads them,
# so a shard row is substitutable for an advisor_index row with no translation:
#   crd, name, firm index, state, city index, filed states, filed-as name
# plus `alt`, the nickname tokens.
#
# `alt` is not cosmetic. William Kaiser lands in the "bi" shard because "bill"
# is one of his tokens; without shipping that token the client would substring
# match "bill" against "William Kaiser", fail, and throw him back out -- so
# searching "bill" would return Billups and Billeter and no Williams at all.
# Only tokens the client could NOT derive from the displayed name are shipped,
# so the ~60% of people with no nickname form cost nothing.
COLUMNS = ["crd", "name", "firm", "state", "city", "states", "filed", "alt"]


def surplus_tokens(name: str, filed: str, toks: set) -> str:
    """The tokens a client could not find by substring on what it displays."""
    plain = f"{name} {filed}".lower()
    return " ".join(sorted(t for t in toks if t not in plain))


def main() -> None:
    index = json.loads((WEB / "advisor_index.json").read_text(encoding="utf-8"))
    rows = index["advisors"]

    shards: dict = collections.defaultdict(dict)     # prefix -> {crd: record}
    crd_shards: dict = collections.defaultdict(list)
    for r in rows:
        crd, name = str(r[0]), r[1]
        filed = r[6] if len(r) > 6 and r[6] else ""
        toks = tokens_for(name) | (tokens_for(filed) if filed else set())
        rec = [crd, name, r[2], r[3], r[4], r[5], filed,
               surplus_tokens(name, filed, toks)]
        for tok in toks:
            # Deduped per shard by CRD: someone whose given name and surname
            # share a prefix belongs in it once, not twice.
            shards[tok[:PREFIX]][crd] = rec
        crd_shards[crd[:CRD_PREFIX]].append(rec)

    # Split the heavy prefixes, REPEATEDLY. Rebuilt from each record's OWN
    # tokens rather than from the parent bucket, because a record belongs in a
    # child only if one of its tokens starts that way -- reusing the parent
    # bucket would file "Smith, Jane" under "jam".
    #
    # One pass is not enough at this scale. The field app splits once because
    # its 121,136 records leave no three-letter child oversized; over 412,567
    # they do -- "smi" alone holds 16,978 people, a 1.1 MB shard, which is the
    # problem this file exists to remove rather than relocate. So it repeats
    # until every shard is under the threshold or the prefix reaches MAX_PREFIX,
    # where "keep typing" stops being reasonable advice.
    split = set()
    depth = PREFIX
    while depth < MAX_PREFIX:
        heavy = [p for p, byname in shards.items()
                 if len(byname) > SPLIT_ABOVE and len(p) == depth]
        if not heavy:
            break
        for prefix in heavy:
            byname = shards.pop(prefix)
            split.add(prefix)
            children: dict = collections.defaultdict(dict)
            for crd, rec in byname.items():
                toks = tokens_for(rec[1]) | (tokens_for(rec[6]) if rec[6] else set())
                for tok in toks:
                    if tok.startswith(prefix) and len(tok) > depth:
                        children[tok[:depth + 1]][crd] = rec
                else:
                    # A record whose only matching token IS the prefix, exactly
                    # -- a surname of two or three letters, "Ho", "Ng", "Lee".
                    # It has no child to fall into and would vanish silently.
                    if not any(t.startswith(prefix) and len(t) > depth for t in toks):
                        children[prefix + "."][crd] = rec
            for key, kids in children.items():
                shards[key].update(kids)
        depth += 1

    if SEARCH.exists():
        # Rebuilt wholesale. A shard left over from a previous run would keep
        # returning advisors who have since left the file entirely.
        shutil.rmtree(SEARCH)
    (SEARCH / "crd").mkdir(parents=True, exist_ok=True)

    sizes = []
    for prefix, byname in shards.items():
        out = sorted(byname.values(), key=lambda x: x[1])
        path = SEARCH / f"{prefix}.json"
        path.write_text(json.dumps({"prefix": prefix, "columns": COLUMNS,
                                    "rows": out}, separators=(",", ":")),
                        encoding="utf-8")
        sizes.append((len(out), path.stat().st_size))

    crd_sizes = []
    for prefix, recs in crd_shards.items():
        out = sorted(recs, key=lambda x: x[0])
        path = SEARCH / "crd" / f"{prefix}.json"
        path.write_text(json.dumps({"prefix": prefix, "columns": COLUMNS,
                                    "rows": out}, separators=(",", ":")),
                        encoding="utf-8")
        crd_sizes.append((len(out), path.stat().st_size))

    # firms and cities are the dictionaries the result rows index into, and at
    # 396 KB raw they are small enough to ship whole -- the same call
    # geo_index.json already makes for ZIPs and cities. Without them a shard
    # row cannot name the advisor's firm or city.
    write_json_gz(WEB / "advisor_search.json",
                  {"prefix": PREFIX, "crdPrefix": CRD_PREFIX,
                   "columns": COLUMNS,
                   "shards": sorted(shards),
                   # Prefixes needing a third character, so the client can say
                   # "keep typing" rather than returning nothing.
                   "split": sorted(split),
                   "crdShards": sorted(crd_shards),
                   "firms": index["firms"], "cities": index["cities"],
                   "advisors": len(rows)},
                  separators=(",", ":"))

    counts = sorted(n for n, _ in sizes)
    total = sum(b for _, b in sizes) + sum(b for _, b in crd_sizes)
    n = len(counts)
    manifest = (WEB / "advisor_search.json").stat().st_size
    print(f"[*] {n:,} name shards + {len(crd_sizes):,} CRD shards, "
          f"{len(rows):,} advisors, {total / 1e6:.1f} MB total")
    print(f"    records per name shard: median {counts[n // 2]:,}, "
          f"p95 {counts[int(n * 0.95)]:,}, max {counts[-1]:,}")
    biggest = max(sizes, key=lambda x: x[1])
    print(f"    largest name shard {biggest[1] / 1024:,.0f} KB raw "
          f"(~{biggest[1] / 1024 / 3.5:,.0f} KB gzipped)")
    big_crd = max(crd_sizes, key=lambda x: x[1])
    print(f"    largest CRD shard  {big_crd[1] / 1024:,.0f} KB raw")
    print(f"    {len(split)} prefixes split to 3 letters: {', '.join(sorted(split))}")
    print(f"    manifest advisor_search.json {manifest / 1024:,.0f} KB "
          f"(firms + cities dictionaries) -- this is the only up-front cost, "
          f"against 6.9 MB gzipped today")


if __name__ == "__main__":
    main()

"""National name search for the field view, as prefix-sharded static files.

THE PROBLEM
-----------
The field view could only search advisors it had already loaded. A rep standing
in Atlanta, about to meet someone from Chicago, could not find them -- and the
status line had to admit it. The obvious fix is a server endpoint, and the
obvious fix is wrong for this feature: managed Functions run on consumption and
cold-start in seconds, which is precisely the wrong latency for search-as-you-
type. A file from a CDN edge is always warm.

THE SHAPE
---------
Shard by the first two letters of any name token. Measured across the 121,136
searchable advisors:

    prefix length    shards    median records    largest shard
    1                26        4,610             554 KB raw / 158 KB gz
    2                393       37                160 KB raw /  46 KB gz
    3                3,616     6                  69 KB raw /  20 KB gz

Two letters is the sweet spot: type "sm", fetch one file, filter locally as the
rest is typed. The worst case in the country is smaller than half a single
near-me fetch. A single national index would be 5.5 MB raw / 1.6 MB gzipped --
workable as a one-time download, but iOS evicts caches after a few weeks of
non-use, so reps would periodically pay it again.

EVERY NAME TOKEN, AND NICKNAMES
-------------------------------
Indexing surnames alone means typing "john" never finds John Smith, which is
not how anyone searches. Every token is indexed, and given names are expanded
through the same NICKNAMES table that fixed the Edward Jones email selection --
so "Bill" finds William Kaiser. The expansion happens HERE, at build time, so
the client stays a substring match with no name logic in it.

WHAT CANNOT BE FOUND
--------------------
Advisors with contact detail but no mapped office. A record carries its tile
cell so tapping a result can open the full card, and without a location there
is no cell. The count is written into the manifest so the UI can say so rather
than letting a rep conclude the person is not in the system.

Run:  python src/build_name_index.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from forbes_match import NICKNAMES
from web_assets import write_json_gz

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp" / "data"
TILES = WEB / "tiles"
NAMES = WEB / "names"

PREFIX = 2
# Above this, a two-letter shard is split into three-letter children. The heavy
# prefixes are all common GIVEN names -- ja (James/Jason/Jack), jo (John/
# Joseph), ma, da, mi -- where two letters separate almost nobody. 28 of 437
# shards carried more than 150 KB; splitting them costs a third keystroke on
# those prefixes only, and leaves every other search a single small file.
SPLIT_ABOVE = 2500
# `alt` carries only the tokens a client could NOT derive from `name` -- in
# practice the nickname expansions. Without it the index was subtly broken:
# William Kaiser is filed in the "bi" shard because "bill" is one of his
# tokens, but the client then filtered by substring on the displayed name and
# threw him straight back out. Searching "bill" returned Billups and Billeter
# and no Williams at all. Only the surplus is shipped, so the ~60% of people
# with no nickname form cost nothing.
COLUMNS = ["name", "crd", "cell", "city", "state", "alt"]

# Tokens that are not a name and would pull thousands of unrelated people into
# a shard. "jr" and "ii" are the ones that actually appear.
SKIP = {"jr", "sr", "ii", "iii", "iv", "and", "the", "of", "de", "la"}


def tokens_for(name: str) -> set:
    """Every string a human might type to find this person."""
    raw = [re.sub(r"[^a-z]", "", t.lower()) for t in str(name or "").split()]
    out = {t for t in raw if len(t) >= 2 and t not in SKIP}
    # Nicknames both ways: "Bill" should find William Kaiser, and someone filed
    # as Bill should be found by typing William.
    for t in list(out):
        out |= {n for n in NICKNAMES.get(t, set()) if len(n) >= 2}
    return out


def main() -> None:
    index = json.loads((WEB / "tile_index.json").read_text(encoding="utf-8"))
    C = {c: i for i, c in enumerate(index["columns"])}

    shards: dict = collections.defaultdict(dict)   # prefix -> {crd: record}
    seen = 0
    for path in TILES.glob("*.json"):
        tile = json.loads(path.read_text(encoding="utf-8"))
        cell = tile["cell"]
        for r in tile["rows"]:
            seen += 1
            name = r[C["name"]]
            toks = tokens_for(name)
            plain = name.lower()
            alt = sorted(tok for tok in toks if tok not in plain)
            rec = [name, r[C["crd"]], cell, r[C["city"]], r[C["state"]],
                   " ".join(alt)]
            for tok in toks:
                # Deduped per shard by CRD: a person whose given name and
                # surname share a prefix should appear once, not twice.
                shards[tok[:PREFIX]][r[C["crd"]]] = rec

    # Split the heavy prefixes. Rebuilt from the token lists rather than from
    # the two-letter buckets, because a record belongs in a three-letter child
    # only if one of ITS OWN tokens starts that way -- reusing the parent
    # bucket would file "Smith, Jane" under "jam".
    split = set()
    for prefix, byname in list(shards.items()):
        if len(byname) <= SPLIT_ABOVE:
            continue
        split.add(prefix)
        children: dict = collections.defaultdict(dict)
        for crd, rec in byname.items():
            for tok in tokens_for(rec[0]):
                if tok.startswith(prefix) and len(tok) >= 3:
                    children[tok[:3]][crd] = rec
        del shards[prefix]
        for key, rows in children.items():
            shards[key].update(rows)

    if NAMES.exists():
        # Rebuilt wholesale -- a shard left from a previous run would keep
        # returning advisors who have since moved cell or left the file.
        shutil.rmtree(NAMES)
    NAMES.mkdir(parents=True, exist_ok=True)

    sizes = []
    for prefix, byname in shards.items():
        rows = sorted(byname.values(), key=lambda x: x[0])
        (NAMES / f"{prefix}.json").write_text(
            json.dumps({"prefix": prefix, "columns": COLUMNS, "rows": rows},
                       separators=(",", ":")), encoding="utf-8")
        sizes.append((len(rows), (NAMES / f"{prefix}.json").stat().st_size))

    # How many advisors have contact detail but no mapped office, and so cannot
    # be indexed at all. Carried to the client so the gap is stated rather than
    # looking like the person is missing from the system.
    contacts = json.loads((WEB / "contacts.json").read_text(encoding="utf-8"))
    unplaced = len(contacts["advisors"]) - seen

    write_json_gz(WEB / "name_index.json",
                  {"prefix": PREFIX, "columns": COLUMNS,
                   "shards": sorted(shards),
                   # Prefixes that need a third character. The client uses this
                   # to say "keep typing" instead of returning nothing.
                   "split": sorted(split),
                   "searchable": seen, "unplaced": unplaced},
                  separators=(",", ":"))

    counts = sorted(n for n, _ in sizes)
    total = sum(b for _, b in sizes)
    n = len(counts)
    print(f"[*] {n:,} shards, {seen:,} searchable advisors, {total / 1e6:.1f} MB total")
    print(f"    records per shard: median {counts[n // 2]:,}, "
          f"p95 {counts[int(n * 0.95)]:,}, max {counts[-1]:,}")
    biggest = max(sizes, key=lambda x: x[1])
    print(f"    largest shard {biggest[1] / 1024:,.0f} KB raw "
          f"(~{biggest[1] / 1024 / 3.5:,.0f} KB gzipped)")
    print(f"    {len(split)} prefixes split to 3 letters: {', '.join(sorted(split))}")
    print(f"    {unplaced:,} advisors have contact detail but no mapped office "
          f"and cannot be searched")


if __name__ == "__main__":
    main()

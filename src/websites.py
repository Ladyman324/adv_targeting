"""Extract every Item 1.I website address per firm from the firm XML feed.

Item 1.I asks firms to list "website addresses, including addresses for accounts
on publicly available social media platforms" -- so the roster CSV's single
"Website Address" column is one of several, and for 32.7% of firms it is a
LinkedIn or X profile rather than the corporate site. The XML feed carries the
full list, which lets us pick a real domain when one exists.
"""
from __future__ import annotations

import gzip
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import config

ROOT = pathlib.Path(__file__).parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"

SOCIAL = re.compile(
    r"(linkedin\.|twitter\.|//x\.com|facebook\.|instagram\.|youtube\.|tiktok\.)", re.I)


def parse() -> pd.DataFrame:
    src = config.newest_feed(RAW, "IA_FIRM_SEC_Feed_*.xml.gz")
    rows = []
    for _, el in ET.iterparse(gzip.open(src, "rb"), events=("end",)):
        if el.tag != "Firm":
            continue
        info = el.find("./Info")
        if info is not None:
            crd = info.get("FirmCrdNb")
            for w in el.findall("./FormInfo/Part1A/Item1/WebAddrs/WebAddr"):
                url = (w.text or "").strip()
                if url:
                    rows.append({"firm_crd": crd, "url": url,
                                 "is_social": bool(SOCIAL.search(url))})
        el.clear()
    return pd.DataFrame(rows)


def main() -> None:
    w = parse().drop_duplicates()
    w.to_parquet(INTERIM / "firm_websites.parquet", index=False)

    # Prefer a non-social address; fall back to whatever exists.
    w = w.sort_values(["firm_crd", "is_social"])
    primary = (w.groupby("firm_crd")
                 .agg(website_primary=("url", "first"),
                      n_websites=("url", "size"),
                      n_social=("is_social", "sum"))
                 .reset_index())
    primary["website_is_social"] = primary["website_primary"].str.contains(
        SOCIAL, na=False)
    primary.to_parquet(INTERIM / "firm_website_primary.parquet", index=False)

    print(f"website rows        {len(w):>9,}")
    print(f"firms with any URL  {primary['firm_crd'].nunique():>9,}")
    print(f"firms w/ >1 URL     {(primary.n_websites > 1).sum():>9,}")
    print(f"primary still social{primary.website_is_social.sum():>9,} "
          f"({primary.website_is_social.mean():.1%}) -- these firms listed only social accounts")


if __name__ == "__main__":
    main()

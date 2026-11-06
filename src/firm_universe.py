"""Current registered firms must not depend on an older financial snapshot."""
from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET

import pandas as pd


def supplement_registered_firms(scored, feed, employments):
    """Add SEC-approved employers absent from ADV, without fabricating scores.

    Only the pinned SEC firm compilation grants membership. A name in ACT,
    an individual employment, or an exempt-reporting firm is not sufficient.
    Existing ADV rows retain their original facts and classifications.
    """
    out = scored.copy()
    out["crd"] = out.crd.astype(str)
    out["adv_financials_available"] = True
    out["firm_universe_source"] = "adv"
    existing = set(out.crd)
    counts = employments.assign(firm_crd=employments.firm_crd.astype(str)).groupby("firm_crd").advisor_crd.nunique()
    additions, seen = [], set()
    with gzip.open(feed, "rb") as handle:
        for _, firm in ET.iterparse(handle, events=("end",)):
            if firm.tag != "Firm":
                continue
            info, registration, address = firm.find("Info"), firm.find("Rgstn"), firm.find("MainAddr")
            if info is None:
                raise ValueError("SEC firm lacks Info")
            crd = info.get("FirmCrdNb", "")
            if not re.fullmatch(r"[1-9]\d*", crd) or crd in seen:
                raise ValueError("SEC firm CRD is invalid or duplicated")
            seen.add(crd)
            if (crd not in existing and crd in counts and registration is not None
                    and registration.get("FirmType", "").upper() == "REGISTERED"
                    and registration.get("St", "").upper() == "APPROVED"):
                name = info.get("BusNm") or info.get("LegalNm")
                if not name:
                    raise ValueError("Approved SEC firm lacks a name")
                row = {column: None for column in out.columns}
                # Unknown financial quantities remain null. Boolean targeting
                # signals default false, explicitly qualified by missing ADV.
                for column in out.select_dtypes(include="bool").columns:
                    row[column] = False
                row.update(crd=crd, name=name, legal_name=info.get("LegalNm") or name,
                    firm_display=name, status="APPROVED", firm_type="insufficient_data",
                    motion="low_fit", is_target=False, review_reason="missing_adv_financials",
                    n_advisors=int(counts[crd]), adv_financials_available=False,
                    firm_universe_source="sec_registered_employer", sales_channel="territory")
                if address is not None:
                    row.update(city=address.get("City"), state=address.get("State"),
                        postal=address.get("PostlCd"), phone=address.get("PhNb"))
                additions.append(row)
            firm.clear()
    if additions:
        out = pd.concat([out, pd.DataFrame(additions)], ignore_index=True)
        duplicate = out.name.duplicated(keep=False)
        out.loc[duplicate, "firm_display"] = (out.loc[duplicate, "name"] + " ("
            + out.loc[duplicate, "state"].fillna("--") + ", CRD " + out.loc[duplicate, "crd"] + ")")
    return out

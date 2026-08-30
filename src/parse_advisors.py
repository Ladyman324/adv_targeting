"""Parse the IA_INDVL compilation feed into advisor tables.

Produces two tables, because an adviser representative can be registered with
more than one firm -- a rep is not a child of a single firm:

  advisors      one row per individual (indvlPK)
  employments   one row per (individual, current employer) pair -- the bridge

Join key to the firm roster is CrntEmp/@orgPK == "Organization CRD#".
"""
from __future__ import annotations

import pathlib
import sys
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import config
from sec_names import OTHER_NAME_COLUMNS, build_nickname_evidence

ROOT = pathlib.Path(__file__).parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"


def _designations(ind) -> str:
    d = [x.get("dsgntnNm", "") for x in ind.findall("./Dsgntns/Dsgntn")]
    return "|".join(sorted(filter(None, d)))


# Names declared on Form U4 beyond the filed legal name. 65% of individuals
# carry at least one, and this is where "goes by" lives: Edison Tate Lambeth
# (CRD 4237043) files <OthrNm lastNm="LAMBETH" midNm="TATE"/>, and Thomas Mahone
# Tolleson (CRD 1452953) files <OthrNm lastNm="Tolleson" firstNm="Tom"/>. The
# list is unranked and noisy -- it also holds bare initials ("B" for MANNIX),
# maiden names and near-duplicates -- so capture it raw and interpret it in
# _used_first rather than trusting it wholesale.
def _other_names(ind) -> list:
    return [{k: (nm.get(k) or "").strip()
             for k in ("firstNm", "midNm", "lastNm", "sufNm")}
            for nm in ind.findall("./OthrNms/OthrNm")]


def _norm(value) -> str:
    return str(value or "").strip().upper().rstrip(".")


def _used_first(info, others: list) -> str:
    """The first name this person goes by, or "" to keep the filed one.

    Two conservative rules, both requiring the surname to match so a maiden name
    or a mis-keyed row cannot leak in:

      * an entry giving only a middle name -> they go by that middle name
        (Lambeth files no firstNm and midNm=TATE).
      * an entry giving a different first name -> that is the used name
        (Tolleson files firstNm=Tom against a filed THOMAS).

    Single letters are skipped, or William Mannix -- who files "B", "BILL" and
    "WILLIAM" -- would resolve to "B".
    """
    filed_first, filed_mid = _norm(info.get("firstNm")), _norm(info.get("midNm"))
    filed_last = _norm(info.get("lastNm"))
    for other in others:
        if _norm(other["lastNm"]) != filed_last:
            continue
        first, mid = _norm(other["firstNm"]), _norm(other["midNm"])
        if not first and mid and mid == filed_mid and mid != filed_first:
            return other["midNm"]
        if first and first != filed_first and len(first) > 1:
            return other["firstNm"]
    return ""


def _drp_flags(ind) -> dict:
    """Disclosure reporting page flags, OR'd across all DRPs for the person."""
    keys = ["hasCriminal", "hasRegAction", "hasCivilJudc", "hasCustComp",
            "hasTermination", "hasJudgment", "hasBankrupt", "hasBond",
            "hasInvstgn"]
    out = {k: False for k in keys}
    for drp in ind.findall("./DRPs/DRP"):
        for k in keys:
            if str(drp.get(k, "")).strip().upper() in ("Y", "YES", "TRUE"):
                out[k] = True
    return out


def parse(zip_path: pathlib.Path, *, include_other_names: bool = False):
    """Parse the feed, optionally returning normalized ``OthrNm`` rows.

    The default five-frame result remains compatible with older callers. The
    production build opts in to a sixth frame for the complete SEC name forms.
    """
    advisors, employments, emp_hist, exams, othr = [], [], [], [], []
    other_names = []
    z = zipfile.ZipFile(zip_path)
    members = sorted(z.namelist())

    for mi, member in enumerate(members, 1):
        with z.open(member) as f:
            for ev, el in ET.iterparse(f, events=("end",)):
                if el.tag != "Indvl":
                    continue
                info = el.find("./Info")
                if info is None:
                    el.clear(); continue
                pk = info.get("indvlPK")

                names = _other_names(el)
                rec = {
                    "advisor_crd": pk,
                    "iapd_url": info.get("link"),
                    "first_name": info.get("firstNm"),
                    "middle_name": info.get("midNm"),
                    "last_name": info.get("lastNm"),
                    "suffix": info.get("sufNm"),
                    # "" when the filed first name is the one they use
                    "used_first_name": _used_first(info, names),
                    "active_ag_reg": info.get("actvAGReg"),
                    "designations": _designations(el),
                    "n_exams": len(el.findall("./Exms/Exm")),
                    "n_prior_firms": len(el.findall("./PrevRgstns/PrevRgstn")),
                    "n_other_business": len(el.findall("./OthrBuss/OthrBus")),
                }
                rec.update(_drp_flags(el))
                advisors.append(rec)
                for ordinal, name in enumerate(names, 1):
                    other_names.append({
                        "advisor_crd": pk,
                        "alias_ordinal": ordinal,
                        "first_name": name["firstNm"],
                        "middle_name": name["midNm"],
                        "last_name": name["lastNm"],
                        "suffix": name["sufNm"],
                    })

                for eh in el.findall("./EmpHss/EmpHs"):
                    emp_hist.append({
                        "advisor_crd": pk,
                        "firm_name_on_record": eh.get("orgNm"),
                        "city": eh.get("city"),
                        "state": eh.get("state"),
                        "from_date": eh.get("fromDt"),
                        "to_date": eh.get("toDt"),
                    })
                for ex in el.findall("./Exms/Exm"):
                    exams.append({
                        "advisor_crd": pk,
                        "exam_code": ex.get("exmCd"),
                        "exam_name": ex.get("exmNm"),
                        "exam_date": ex.get("exmDt"),
                    })
                for ob in el.findall("./OthrBuss/OthrBus"):
                    othr.append({"advisor_crd": pk, "description": ob.get("desc")})

                for emp in el.findall("./CrntEmps/CrntEmp"):
                    regs = emp.findall("./CrntRgstns/CrntRgstn")
                    branches = emp.findall("./BrnchOfLocs/BrnchOfLoc")
                    b0 = branches[0] if branches else None
                    employments.append({
                        "advisor_crd": pk,
                        "firm_crd": emp.get("orgPK"),
                        "firm_name_on_record": emp.get("orgNm"),
                        "emp_city": emp.get("city"),
                        "emp_state": emp.get("state"),
                        "emp_postal": emp.get("postlCd"),
                    "emp_country": emp.get("cntry"),
                    "emp_street1": emp.get("str1"),
                    "reg_categories": "|".join(sorted({r.get("regCat", "")
                                                       for r in regs if r.get("regCat")})),
                    "reg_status": "|".join(sorted({r.get("st", "")
                                                   for r in regs if r.get("st")})),
                    "reg_earliest_date": min([r.get("stDt") for r in regs
                                              if r.get("stDt")], default=None),
                        "n_branch_locations": len(branches),
                        "branch_city": b0.get("city") if b0 is not None else None,
                        "branch_state": b0.get("state") if b0 is not None else None,
                        "n_registrations": len(regs),
                        "reg_states": "|".join(sorted({r.get("regAuth", "")
                                                       for r in regs if r.get("regAuth")})),
                    })
                el.clear()
        print(f"  [{mi:>2}/{len(members)}] {member:24s} advisors={len(advisors):>8,} "
              f"emp={len(employments):>8,} hist={len(emp_hist):>9,} exams={len(exams):>8,}")

    base = (pd.DataFrame(advisors), pd.DataFrame(employments),
            pd.DataFrame(emp_hist), pd.DataFrame(exams), pd.DataFrame(othr))
    if include_other_names:
        return base + (pd.DataFrame(other_names, columns=OTHER_NAME_COLUMNS),)
    return base


def parse_prior_registrations(zip_path: pathlib.Path) -> pd.DataFrame:
    """One row per (advisor, previous firm). Traces adviser movement -- which
    firms are recruiting and which are losing people."""
    rows = []
    z = zipfile.ZipFile(zip_path)
    for member in sorted(z.namelist()):
        with z.open(member) as f:
            for _, el in ET.iterparse(f, events=("end",)):
                if el.tag != "Indvl":
                    continue
                info = el.find("./Info")
                if info is not None:
                    pk = info.get("indvlPK")
                    for pr in el.findall("./PrevRgstns/PrevRgstn"):
                        rows.append({
                            "advisor_crd": pk,
                            "firm_crd": pr.get("orgPK"),
                            "firm_name_on_record": pr.get("orgNm"),
                            "reg_begin": pr.get("regBeginDt"),
                            "reg_end": pr.get("regEndDt"),
                        })
                el.clear()
    return pd.DataFrame(rows)


def main() -> None:
    src = config.newest_feed(RAW, "IA_INDVL_Feed_*.xml.zip")
    print(f"parsing {src.name} ...")
    adv, emp, hist, exams, othr, other_names = parse(
        src, include_other_names=True)
    INTERIM.mkdir(parents=True, exist_ok=True)
    adv.to_parquet(INTERIM / "advisors.parquet", index=False)
    emp.to_parquet(INTERIM / "advisor_employments.parquet", index=False)
    other_names.to_parquet(INTERIM / "advisor_other_names.parquet", index=False)
    nickname_evidence = build_nickname_evidence(adv, other_names)
    nickname_evidence.to_parquet(
        INTERIM / "sec_nickname_evidence.parquet", index=False)
    print(f"\nadvisors    {len(adv):,} rows -> advisors.parquet")
    print(f"employments {len(emp):,} rows -> advisor_employments.parquet")
    print(f"other names {len(other_names):,} rows -> advisor_other_names.parquet")
    common = int((nickname_evidence["evidence_status"] == "common_candidate").sum())
    print(f"nicknames   {len(nickname_evidence):,} observed pairs; {common:,} "
          "common candidates -> sec_nickname_evidence.parquet")
    for df, nm in [(hist, "advisor_employment_history"), (exams, "advisor_exams"),
                   (othr, "advisor_other_business")]:
        df.to_parquet(INTERIM / f"{nm}.parquet", index=False)
        print(f"{nm:27s} {len(df):>9,} rows")
    prev = parse_prior_registrations(src)
    prev.to_parquet(INTERIM / "advisor_prior_registrations.parquet", index=False)
    print(f"prior regs  {len(prev):,} rows -> advisor_prior_registrations.parquet")


if __name__ == "__main__":
    main()

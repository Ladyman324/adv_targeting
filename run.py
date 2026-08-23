"""Build the firm + advisor tables and print verification checks.

Nothing is filtered out: every SEC-registered firm is retained with its
classification, score, and flags as attributes. Filtering happens in the BI layer.
"""
import sys, pathlib
import pandas as pd

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
import config, adv, score as scoring

pd.set_option("display.width", 240)


def main():
    print("loading", config.ADV_SOURCE.name, "...")
    d = adv.load()
    print(f"  {len(d):,} firms x {d.shape[1]} features\n")

    res = scoring.run(d)
    config.INTERIM.mkdir(parents=True, exist_ok=True)
    config.OUTPUT.mkdir(parents=True, exist_ok=True)
    res.to_parquet(config.INTERIM / "firms_scored.parquet", index=False)

    print("=" * 76)
    print("CHECK 1 - SELF-TEST: how is Equity Investment Corp classified?")
    me = res[res["crd"] == config.OUR_CRD]
    if me.empty:
        print("  !! our CRD not found")
    else:
        m = me.iloc[0]
        verdict = "PASS" if m["firm_type"] == "asset_manager" else "FAIL"
        print(f"  {m['name']} | firm_type={m['firm_type']} | motion={m['motion']}")
        print(f"  advisors={m['n_advisors']:.0f}  RAUM=${m['raum_total']/1e9:.2f}B  "
              f"wrap_pm_share={m['wrap_pm_share']:.1%}  -> {verdict}")

    print("\n" + "=" * 76)
    print("CHECK 2 - firm_type distribution (nothing dropped)")
    for k, v in res["firm_type"].value_counts().items():
        print(f"  {k:32s} {v:>6,}")
    print(f"  {'TOTAL':32s} {len(res):>6,}")

    print("\n" + "=" * 76)
    print("CHECK 3 - sales motion (targets only)")
    t = res[res["is_target"]]
    for k, v in t["motion"].value_counts().items():
        print(f"  {k:32s} {v:>6,}")
    print(f"  needs review: {res['review_reason'].notna().sum():,}")

    print("\n" + "=" * 76)
    print("TOP 20 TARGETS by opportunity_score (SIZE 50 + FIT 50; FIT = 5.G(7) 40 / wrap sponsor 10)")
    cols = ["name", "state", "n_advisors", "raum_total",
            "opportunity_score_size", "opportunity_score_fit",
            "opportunity_score", "motion"]
    top = t.nlargest(20, "opportunity_score")[cols].copy()
    top["raum_total"] = (top["raum_total"] / 1e9).round(1)
    top.columns = ["name", "st", "advisors", "raum_$B",
                   "size", "fit", "score", "motion"]
    print(top.to_string(index=False))

    res.to_csv(config.OUTPUT / "firms.csv", index=False)
    print(f"\nwrote {config.OUTPUT / 'firms.csv'}  (all {len(res):,} firms)")


if __name__ == "__main__":
    main()

"""Run whole-brochure extraction over the sample and verify every quote."""
from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
import config, extract  # noqa: E402

MODEL = "deepseek/deepseek-v4-flash"


def brochure_text(crd: str) -> str:
    files = sorted((config.ROOT / "data" / "brochures" / crd).glob("brochure_*.txt"))
    if not files:
        return ""
    return max(files, key=lambda p: p.stat().st_size).read_text(encoding="utf-8")


def main():
    sample = pd.read_csv(config.INTERIM / "sample20.csv", dtype={"crd": str})
    results, in_tok, out_tok = [], 0, 0

    for i, row in enumerate(sample.itertuples(), 1):
        crd = row.crd
        text = brochure_text(crd)
        if not text:
            print(f"[{i:>2}] {crd:>7} NO BROCHURE TEXT")
            continue
        try:
            res = extract.call_openrouter(text, MODEL)
        except Exception as e:                                   # noqa: BLE001
            print(f"[{i:>2}] {crd:>7} ERROR {type(e).__name__}: {str(e)[:80]}")
            continue

        rep = extract.verify(res["parsed"], text)
        counts = pd.Series(list(rep.values())).value_counts().to_dict()
        u = res["usage"]
        in_tok += u.get("prompt_tokens", 0)
        out_tok += u.get("completion_tokens", 0)

        bad = sum(v for k, v in counts.items() if k.isupper() or k == "missing_field")
        print(f"[{i:>2}] {crd:>7} {row.name[:28]:28s} "
              f"{len(text):>7,}ch {res['elapsed_s']:>5.1f}s  "
              f"ok={counts.get('verified',0):>2} ns={counts.get('not_stated',0):>2} "
              f"bad={bad:>2}")

        results.append({"crd": crd, "name": row.name, "chars": len(text),
                        "parsed": res["parsed"], "verify": rep,
                        "usage": u, "elapsed_s": res["elapsed_s"],
                        "truncated": res["truncated"],
                        "raw_content": res["raw_content"]})

    out = config.INTERIM / "extract_deepseek.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    cost = in_tok / 1e6 * 0.09 + out_tok / 1e6 * 0.18
    print(f"\n{len(results)}/{len(sample)} firms extracted")
    print(f"tokens: {in_tok:,} in / {out_tok:,} out   cost ${cost:.4f}")
    print(f"projected for 1,538 Tier A firms: "
          f"${cost / max(len(results),1) * 1538:.2f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

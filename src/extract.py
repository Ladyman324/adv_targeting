"""Pass B: whole-brochure extraction into a fixed schema.

Deliberately contains NO judgment fields. Every field is a fact the brochure
either states or does not state. Each extracted value is paired with a verbatim
quote so the value can be mechanically verified against the source text --
this is what converts a cheap model's failures from silent to detectable.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).parents[1]
CACHE = ROOT / "data" / "brochures"
OUT = ROOT / "data" / "interim"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FIELDS = {
    "uses_third_party_managers":
        "Does the firm use, recommend, or select outside/third-party/independent "
        "investment managers for client assets? yes | no | not_stated",
    "third_party_manager_aum_usd":
        "Dollar amount of client assets managed by third-party/outside managers. "
        "Number only, no symbols or commas. null if not stated.",
    "account_minimum_usd":
        "Stated minimum account size for portfolio management. Number only. "
        "null if none stated or explicitly no minimum.",
    "has_no_account_minimum":
        "Does the brochure explicitly say there is no account minimum? "
        "yes | no | not_stated",
    "max_advisory_fee_pct":
        "Highest stated annual advisory fee as a percent. Number only "
        "(e.g. 2 for 2%). null if not stated.",
    "custodians_named":
        "List of custodian firm names explicitly named. Empty list if none.",
    "affiliated_broker_dealer":
        "Name of any affiliated/related broker-dealer. null if none stated.",
    "mentions_separately_managed_accounts":
        "Does it discuss separately managed accounts or SMA programs? "
        "yes | no | not_stated",
    "mentions_value_investing":
        "Does it describe a value investing approach, intrinsic value, or "
        "undervalued securities? yes | no | not_stated",
    "wrap_fee_program":
        "Does the firm participate in a wrap fee program? yes | no | not_stated",
}

SYSTEM = """You extract facts from SEC Form ADV Part 2A brochures.

Rules:
- Report ONLY what the document explicitly states. Never infer or estimate.
- If the document does not state something, use "not_stated" or null. Do NOT guess.
- For every field, supply a `quote` copied EXACTLY, character for character, from
  the document. The quote must be a contiguous span you actually saw in the text.
- If you cannot supply an exact quote, the value must be "not_stated"/null.
- Never invent a dollar amount. If no figure appears, the value is null.

Return ONLY a JSON object, no prose, with this shape:
{"<field>": {"value": <value>, "quote": "<verbatim span or null>"}, ...}"""


def build_prompt(text: str) -> str:
    spec = "\n".join(f"- {k}: {v}" for k, v in FIELDS.items())
    return (f"Extract these fields:\n{spec}\n\n"
            f"--- BEGIN BROCHURE ---\n{text}\n--- END BROCHURE ---")


def call_openrouter(text: str, model: str, max_chars: int = 400_000) -> dict:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    truncated = len(text) > max_chars
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(text[:max_chars])}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    t0 = time.time()
    raw = json.loads(urllib.request.urlopen(req, timeout=300).read())
    content = raw["choices"][0]["message"]["content"]
    return {
        "parsed": _loads_loose(content),
        "raw_content": content,
        "usage": raw.get("usage", {}),
        "elapsed_s": round(time.time() - t0, 1),
        "truncated": truncated,
    }


def _loads_loose(s: str):
    """Models sometimes wrap JSON in prose or code fences."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# Typographic variants that PDF extraction and model echo disagree on. Without
# folding these, verification rejects quotes that are genuinely present.
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", " ": " ",
    "�": " ",  # replacement char from encoding round-trips
})


# Typeset PDFs render these as single ligature glyphs while the model echoes the
# expanded letters. Unfolded, any quote containing "affiliated", "fee", "office",
# or "financial" fails to match its own source text.
_LIGATURES = {
    0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl",
    0xFB03: "ffi", 0xFB04: "ffl", 0xFB05: "ft", 0xFB06: "st",
}


def _norm(s: str) -> str:
    """Fold typography and collapse whitespace so PDF line-wrapping, curly
    quotes, ligatures, and encoding artifacts don't defeat a valid match."""
    folded = str(s).translate(_FOLD).translate(_LIGATURES)
    return re.sub(r"\s+", " ", folded).strip().lower()


def _quote_present(quote: str, hay: str) -> bool:
    """A quote counts as present if it appears verbatim, or -- when the model
    joined discontiguous spans with an ellipsis -- if every segment appears."""
    q = _norm(quote)
    if not q:
        return False
    if q in hay:
        return True
    parts = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if len(p) > 12]
    return bool(parts) and all(p in hay for p in parts)


# A negative finding ("no", "false") is an assertion of absence -- there is by
# definition no span to quote, so requiring one is a schema error on our side.
_NEGATIVE = {"no", "false", "none", "not_stated", "null", ""}


def verify(parsed: dict, source: str) -> dict:
    """Check each quote actually appears in the source. Returns per-field status."""
    hay = _norm(source)
    report = {}
    for field in FIELDS:
        entry = (parsed or {}).get(field)
        if not isinstance(entry, dict):
            report[field] = "missing_field"
            continue
        value, quote = entry.get("value"), entry.get("quote")

        if value in (None, "", [], "null", "not_stated"):
            report[field] = "not_stated"
        elif str(value).strip().lower() in _NEGATIVE:
            report[field] = "negative_finding"
        elif not quote or str(quote).strip().lower() in _NEGATIVE:
            report[field] = "VALUE_WITHOUT_QUOTE"
        elif _quote_present(quote, hay):
            report[field] = "verified"
        else:
            report[field] = "QUOTE_NOT_IN_SOURCE"
    return report

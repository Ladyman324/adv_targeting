"""Create WebVTT captions and a review transcript with faster-whisper.

This is an optional release tool, not an application dependency. Install
faster-whisper in an isolated environment and run it against the source MP4.
The generated files belong in ignored dist/training/, not in the static app.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path


DOMAIN_PROMPT = (
    "Advisor Map training for EIC sales representatives. Terms include Advisor "
    "Map, Field App, SEC, CRD, Act!, EIC assets, Morgan Stanley, UBS, Raymond "
    "James, Merrill Lynch, Edward Jones, key person, research, scheduler, "
    "dynamic list, email batch, approved materials, and quarterly commentary."
)

# Reviewed corrections for phrases whose pronunciation is ambiguous to the
# model but whose on-screen value or application vocabulary is definitive.
CORRECTIONS = {
    "advisors.eicatlantic.com": "advisors.eicatlanta.com",
    "the southeast format": "the Southeast territory",
    "funds in ETFs": "funds and ETFs",
    "assets to act.": "assets to Act!.",
    "once individual building": "one individual building",
    "call contracts directly": "call contacts directly",
    "duly registered": "dually registered",
    "VCCingMarketingMaterial.EICAdelana.com":
        "BCCing marketingmaterial@eicatlanta.com",
}


def reviewed(text: str) -> str:
    for heard, actual in CORRECTIONS.items():
        text = text.replace(heard, actual)
    return text


def stamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="WebVTT file to write")
    parser.add_argument("--model", default="small.en")
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    args.output.parent.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    stream, info = model.transcribe(
        str(args.input),
        language="en",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
        initial_prompt=DOMAIN_PROMPT,
    )
    segments = []
    for segment in stream:
        sentence = reviewed(" ".join(segment.text.strip().split()))
        if sentence:
            segments.append({
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": sentence,
            })

    vtt = ["WEBVTT", "", "NOTE Machine-generated; reviewed before publication.", ""]
    for index, segment in enumerate(segments, 1):
        vtt.extend([
            str(index),
            f"{stamp(segment['start'])} --> {stamp(segment['end'])}",
            "\n".join(textwrap.wrap(segment["text"], width=76,
                                    break_long_words=False)),
            "",
        ])
    args.output.write_text("\n".join(vtt), encoding="utf-8")

    review = args.output.with_suffix(".transcript.json")
    review.write_text(json.dumps({
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "model": args.model,
        "segments": segments,
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(segments)} captions to {args.output}")
    print(f"review transcript: {review}")


if __name__ == "__main__":
    main()

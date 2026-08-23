"""Pre-compress webapp/data/*.json, and make a stale .gz impossible to serve.

contacts.json is 24 MB of JSON and compresses to 5.2 MB -- a 4.7x saving on the
slowest thing the map downloads. Compressing on the fly would cost 0.4s of CPU
per request for a file that changes a few times a day, so it is compressed once
here, at build time.

THE ONLY REAL RISK IS A STALE .gz, SO IT IS DESIGNED OUT
--------------------------------------------------------
Two files that can disagree is the entire downside of pre-compression. If
build_contacts.py rewrites contacts.json and nobody regenerates the .gz, the
map keeps serving YESTERDAY'S CONTACTS with no error anywhere -- a rep sees a
fix that appears not to have worked and has no reason to suspect the transport
layer. That is a correctness bug wearing a performance bug's clothes.

Three layers stop it, in order of when they act:

  1. WRITE TOGETHER. The builder calls write_json_gz(), which writes the JSON
     and its .gz in one step. There is no separate command to forget.
  2. RECORD WHAT WAS COMPRESSED. A manifest stores each source's size and
     mtime at the moment it was compressed.
  3. VERIFY BEFORE SERVING. serve.py re-stats the source on every request and
     serves the .gz ONLY if size and mtime still match the manifest. Anything
     else -- edited JSON, restored backup, half-finished build -- falls back to
     the uncompressed file, which is always correct.

The fallback direction matters: the failure mode is "slower", never "wrong".

Size and mtime rather than a hash because the check runs per request. Hashing
24 MB to answer a GET would cost more than the compression saved. Size+mtime
misses only a same-size edit within the filesystem's timestamp resolution,
which a build that rewrites the whole file does not produce.

ATOMIC WRITES
-------------
Both files are written to a temp name and replaced, so a reader during a build
sees the old file or the new one, never a truncated one. This project's data
lives on a network share where a partial read is a realistic failure.

Run:  python src/web_assets.py [--check]
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_DATA = ROOT / "webapp" / "data"
MANIFEST = WEB_DATA / ".gzip_manifest.json"

# Level 6. Level 9 buys 2% more for 2.4x the compression time, and level 1
# gives up 25% of the saving -- measured on contacts.json at 24.17 MB:
#     -1  6.45 MB  0.15s      -6  5.18 MB  0.40s      -9  5.06 MB  0.97s
LEVEL = 6
# Below this, the HTTP and gzip overhead is a bigger share than the saving.
MIN_BYTES = 64 * 1024


def stat_key(path: pathlib.Path) -> dict:
    st = path.stat()
    # mtime as an int: some filesystems (and this project's network share)
    # round sub-second precision differently between write and read, which
    # would make a fresh .gz look stale on every request.
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(entries: dict) -> None:
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, MANIFEST)


def compress(path: pathlib.Path, manifest: dict | None = None) -> bool:
    """Write <path>.gz next to path and record what was compressed."""
    if not path.exists() or path.stat().st_size < MIN_BYTES:
        return False
    entries = load_manifest() if manifest is None else manifest
    gz = path.with_suffix(path.suffix + ".gz")
    # Distinct temp name. `gz.with_suffix(".tmp")` resolves to the SAME path
    # write_json_gz uses for the JSON (contacts.json.tmp), so a future caller
    # doing both at once would have them overwrite each other.
    tmp = gz.with_name(gz.name + ".tmp")
    raw = path.read_bytes()
    # mtime=0 so the same input always produces byte-identical output; an
    # embedded timestamp would make every build look changed to a cache.
    # GzipFile does NOT close a fileobj handed to it, so `fileobj=open(...)`
    # leaks the handle -- and on Windows an open handle can make the following
    # os.replace fail with a sharing violation. The raw file is opened in its
    # own `with` so both are closed, in order, before the replace.
    with open(tmp, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=LEVEL,
                           fileobj=fh, mtime=0) as gzf:
            gzf.write(raw)
    os.replace(tmp, gz)
    # Stat AFTER writing the gz, so the manifest describes the source as it was
    # actually compressed.
    entries[path.name] = stat_key(path)
    if manifest is None:
        save_manifest(entries)
    return True


def write_json_gz(path: pathlib.Path, payload, **dumps_kwargs) -> None:
    """Write a JSON payload and its .gz together. The point of this function is
    that there is no way to do one without the other."""
    text = json.dumps(payload, **dumps_kwargs)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    compress(path)


def is_fresh(path: pathlib.Path, entries: dict | None = None) -> bool:
    """Does the .gz still describe the file sitting on disk right now?"""
    entries = load_manifest() if entries is None else entries
    gz = path.with_suffix(path.suffix + ".gz")
    if not gz.exists() or not path.exists():
        return False
    recorded = entries.get(path.name)
    return bool(recorded) and recorded == stat_key(path)


def refresh_all(verbose: bool = True) -> dict:
    entries = load_manifest()
    done, skipped = [], []
    for path in sorted(WEB_DATA.glob("*.json")):
        if path.name.startswith("."):
            continue
        if is_fresh(path, entries):
            skipped.append(path.name)
            continue
        if compress(path, entries):
            done.append(path)
    save_manifest(entries)
    if verbose:
        for path in done:
            gz = path.with_suffix(path.suffix + ".gz")
            print(f"    {path.name:28} {path.stat().st_size / 1e6:>7.2f} MB -> "
                  f"{gz.stat().st_size / 1e6:>6.2f} MB "
                  f"({gz.stat().st_size / path.stat().st_size:.0%})")
        print(f"[*] compressed {len(done)}, already current {len(skipped)}")
    return entries


WEB = ROOT / "webapp"
# Which files index.html cache-busts, in the order the tag hashes them.
# dial.js appears in BOTH lists because both pages load it. That means editing
# it invalidates the desktop bundle and the field shell together, which is
# correct: a shared module that changed on one page and not the other is how you
# get two views disagreeing about what a valid call outcome is.
#
# email.js/email.css are in BOTH lists for exactly that reason. They arrived
# pinned at a hand-typed `?v=1` and tracked by neither, which is the precise
# failure described in stamp_assets() below: edited, deployed, and invisible,
# because the tag never moves. A mail pipeline is a bad place to be shipping a
# fix that browsers refuse to fetch.
VERSIONED = ("app.js", "style.css", "dial.js", "email.js", "email.css")
_VTAG = re.compile(
    r'(app\.js|style\.css|dial\.js|email\.js|email\.css)\?v=([^"\']+)')

# The field view is versioned SEPARATELY, from its own files. One shared tag
# would mean every desktop tweak invalidates the phone's cached shell and every
# field tweak invalidates the map -- and the field shell is precisely what a rep
# on the road would have to re-download.
FIELD_VERSIONED = ("field.js", "field.css", "dial.js", "email.js", "email.css")
_FTAG = re.compile(
    r'(field\.js|field\.css|dial\.js|email\.js|email\.css)\?v=([^"\']+)')
_SWTAG = re.compile(r'const VERSION = "([^"]*)"')
_DATA_VERSION = re.compile(r'const DATA_VERSION = "([^"]+)";')
DATA_VERSION_FILES = (WEB / "app.js", WEB / "field.js")
# Legacy monolith retained as a build/debug artefact. deploy_swa.py deliberately
# never ships it and neither application fetches it; hashing it would make the
# source tree and staged deployment compute different browser versions.
FINGERPRINT_EXCLUDED = {"contacts.json"}


def data_json_paths(root: pathlib.Path = WEB_DATA) -> list:
    """Generated JSON inputs to the browser cache version.

    Gzip files, temporary files and dot-prefixed manifests are excluded because
    they repeat/derive from JSON. The root legacy contacts monolith is excluded
    because it is neither shipped nor fetched; deployed contact shards remain.
    """
    return sorted(
        (path for path in root.rglob("*.json")
         if path.is_file()
         and not any(part.startswith(".") for part in path.relative_to(root).parts)
         and path.relative_to(root).as_posix() not in FINGERPRINT_EXCLUDED),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def data_version_prefix(root: pathlib.Path = WEB_DATA) -> str:
    """Normalize metadata.generated_utc to a readable, URL-safe UTC prefix."""
    meta = root / "metadata.json"
    try:
        generated = json.loads(meta.read_text(encoding="utf-8"))["generated_utc"]
    except FileNotFoundError as exc:
        raise ValueError("metadata.json is missing") from exc
    except (ValueError, KeyError) as exc:
        raise ValueError("metadata.json has no valid generated_utc") from exc
    raw = str(generated).strip()
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"invalid generated_utc {raw!r}") from exc
    if moment.tzinfo is None:
        raise ValueError("generated_utc must include a timezone")
    moment = moment.astimezone(timezone.utc)
    prefix = moment.strftime("%Y%m%dT%H%M%S")
    if moment.microsecond:
        prefix += "F" + f"{moment.microsecond:06d}".rstrip("0")
    return prefix + "Z"


def _file_digest(path: pathlib.Path) -> tuple:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.digest()


def data_fingerprint(root: pathlib.Path = WEB_DATA) -> str:
    """Hash every generated JSON path and byte with unambiguous framing.

    Files are read concurrently to overlap network-share open latency. map()
    yields results in input order, so scheduling cannot change the root digest.
    """
    prefix = data_version_prefix(root)
    paths = data_json_paths(root)
    digest = hashlib.sha256()
    workers = min(16, max(1, len(paths)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, (size, content_digest) in zip(paths, pool.map(_file_digest, paths)):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(size.to_bytes(8, "big"))
            digest.update(content_digest)
    return prefix + "-" + digest.hexdigest()[:16]


def _constant_in(path: pathlib.Path, pattern: re.Pattern) -> str | None:
    if not path.exists():
        return None
    found = pattern.search(path.read_text(encoding="utf-8"))
    return found.group(1) if found else None


def sync_data_version() -> str | None:
    """Stamp both applications from metadata time plus all generated bytes.

    Standalone builders can update shards after metadata was written, so the
    timestamp is provenance, not the cache key by itself. The digest guarantees
    that any JSON change produces a new URL, including another same-day build.
    Repeating an unchanged stamp is stable.
    """
    missing = [path.name for path in DATA_VERSION_FILES
               if _constant_in(path, _DATA_VERSION) is None]
    if missing:
        raise ValueError(f"missing DATA_VERSION in: {', '.join(missing)}")
    expected = data_fingerprint()
    changed = False
    for path in DATA_VERSION_FILES:
        source = path.read_text(encoding="utf-8")
        fixed = _DATA_VERSION.sub(
            f'const DATA_VERSION = "{expected}";', source, count=1)
        if fixed != source:
            path.write_text(fixed, encoding="utf-8")
            changed = True
    return expected if changed else None


def asset_tag() -> str:
    """A content hash of the front-end files index.html pins."""
    blob = b"".join((WEB / name).read_bytes() for name in VERSIONED)
    return hashlib.sha1(blob).hexdigest()[:10]


def field_tag() -> str:
    """Content hash of the field view's shell files, INCLUDING sw.js.

    sw.js has to be in here. The worker's cache is named after this tag, and
    the tag was computed from field.js/css/dial.js only -- so a fix to the
    worker's own caching logic left the tag unchanged, the cache name
    unchanged, and every existing cache entry in place. The bug it fixed
    (caching a sign-in page as field.html) would have survived the fix on every
    device that already had it.

    Hashed with its VERSION line blanked, because that line holds this value:
    including it as written would make the tag depend on itself.
    """
    parts = []
    for name in FIELD_VERSIONED:
        if (WEB / name).exists():
            parts.append((WEB / name).read_bytes())
    sw = WEB / "sw.js"
    if sw.exists():
        text = _SWTAG.sub('const VERSION = ""', sw.read_text(encoding="utf-8"))
        parts.append(text.encode("utf-8"))
    blob = b"".join(parts)
    return hashlib.sha1(blob).hexdigest()[:10] if blob else ""


def stamp_field() -> bool:
    """Point field.html and the service worker at the CURRENT field shell.

    sw.js embeds the same tag as its cache name, so a deploy retires the old
    cache instead of serving a rep last week's app from their home screen --
    which is the stale-asset bug this project already hit on the desktop, in
    the one place it would be hardest to notice.
    """
    tag = field_tag()
    if not tag:
        return False
    changed = False
    html = WEB / "field.html"
    if html.exists():
        text = html.read_text(encoding="utf-8")
        fixed = _FTAG.sub(lambda m: f"{m.group(1)}?v={tag}", text)
        if fixed != text:
            html.write_text(fixed, encoding="utf-8"); changed = True
    sw = WEB / "sw.js"
    if sw.exists():
        text = sw.read_text(encoding="utf-8")
        fixed = _SWTAG.sub(f'const VERSION = "{tag}"', text)
        if fixed != text:
            sw.write_text(fixed, encoding="utf-8"); changed = True
    return changed


def stamp_assets() -> bool:
    """Point index.html at the CURRENT app.js and style.css.

    index.html pins both with `?v=`, so a browser that has loaded the page
    before keeps serving the old files from cache no matter what is on disk.
    That is not a small problem: an entire session's worth of panel work --
    the direct-line labels, the profile buttons, the teammate roster -- was
    edited, deployed and invisible, because the tag still read v=20260804e.

    It is the same failure as a stale .gz and is handled the same way: derived
    from content, checked on every run, never typed by hand.
    """
    idx = WEB / "index.html"
    text = idx.read_text(encoding="utf-8")
    tag = asset_tag()
    fixed = _VTAG.sub(lambda m: f"{m.group(1)}?v={tag}", text)
    if fixed == text:
        return False
    idx.write_text(fixed, encoding="utf-8")
    return True


def check_assets() -> list:
    """Names whose pinned ?v= no longer matches what is on disk."""
    text = (WEB / "index.html").read_text(encoding="utf-8")
    found = {name: ver for name, ver in _VTAG.findall(text)}
    tag = asset_tag()
    stale = [n for n in VERSIONED if found.get(n) != tag]

    ftag = field_tag()
    fhtml = WEB / "field.html"
    if ftag and fhtml.exists():
        ffound = {n: v for n, v in _FTAG.findall(fhtml.read_text(encoding="utf-8"))}
        stale += [n for n in FIELD_VERSIONED if ffound.get(n) != ftag]
        sw = WEB / "sw.js"
        if sw.exists():
            m = _SWTAG.search(sw.read_text(encoding="utf-8"))
            if not m or m.group(1) != ftag:
                stale.append("sw.js")
    return stale

def check_data_versions(expected: str | None = None) -> list:
    """Return deploy-blocking data fingerprint/JavaScript mismatches."""
    try:
        expected = expected or data_fingerprint()
    except (OSError, ValueError) as exc:
        return [str(exc)]
    problems = []
    for path in DATA_VERSION_FILES:
        actual = _constant_in(path, _DATA_VERSION)
        if actual is None:
            problems.append(f"{path.name} has no DATA_VERSION")
        elif actual != expected:
            problems.append(
                f"{path.name} DATA_VERSION {actual} != data fingerprint {expected}")
    return problems



def check(expected_version: str | None = None) -> int:
    """Report any .gz that no longer matches its source. Exit code is the count
    so this can gate a deploy."""
    entries = load_manifest()
    stale, missing = [], []
    for path in sorted(WEB_DATA.glob("*.json")):
        if path.name.startswith("."):
            continue
        gz = path.with_suffix(path.suffix + ".gz")
        if path.stat().st_size < MIN_BYTES:
            continue
        if not gz.exists():
            missing.append(path.name)
        elif not is_fresh(path, entries):
            stale.append(path.name)
    for name in stale:
        print(f"    [!] STALE  {name} -- .gz does not match the JSON on disk")
    for name in missing:
        print(f"    [ ] absent {name} -- no .gz, will be served uncompressed")
    drifted = check_assets()
    for name in drifted:
        print(f"    [!] STALE  index.html pins an old ?v= for {name} -- "
              f"browsers will serve a cached copy and your edit is invisible")
    version_drift = check_data_versions(expected_version)
    for problem in version_drift:
        print(f"    [!] STALE  {problem} -- run src/web_assets.py")

    if not stale and not drifted and not version_drift:
        print("[*] no stale .gz files; every compressed asset matches its source")
        print(f"[*] index.html pins the current app.js and style.css (v={asset_tag()})")
        if field_tag():
            print(f"[*] field.html + sw.js pin the current field shell "
                  f"(v={field_tag()})")
    return len(stale) + len(drifted) + len(version_drift)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="verify only; exit code is the number of stale files")
    args = ap.parse_args()
    if args.check:
        raise SystemExit(check())
    started = time.time()
    # Before any shell hashing: this stamps app.js/field.js, so asset hashes
    # include the exact data fingerprint they will request.
    bumped = sync_data_version()
    if bumped:
        print(f"[*] DATA_VERSION bumped to {bumped} to match the data build")
    if stamp_assets():
        print(f"[*] index.html re-stamped to v={asset_tag()} "
              f"(app.js / style.css changed since the last run)")
    if stamp_field():
        print(f"[*] field.html + sw.js re-stamped to v={field_tag()} "
              f"(field.js / field.css changed since the last run)")
    refresh_all()
    print(f"[*] {time.time() - started:.1f}s")
    problems = check()
    if problems:
        raise SystemExit(problems)


if __name__ == "__main__":
    main()

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


def sync_data_version() -> str | None:
    """Keep app.js's DATA_VERSION in step with the data actually built.

    THE SYMPTOM THIS REMOVES: a rep opening the map and being told

        "This page is running an older build (20260803b) than the data it just
         loaded (generated Aug 22, 2026). Reload with Ctrl+F5."

    -- which no amount of reloading fixes, because DATA_VERSION is a constant
    in the SOURCE and the browser is doing exactly what it was told.

    It was hand-typed, so it had to be remembered every time the pipeline ran,
    and it drifted the first time somebody rebuilt the data without thinking
    about a constant three thousand lines away in another language. The check
    that fires the warning was right; the constant was simply stale.

    Bumped HERE rather than in the exporters because this runs last, after
    every artefact is written -- and because it has to happen before the asset
    hash is taken. Editing app.js afterwards would leave index.html pinning a
    hash of the previous contents, which is the very failure the hash exists to
    prevent.

    Compared on the DATE only. The letter suffix is a same-day tiebreak someone
    can still set by hand; it is not an ordering.
    """
    meta = WEB_DATA / "metadata.json"
    app = WEB / "app.js"
    if not meta.exists() or not app.exists():
        return None
    try:
        generated = json.loads(meta.read_text(encoding="utf-8"))["generated_utc"]
    except (ValueError, KeyError):
        return None
    built = str(generated)[:10].replace("-", "")
    src = app.read_text(encoding="utf-8")
    found = re.search(r'const DATA_VERSION = "([^"]+)";', src)
    if not found:
        return None
    current = found.group(1)
    if re.sub(r"[^0-9]", "", current)[:8] >= built:
        return None
    updated = f"{built}a"
    app.write_text(src.replace(f'const DATA_VERSION = "{current}";',
                               f'const DATA_VERSION = "{updated}";', 1), encoding="utf-8")
    return updated


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


def check() -> int:
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
    if not stale and not drifted:
        print("[*] no stale .gz files; every compressed asset matches its source")
        print(f"[*] index.html pins the current app.js and style.css (v={asset_tag()})")
        if field_tag():
            print(f"[*] field.html + sw.js pin the current field shell "
                  f"(v={field_tag()})")
    return len(stale) + len(drifted)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="verify only; exit code is the number of stale files")
    args = ap.parse_args()
    if args.check:
        raise SystemExit(check())
    started = time.time()
    # Before any hashing: this edits app.js, and a hash taken first would pin
    # the previous contents.
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
    check()


if __name__ == "__main__":
    main()

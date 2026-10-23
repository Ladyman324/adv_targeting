"""Validated, immutable imports and opt-in acquisition inside refresh staging."""
from __future__ import annotations
import csv
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import tempfile
import refresh_core as core

ROSTER_COLUMNS = {
    "ubs": ({"MarketingName", "Emails", "LocalNumber", "City", "PostalCode"}, ("MarketingName",), ("Emails", "LocalNumber")),
    "morgan_stanley": ({"Name", "Email", "Main Phone", "City", "State", "Entity Type"}, ("Name",), ("Email", "Main Phone")),
    "merrill": ({"Marketing Name", "Email", "Formatted Phone", "City", "State"}, ("Marketing Name", "First Name", "Last Name"), ("Email", "Formatted Phone")),
    "edward_jones": ({"faName", "phone", "faCity", "faState"}, ("faName",), ("email", "emails", "phone")),
    "wells_fargo": ({"name", "emails", "phone_numbers", "address"}, ("name",), ("emails", "phone_numbers")),
    "lpl": ({"Display Name", "Email", "Phone", "City", "State"}, ("Display Name",), ("Email", "Phone")),
    "raymond_james": ({"name", "email", "phone", "city", "state"}, ("name",), ("email", "phone")),
    "captrust": ({"name", "office_phone", "office_city", "office_state"}, ("name",), ("office_phone", "direct_phone")),
    "ep_wealth": ({"name", "office_phone", "city", "state"}, ("name",), ("office_phone",)),
    "mercer": ({"name", "office_phone", "direct_phone"}, ("name",), ("office_phone", "direct_phone")),
}
PATTERNS = {
    "act": ("data/raw/act_contacts_*.json", "data/raw/act_eic_contact_*.json", "data/raw/act_pull_manifest_*.json"),
    "sec_individuals": ("data/raw/IA_INDVL_Feed_*",), "sec_firms": ("data/raw/IA_FIRM_SEC_Feed_*",),
    "sec_adv": ("data/raw/ia??????.zip", "data/raw/refresh_adv_*"),
    "barrons": ("data/raw/barrons_rankings*.json",), "forbes": ("data/raw/forbes_rankings*.json",),
}

def _source(source_id):
    from firm_rosters import FIRMS
    if source_id in PATTERNS or (source_id.startswith("roster:") and source_id[7:] in FIRMS):
        return source_id
    raise core.RefreshError("Unknown refresh source ID")

def _compatible(manifest, source_id):
    mode = manifest.get("mode")
    allowed = mode == "full" or (
        mode == "act-only" and source_id == "act"
        or mode == "selected-firm" and source_id.startswith("roster:") and source_id[7:] in manifest.get("firms", [])
        or mode == "sec-only" and source_id in ("sec_individuals", "sec_firms", "sec_adv")
        or mode == "rankings-only" and source_id in ("barrons", "forbes"))
    if not allowed:
        raise core.RefreshError("Source is outside this run's refresh mode or selected firms; create an appropriate run")

def _date(value):
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (ValueError, TypeError) as exc:
        raise core.RefreshError("Source date must be YYYY-MM-DD") from exc

def _source_files(stage, source_id):
    return core.paths_matching(stage, PATTERNS.get(source_id, ("data/raw/firm_rosters/" + source_id[7:] + "_*.csv",)))


def _attempt_failure(exc, interrupted, default):
    from refresh_resilience import HistoricalHold
    if interrupted:
        return {"errorCode": "INTERRUPTED"}
    if isinstance(exc, HistoricalHold):
        return {"errorCode": "SOURCE_HISTORY_HELD", "historyComparisonId": exc.comparison_id,
                "historyIssueCodes": exc.issue_codes}
    return {"errorCode": default}

def _copy(source, target):
    if source.is_symlink():
        raise core.RefreshError("Input symlink refused")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if core.file_hash(source) != core.file_hash(target):
            raise core.RefreshError("Immutable source filename already exists with different content")
        return False
    expected = core.file_hash(source)
    descriptor, name = tempfile.mkstemp(prefix=".refresh-import-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copy2(source, temporary)
        if core.file_hash(temporary) != expected or core.file_hash(source) != expected:
            raise core.RefreshError("Source changed or copy verification failed during import")
        if target.exists():
            if core.file_hash(target) != expected:
                raise core.RefreshError("Immutable source filename already exists with different content")
            return False
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True

def _roster(path, slug, baseline=None, reduction_reason=None):
    if reduction_reason is not None and (not isinstance(reduction_reason, str) or len(reduction_reason.strip()) < 12):
        raise core.RefreshError("Reviewed reduction reason must contain at least 12 characters")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = set(reader.fieldnames or []), list(reader)
    required, names, contacts = ROSTER_COLUMNS.get(slug, ({"name"}, ("name",), ("email", "phone", "office_phone", "direct_phone")))
    if not required.issubset(fields) or not fields.intersection(contacts):
        raise core.RefreshError("Roster lacks required firm-specific name/contact/location columns")
    if not rows or any(None in row for row in rows):
        raise core.RefreshError("Roster is empty or has malformed CSV rows")
    def present(value):
        return str(value or "").strip().lower() not in ("", "[]", "{}", "null", "none", "nan")
    email_fields = ("email", "emails", "Email", "Emails")
    phone_fields = ("phone", "Phone", "Main Phone", "Branch Phone", "Formatted Phone",
                    "LocalNumber", "phone_numbers", "office_phone", "direct_phone")
    def metrics(records):
        return {"records": len(records),
                "namedRecords": sum(any(present(row.get(k)) for k in names) for row in records),
                "contactRecords": sum(any(present(row.get(k)) for k in contacts) for row in records),
                "emailRecords": sum(any(present(row.get(k)) for k in email_fields) for row in records),
                "phoneRecords": sum(any(present(row.get(k)) for k in phone_fields) for row in records)}
    current = metrics(rows)
    named, reached = current["namedRecords"], current["contactRecords"]
    if not reached or (not named and slug != "sanctuary"):
        raise core.RefreshError("Roster has no usable named/contact evidence")
    prior, prior_metrics = None, None
    if baseline and baseline.exists():
        with baseline.open(encoding="utf-8-sig", newline="") as handle:
            prior_metrics = metrics(list(csv.DictReader(handle)))
        prior = prior_metrics["records"]
    losses = [key for key, before in (prior_metrics or {}).items() if before and current[key] < before * .8]
    if losses and not reduction_reason:
        raise core.RefreshError("Roster usable coverage fell more than 20% (" + ", ".join(losses)
                                + "); supply a reviewed reduction reason of at least 12 characters")
    sidecar = path.with_suffix(".completeness.json")
    meta = core.read_json(sidecar) if sidecar.exists() else None
    if meta and (meta.get("complete") is not True or meta.get("sha256") != core.file_hash(path)):
        raise core.RefreshError("Roster completeness evidence does not bind a complete CSV")
    if slug == "morgan_stanley" and (not meta or meta.get("teamComplete") is not True or meta.get("officeComplete") is not True):
        raise core.RefreshError("Morgan Stanley requires CSV-bound office/team completeness sidecar; directory-only refresh held")
    return {**current, "priorRecords": prior, "priorCoverage": prior_metrics, "coverageLosses": losses,
            "reductionReason": reduction_reason.strip() if reduction_reason else None,
            "completeness": "verified" if meta else "coverage_checked"}

def _rankings(path, provider, edition, categories):
    rows = core.read_json(path)
    if not isinstance(rows, list) or not rows:
        raise core.RefreshError("Ranking input must be a nonempty JSON record list")
    if edition is not None and (isinstance(edition, bool) or not str(edition).isdigit() or not 1900 <= int(edition) <= 2200):
        raise core.RefreshError("Ranking edition must be a four-digit year")
    ck, nk = ("ranking", "advisor") if provider == "barrons" else ("list", "name")
    encountered = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get(ck) or not row.get(nk):
            raise core.RefreshError("Ranking record lacks category or person name")
        year = row.get("year")
        if year is not None and (isinstance(year, bool) or not str(year).isdigit() or not 1900 <= int(year) <= 2200):
            raise core.RefreshError("Invalid ranking record year")
        if edition is not None and year is not None and int(year) != int(edition):
            raise core.RefreshError("Ranking year disagrees with selected edition")
        encountered.add(row[ck])
    if set(categories) - encountered:
        raise core.RefreshError("Selected ranking category is absent")
    return {"records": len(rows), "edition": int(edition) if edition is not None else None, "categories": list(categories)}

def _act_pair(path, scratch):
    from refresh_inventory import validate_act_snapshot
    if path.name.startswith("act_contacts_"):
        path = path.with_name(path.name.replace("act_contacts_", "act_pull_manifest_", 1))
    if not re.fullmatch(r"act_pull_manifest_[A-Za-z0-9_-]+\.json", path.name):
        raise core.RefreshError("ACT import requires a committed ACT pull manifest or its contacts JSON")
    payload, files = core.read_json(path), [path]
    raw = scratch / "data/raw"; raw.mkdir(parents=True, exist_ok=True)
    for role in ("act_contacts", "act_eic_contact"):
        items = [r for r in payload.get("inputs", []) if r.get("role") == role]
        if len(items) != 1:
            raise core.RefreshError("ACT manifest lacks one complete paired input")
        name = items[0].get("file", "")
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise core.RefreshError("ACT manifest input must use a basename")
        files.append(path.parent / name)
    for source in files:
        if source.is_symlink():
            raise core.RefreshError("Input symlink refused")
        _copy(source, raw / source.name)
    try:
        validated = validate_act_snapshot(scratch)
    except (SystemExit, ValueError, KeyError) as exc:
        raise core.RefreshError("ACT pair/provenance validation failed") from exc
    return [(p, "data/raw/" + p.name) for p in files], {"records": validated["records"]}

def _prepare(stage, sid, source, scratch, source_date, edition, categories, reason):
    source_date = _date(source_date)
    if not source.is_file() or source.is_symlink():
        raise core.RefreshError("Import input must be an existing regular file")
    if sid == "act":
        return _act_pair(source, scratch)
    if sid.startswith("roster:"):
        slug = sid[7:]
        if reason is not None and (not isinstance(reason, str) or len(reason.strip()) < 12):
            raise core.RefreshError("Reviewed reduction reason must contain at least 12 characters")
        if not re.fullmatch(re.escape(slug) + r"_\d{4}-\d{2}-\d{2}\.csv", source.name):
            raise core.RefreshError("Roster filename must be firm_slug_YYYY-MM-DD.csv")
        file_date = _date(source.name[-14:-4])
        if source_date and source_date != file_date:
            raise core.RefreshError("Roster source date disagrees with filename")
        prior = sorted((stage / "data/raw/firm_rosters").glob(slug + "_*.csv"))[-1:]
        # Historical person/field checks cannot be bypassed by the older
        # aggregate coverage reduction override.
        from refresh_resilience import gate_roster
        historical = gate_roster(stage, source, slug, prior[0] if prior else None)
        coverage_reason = reason
        if historical and historical.get("reviewDecisionSha256") and not coverage_reason:
            from refresh_review import _folder
            decision = core.read_json(_folder(stage, historical["comparisonId"], sid) / "decision.json")
            coverage_reason = decision["reason"]
        details = _roster(source, slug, prior[0] if prior else None, coverage_reason)
        if historical:
            details["historicalAcceptance"] = historical
        details["sourceDate"] = file_date
        products = [(source, "data/raw/firm_rosters/" + source.name)]
        if source.with_suffix(".completeness.json").exists():
            p = source.with_suffix(".completeness.json"); products.append((p, "data/raw/firm_rosters/" + p.name))
        return products, details
    if sid in ("barrons", "forbes"):
        details = _rankings(source, sid, edition, categories)
        # Freeze explicit edition metadata into bytes; a later CLI merge must
        # not depend on a transient command option to retain recognition year.
        rows = core.read_json(source)
        if categories:
            key = "ranking" if sid == "barrons" else "list"
            rows = [row for row in rows if row[key] in categories]
        details["records"] = len(rows)
        if edition is not None:
            rows = [{**row, "year": int(edition)} for row in rows]
        normalized = scratch / (sid + "_edition.json")
        core.atomic_json(normalized, rows)
        from refresh_resilience import gate_source
        details["historicalAcceptance"] = gate_source(stage, normalized, sid, {"categories": list(categories)})
        return [(normalized, "data/refresh/ranking_editions/" + sid + "/" + core.file_hash(normalized) + ".json"),
                (source, "data/refresh/source_archive/" + sid + "/original-" + core.file_hash(source) + "/" + source.name)], details
    if sid == "sec_adv":
        if source.suffix.lower() not in {".csv", ".zip"}:
            raise core.RefreshError("ADV import requires CSV or ZIP")
        from refresh_resilience import gate_source
        proof = gate_source(stage, source, sid, {"format": source.suffix.lower().lstrip(".")})
        return [(source, "data/raw/refresh_adv_" + core.file_hash(source) + source.suffix.lower())], {
            "records": proof["candidateRecords"], "historicalAcceptance": proof}
    individual = sid == "sec_individuals"
    pattern = r"IA_INDVL_Feed_\d{2}_\d{2}_\d{4}\.xml\.zip" if individual else r"IA_FIRM_SEC_Feed_\d{2}_\d{2}_\d{4}\.xml\.gz"
    if not re.fullmatch(pattern, source.name):
        raise core.RefreshError("SEC feed filename does not match requested feed type")
    date = dt.datetime.strptime(source.name.split("Feed_", 1)[1].split(".xml", 1)[0], "%m_%d_%Y").date().isoformat()
    if source_date and source_date != date:
        raise core.RefreshError("SEC source date disagrees with filename")
    from refresh_resilience import gate_source
    proof = gate_source(stage, source, sid)
    return [(source, "data/raw/" + source.name)], {"records": proof["candidateRecords"], "sourceDate": date,
                                               "historicalAcceptance": proof}

def _register(stage, manifest, sid, products, details, source_date, edition, categories, retrieved=False):
    if sid != "act":
        proof = details.get("historicalAcceptance") or {}
        from refresh_history import baseline_paths
        baseline = baseline_paths(stage, sid)
        baseline_facts = core.facts(baseline, stage)
        expected_hash = (core.digest(baseline_facts) if baseline_facts else None) if sid in {"barrons", "forbes"} else (core.file_hash(baseline[0]) if baseline else None)
        if (proof.get("status") != "accepted" or not products
                or proof.get("candidateSha256") != core.file_hash(products[0][0])
                or proof.get("baselineSha256") != expected_hash
                or proof.get("baselineFacts") != baseline_facts
                or (sid == "roster:edward_jones" and not baseline and not proof.get("reviewDecisionSha256"))):
            raise core.RefreshError("Source historical acceptance no longer binds registration inputs")
        if proof.get("reviewDecisionSha256"):
            from refresh_review import _folder, accepted_decision
            folder = _folder(stage, proof.get("comparisonId"), sid)
            decision_hash = accepted_decision(folder, core.read_json(folder / "comparison.json"))
            if decision_hash != proof["reviewDecisionSha256"]:
                raise core.RefreshError("Historical operator decision changed before registration")
            recorded = [entry for entry in manifest.get("acquisitionReviews", [])
                        if entry.get("comparisonId") == proof["comparisonId"] and entry.get("decision") == "approve"]
            if len(recorded) != 1 or recorded[0].get("decisionSha256") != decision_hash:
                raise core.RefreshError("Historical operator decision is not bound to the run audit trail")
        for warning in proof.get("warnings", []):
            message = sid + ": " + warning
            if message not in manifest.setdefault("warnings", []):
                manifest["warnings"].append(message)
    archive = stage / "data/refresh/source_archive" / sid.replace(":", "_")
    for source, relative in products:
        incoming = core.file_hash(source)
        for old in (() if sid in ("barrons", "forbes") else archive.glob("*/" + source.name)):
            if core.file_hash(old) != incoming:
                raise core.RefreshError("Immutable source filename already exists with different content")
        target = core.contained(stage, relative)
        if target.exists() and core.file_hash(target) != incoming:
            raise core.RefreshError("Immutable source filename already exists with different content")
    snapshot = core.digest({relative: core.file_hash(source) for source, relative in products})
    quarantine = stage / "data/refresh/quarantine" / sid.replace(":", "_") / snapshot
    selected = {r for _, r in products}
    displaced, created = [], []
    try:
        if sid not in ("barrons", "forbes"):
            for old in _source_files(stage, sid):
                relative = old.relative_to(stage).as_posix()
                if relative not in selected:
                    target = quarantine / relative
                    _copy(old, target); old.unlink(); displaced.append((old, target))
        for source, relative in products:
            for target in (core.contained(stage, relative), archive / snapshot / source.name):
                if _copy(source, target):
                    created.append(target)
        event = {"sourceId": sid, "importedUtc": core.utc(), "sourceDate": source_date,
                 "edition": str(edition) if edition is not None else None, "categories": list(categories),
                 "snapshotId": snapshot, "files": [r for _, r in products], **details}
        if retrieved:
            event["retrievedUtc"] = event["importedUtc"]
        manifest.setdefault("imports", []).append(event)
        if sid in ("barrons", "forbes"):
            manifest.setdefault("rankingImports", []).append({"provider": sid, "path": products[0][1],
                "edition": details["edition"], "categories": list(categories)})
        return event
    except BaseException:
        for p in reversed(created):
            p.unlink(missing_ok=True)
        for old, saved in reversed(displaced):
            old.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(saved, old)
        raise

def _finish(stage, manifest):
    from refresh_inventory import inventory
    manifest["inputs"] = core.facts(core.source_paths(stage, manifest.get("mode")), stage)
    manifest["sources"] = inventory(stage)
    by_id = {r["id"]: r for r in manifest["sources"]}
    for event in manifest.get("imports", []):
        row = by_id.get(event["sourceId"])
        if row and event.get("inventorySnapshotId") == row.get("snapshotId"):
            row.update(status="imported", records=event.get("records"),
                       sourceDate=event.get("sourceDate") or row.get("sourceDate"), edition=event.get("edition") or row.get("edition"))
    for event in manifest.get("attempts", []):
        row = by_id.get(event["sourceId"])
        if row:
            row.update(lastAttemptUtc=event.get("finishedUtc") or event["startedUtc"], attemptStatus=event["status"], errorCode=event.get("errorCode"))
    manifest["status"] = "ready"
    manifest.pop("candidate", None)

def _receipt(stage, manifest, attempt):
    file = stage / "data/refresh/source_receipts.json"
    receipts = core.read_json(file) if file.exists() else {}
    row = dict(receipts.get(attempt["sourceId"]) or {})
    row.update(lastAttemptUtc=attempt.get("finishedUtc") or attempt["startedUtc"],
               attemptStatus=attempt["status"], errorCode=attempt.get("errorCode"))
    if attempt["status"] == "success":
        from refresh_inventory import inventory
        event = next(e for e in reversed(manifest["imports"]) if e["sourceId"] == attempt["sourceId"])
        observed = next(e for e in inventory(stage) if e["id"] == attempt["sourceId"])
        same_snapshot = row.get("snapshotId") == observed["snapshotId"]
        retrieved = observed.get("retrievedUtc") or event.get("retrievedUtc")
        if not retrieved and same_snapshot:
            retrieved = row.get("retrievedUtc")
        event["inventorySnapshotId"] = observed["snapshotId"]
        row.update(status="acquired" if event.get("retrievedUtc") else "imported",
                   snapshotId=observed["snapshotId"], fileFacts=observed["fileFacts"],
                   sourceDate=event.get("sourceDate") or observed.get("sourceDate"),
                   edition=str(event["edition"]) if event.get("edition") is not None else observed.get("edition"),
                   retrievedUtc=retrieved, lastSuccessUtc=retrieved)
    else:
        row["status"] = "failed"
    receipts[attempt["sourceId"]] = row
    core.atomic_json(file, receipts)
    manifest["inputs"] = core.facts(core.source_paths(stage, manifest.get("mode")), stage)

def import_snapshot(root, run_id, source_id, path, source_date=None, edition=None, categories=(), reduction_reason=None):
    _source(source_id)
    if Path(path).is_symlink():
        raise core.RefreshError("Input symlink refused")
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, source_id)
    with core.lock(directory), core.operation(directory, manifest, "importing", success="ready",
                                               invalidate=("candidate", "validation", "comparison")):
        core.check_inputs(stage, manifest)
        attempt = {"sourceId": source_id, "kind": "import", "startedUtc": core.utc(), "status": "running"}
        manifest.setdefault("attempts", []).append(attempt); core.save_run(directory, manifest)
        try:
            with tempfile.TemporaryDirectory(prefix=".import-", dir=stage) as tmp:
                products, details = _prepare(stage, source_id, Path(path).resolve(), Path(tmp), source_date, edition, categories, reduction_reason)
                _register(stage, manifest, source_id, products, details, source_date, edition, categories)
            attempt.update(status="success", finishedUtc=core.utc()); _receipt(stage, manifest, attempt); _finish(stage, manifest)
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            attempt.update(status="incomplete" if interrupted else "failed", finishedUtc=core.utc(),
                           **_attempt_failure(exc, interrupted, "IMPORT_VALIDATION_FAILED"))
            if attempt["errorCode"] == "SOURCE_HISTORY_HELD":
                manifest["errorCode"] = "SOURCE_HISTORY_HELD"
            # Persist the original failure even if writing its source receipt fails.
            core.save_run(directory, manifest)
            try:
                _receipt(stage, manifest, attempt)
            finally:
                core.save_run(directory, manifest)
            raise
        core.save_run(directory, manifest)
    return manifest

def acquisition_commands(sid, act_user=None, act_db=None):
    _source(sid)
    if sid == "act":
        if not act_user or not act_db:
            raise core.RefreshError("ACT requires --act-user and --act-db; ACT_PASSWORD stays in environment")
        return [["src/act_pull.py", "--user", act_user, "--db", act_db]]
    if sid in ("sec_individuals", "sec_firms"):
        return [["src/fetch_feeds.py", "individual" if sid == "sec_individuals" else "firm_sec"]]
    if sid in ("barrons", "forbes"):
        raise core.RefreshError("Browser handoff: run src/" + sid + "_harvest.js in the authorized session, then import JSON with edition/category")
    if sid == "sec_adv":
        raise core.RefreshError("Download official SEC ADV firm CSV/ZIP, then import sec_adv with --source-date")
    slug = sid[7:]
    if slug == "morgan_stanley":
        return [["src/morgan_stanley_async.py"], ["src/morgan_office_async.py"],
                ["src/morgan_team_async.py", "--concurrency", "2", "--batch-size", "20", "--delay-ms", "2000"]]
    if slug == "edward_jones":
        return [["src/edward_jones_async.py", "run", "--location", "Atlanta, Georgia", "--no-launch-chrome"]]
    if slug == "rj_branches":
        raise core.RefreshError("Raymond James branch browser capture requires operator handoff; import completed CSV")
    return [["src/" + slug + "_async.py"]]

def acquire(root, run_id, source_id, allow_network=False, act_user=None, act_db=None):
    _source(source_id)
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, source_id)
    with core.lock(directory), core.operation(directory, manifest, "acquiring", success="ready",
                                               invalidate=("candidate", "validation", "comparison")):
        core.check_inputs(stage, manifest)
        attempt = {"sourceId": source_id, "kind": "acquire", "startedUtc": core.utc(), "status": "running"}
        manifest.setdefault("attempts", []).append(attempt); core.save_run(directory, manifest)
        command_running = False
        try:
            if not allow_network:
                raise core.RefreshError("Acquisition requires explicit --allow-network")
            commands = acquisition_commands(source_id, act_user, act_db)
            baseline_hash = core.digest({"code": manifest.get("codeNormalization", manifest["codeFiles"]),
                "inputs": {k: v for k, v in manifest["inputs"].items()
                    if k.startswith(("data/raw/", "data/reference/", "data/firm_api/", "data/firm_owners/"))}})
            work = stage / "data/refresh/acquisition_work" / source_id.replace(":", "_") / baseline_hash
            work.mkdir(parents=True, exist_ok=True)
            marker = work / ".refresh-acquisition.json"
            identity = {"runId": run_id, "sourceId": source_id, "baselineHash": baseline_hash}
            if not marker.exists():
                for relative in manifest["codeFiles"]:
                    dst = core.contained(work, relative); dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stage / relative, dst)
                for relative in manifest["inputs"]:
                    if relative.startswith(("data/raw/", "data/reference/", "data/firm_api/", "data/firm_owners/")):
                        dst = core.contained(work, relative); dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(stage / relative, dst)
                for relative in ("data/interim", "data/output/firms.parquet"):
                    src, dst = stage / relative, work / relative
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    elif src.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
                core.atomic_json(marker, identity)
            elif core.read_json(marker) != identity:
                raise core.RefreshError("Acquisition workspace ownership mismatch")
            before = core.facts(_source_files(work, source_id), work)
            logs = directory / "logs"; logs.mkdir(exist_ok=True)
            for number, command in enumerate(commands):
                if not (work / command[0]).is_file():
                    raise core.RefreshError("Source scraper unavailable; use validated import")
                command_running = True
                core.execute_command(work, root, command, logs / (source_id.replace(":", "_") + "-acquire-" + str(number) + ".log"), online=True)
                command_running = False
            after = core.facts(_source_files(work, source_id), work)
            changed = [work / r for r, f in after.items() if before.get(r) != f]
            candidates = sorted(p for p in changed if p.name.startswith("act_pull_manifest_")) if source_id == "act" else sorted(changed)
            if not candidates:
                candidates = sorted(p for p in _source_files(work, source_id)
                    if source_id != "act" or p.name.startswith("act_pull_manifest_"))
                if not candidates:
                    raise core.RefreshError("Acquisition produced no committed source; last-good retained")
                manifest.setdefault("warnings", []).append("Acquisition reused unchanged source bytes: " + source_id)
            source = candidates[-1]
            if source_id == "roster:morgan_stanley":
                coverage = core.read_json(work / "data/output/morgan_stanley_team_coverage.json")
                summary = coverage.get("summary", coverage)
                if summary.get("crawlComplete") is not True or summary.get("pagesUnresolved", 0):
                    raise core.RefreshError("Morgan team crawl incomplete; last-good retained")
                core.atomic_json(source.with_suffix(".completeness.json"), {"complete": True, "teamComplete": True,
                    "officeComplete": True, "sha256": core.file_hash(source)})
            with tempfile.TemporaryDirectory(prefix=".acquired-", dir=stage) as tmp:
                products, details = _prepare(stage, source_id, source, Path(tmp), None, None, (), None)
                _register(stage, manifest, source_id, products, details, None, None, (), retrieved=bool(changed))
            attempt.update(status="success", finishedUtc=core.utc()); _receipt(stage, manifest, attempt); _finish(stage, manifest)
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            attempt.update(status="incomplete" if interrupted else "failed", finishedUtc=core.utc(),
                           **_attempt_failure(exc, interrupted, "ACQUISITION_FAILED"))
            if command_running:
                from refresh_repair import queue_acquisition_failure
                try:
                    incident = queue_acquisition_failure(stage, source_id,
                        "SOURCE_COMMAND_INTERRUPTED" if interrupted else "SOURCE_COMMAND_FAILED",
                        baseline_hash, core.digest(command))
                    attempt["acquisitionIncidentId"] = incident["id"]
                except Exception:
                    # Diagnostic persistence must never hide the original failure.
                    attempt["diagnosticWriteFailed"] = True
            if attempt["errorCode"] == "SOURCE_HISTORY_HELD":
                manifest["errorCode"] = "SOURCE_HISTORY_HELD"
            # Persist the original failure even if writing its source receipt fails.
            core.save_run(directory, manifest)
            try:
                _receipt(stage, manifest, attempt)
            finally:
                core.save_run(directory, manifest)
            raise
        core.save_run(directory, manifest)
    return manifest

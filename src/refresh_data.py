"""One supported entry point for isolated Advisor Map data refreshes.

Start with plan. Build/validate/prepare never publish or contact external sources.
Acquisition requires an explicit separate --allow-network command.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from refresh_core import (MODES, RefreshError, build, build_steps, create_run,
                          load_run, read_json, utc, active_children)

ROOT = Path(__file__).resolve().parents[1]


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=ROOT, help="Repository root (fixtures/staging only; defaults to this checkout)")
    commands = p.add_subparsers(dest="action", required=True)
    for action in ("plan", "start"):
        sub = commands.add_parser(action)
        sub.add_argument("--mode", choices=MODES, required=True)
        sub.add_argument("--firm", action="append", default=[])
        sub.add_argument("--dry-run", action="store_true", help="Read-only plan; do not create a run")
        if action == "start":
            sub.add_argument("--stage-parent", type=Path, help="Local protected staging directory; defaults to OS temp")
            sub.add_argument("--run-id")
    for action in ("status", "build", "resume", "report", "validate", "prepare-release", "verify-release", "prepare-publication-receipt", "import", "acquire"):
        sub = commands.add_parser(action)
        sub.add_argument("--run", required=True)
        if action in {"build", "resume", "validate", "prepare-release", "prepare-publication-receipt", "import", "acquire"}:
            sub.add_argument("--dry-run", action="store_true")
        if action in {"build", "resume"}:
            sub.add_argument("--fallback-reason")
        if action in {"resume", "validate"}:
            sub.add_argument("--coverage-reason")
            sub.add_argument("--skip-tests", action="store_true", help="Diagnostic validation only; not a production-ready candidate")
        if action == "resume":
            sub.add_argument("--through", choices=["build", "validate", "prepare-release"], default="build")
        if action == "verify-release":
            sub.add_argument("--production", action="store_true", help="Require clean committed source and full test evidence; still does not publish")
        if action == "prepare-publication-receipt":
            sub.add_argument("--published-utc", required=True)
            sub.add_argument("--api-commit", required=True)
            sub.add_argument("--static-fingerprint", required=True)
            sub.add_argument("--recipient-registry-hash", required=True)
            sub.add_argument("--confirm-runtime-verified", action="store_true", help="Attest that these values were observed after the separately approved production deployment")
        if action in {"import", "acquire"}:
            sub.add_argument("--source", required=True, help="act, sec_individuals, sec_firms, sec_adv, roster:slug, barrons, forbes")
        if action == "import":
            sub.add_argument("--file", required=True, type=Path)
            sub.add_argument("--source-date")
            sub.add_argument("--edition")
            sub.add_argument("--category", action="append", default=[])
            sub.add_argument("--reduction-reason")
        if action == "acquire":
            sub.add_argument("--allow-network", action="store_true")
            sub.add_argument("--act-user")
            sub.add_argument("--act-db")
    sub = commands.add_parser("review-acquisition", help="Attest a decision on one exact held source snapshot; does not import or publish")
    sub.add_argument("--run", required=True)
    sub.add_argument("--source", default="roster:edward_jones")
    sub.add_argument("--comparison", required=True)
    sub.add_argument("--decision", choices=("approve", "reject"), required=True)
    sub.add_argument("--reviewer", required=True)
    sub.add_argument("--reason", required=True)
    sub.add_argument("--acknowledge-issue", action="append", default=[])
    sub.add_argument("--dry-run", action="store_true")
    sub = commands.add_parser("propose-repair", help="One explicitly authorized text-only AI proposal; never apply or publish")
    sub.add_argument("--run", required=True)
    sub.add_argument("--request", required=True)
    sub.add_argument("--fixture", type=Path, required=True)
    sub.add_argument("--model", required=True)
    sub.add_argument("--operator", required=True)
    sub.add_argument("--reason", required=True)
    sub.add_argument("--allow-ai", action="store_true")
    sub.add_argument("--confirm-public-or-synthetic-fixture", action="store_true")
    sub.add_argument("--dry-run", action="store_true")
    for action in ("validate-repair", "review-repair", "export-repair", "reauthorize-repair"):
        sub = commands.add_parser(action)
        sub.add_argument("--run", required=True)
        sub.add_argument("--request", required=True)
        sub.add_argument("--dry-run", action="store_true")
        if action == "validate-repair":
            sub.add_argument("--regression-fixture", type=Path, required=True)
            sub.add_argument("--timeout-seconds", type=int, default=10)
        if action == "review-repair":
            sub.add_argument("--decision", choices=("approve", "reject"), required=True)
            sub.add_argument("--reviewer", required=True)
            sub.add_argument("--reason", required=True)
        if action == "reauthorize-repair":
            sub.add_argument("--operator", required=True)
            sub.add_argument("--reason", required=True)
            sub.add_argument("--acknowledge-possible-charge", action="store_true")
    return p


def check_selection(mode, firms=()):
    from firm_rosters import FIRMS
    unknown = set(firms) - set(FIRMS)
    if unknown:
        raise RefreshError("Unknown firm slug: " + ", ".join(sorted(unknown)))
    if mode == "selected-firm" and not firms:
        raise RefreshError("selected-firm requires at least one --firm")


def make_plan(root, mode, firms=()):
    from refresh_inventory import inventory
    check_selection(mode, firms)
    sources = inventory(root)
    rankings = [s["id"] for s in sources if s["id"] in {"barrons", "forbes"} and s.get("sha256")]
    return {"schemaVersion": 1, "mode": mode, "firms": list(firms), "readOnly": True,
            "sources": sources, "steps": build_steps(mode, "<pinned ACT snapshot>", rankings),
            "boundaries": {"network": "acquire --allow-network only", "liveOutputsModified": False,
                           "publish": "separate approval and deployment, not implemented by this command",
                           "geocoding": "cached/offline; new external calls require separate approval"},
            "notes": ["Plan does not create a run, download data, or claim anything is newly refreshed.",
                      "Builds reuse registered snapshots. Use acquire/import explicitly before build.",
                      "A staged snapshot is not proof of production publication."]}


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.root.resolve()
    if args.action == "plan" or (args.action == "start" and args.dry_run):
        result = make_plan(root, args.mode, args.firm)
    elif args.action == "start":
        # Validate selection without doing an unrecorded duplicate inventory.
        check_selection(args.mode, args.firm)
        result = create_run(root, args.mode, args.firm, args.stage_parent, args.run_id)
        result = {"runId": result["id"], "status": result["status"], "outcome": result.get("outcome"), "workspace": result["workspaceRoot"],
                  "next": "import/acquire as needed, then build --run " + result["id"]}
    elif getattr(args, "dry_run", False):
        directory, manifest, stage = load_run(root, args.run)
        result = {"readOnly": True, "action": args.action, "runId": args.run, "status": manifest["status"],
                  "source": getattr(args, "source", None), "network": False, "publish": False}
    elif args.action == "status":
        directory, result, _ = load_run(root, args.run)
        result = {key: result.get(key) for key in ("id", "status", "outcome", "phase", "snapshotComplete", "recovery", "mode", "createdUtc", "updatedUtc", "failedStep", "errorCode", "steps", "validation", "candidate", "warnings", "attempts", "acquisitionReviews", "repairRequests")}
        result["activeProcesses"] = active_children(directory)
    elif args.action == "build":
        value = build(root, args.run, fallback_reason=args.fallback_reason)
        result = {"runId": value["id"], "status": value["status"], "outcome": value.get("outcome"), "warnings": value.get("warnings", []), "steps": len(value["steps"])}
    elif args.action == "resume":
        directory, prior, stage = load_run(root, args.run)
        if prior.get("phase") in {"capture", "copy"}:
            from refresh_stage import resume_stage
            resume_stage(root, args.run)
        value = build(root, args.run, retry=True, fallback_reason=args.fallback_reason)
        result = {"runId": value["id"], "status": value["status"], "outcome": value.get("outcome"), "warnings": value.get("warnings", [])}
        if args.through != "build":
            from refresh_release import validate, prepare
            value = validate(root, args.run, args.coverage_reason, not args.skip_tests)
            result.update(status=value["status"], outcome=value.get("outcome"), warnings=value.get("warnings", []))
            if args.through == "prepare-release":
                result = prepare(root, args.run)
    elif args.action == "report":
        from refresh_release import report
        result = report(root, args.run)
        _, value, _ = load_run(root, args.run)
        result.update(runId=args.run, status=value["status"], outcome=value.get("outcome"))
    elif args.action == "validate":
        from refresh_release import validate
        value = validate(root, args.run, args.coverage_reason, not args.skip_tests)
        result = {"runId": value["id"], "status": value["status"], "outcome": value.get("outcome"), "validation": value["validation"]["status"]}
    elif args.action == "prepare-release":
        from refresh_release import prepare
        result = prepare(root, args.run)
    elif args.action == "verify-release":
        from refresh_release import verify_release
        result = verify_release(root, args.run, args.production)
    elif args.action == "prepare-publication-receipt":
        from refresh_publication import prepare_receipt
        result = prepare_receipt(root, args.run, published_utc=args.published_utc,
            api_commit=args.api_commit, static_fingerprint=args.static_fingerprint,
            recipient_registry_hash=args.recipient_registry_hash,
            confirm_runtime_verified=args.confirm_runtime_verified)
    elif args.action == "review-acquisition":
        from refresh_review import review
        result = review(root, args.run, args.comparison, args.decision, args.reviewer,
                        args.reason, args.acknowledge_issue, source_id=args.source)
    elif args.action == "propose-repair":
        from refresh_repair_runner import run_for_run
        result = run_for_run(root, args.run, args.request, args.fixture, model=args.model,
                            operator=args.operator, reason=args.reason, allow_ai=args.allow_ai,
                            confirm_fixture=args.confirm_public_or_synthetic_fixture)
    elif args.action == "reauthorize-repair":
        from refresh_repair_retry import for_run
        result = for_run(root, args.run, args.request, operator=args.operator, reason=args.reason,
                         acknowledge_possible_charge=args.acknowledge_possible_charge)
    elif args.action == "validate-repair":
        from refresh_repair_validation import validate_for_run
        result = validate_for_run(root, args.run, args.request, args.regression_fixture,
                                  timeout_seconds=args.timeout_seconds)
        if result["status"] != "passed":
            print(json.dumps(result, sort_keys=True, indent=2))
            return 1
    elif args.action in {"review-repair", "export-repair"}:
        from refresh_repair_decision import for_run
        options = {"decision": args.decision, "reviewer": args.reviewer, "reason": args.reason} if args.action == "review-repair" else {}
        result = for_run(root, args.run, args.request,
                         action="review" if args.action == "review-repair" else "export", **options)
    elif args.action == "import":
        from refresh_acquire import import_snapshot
        result = import_snapshot(root, args.run, args.source, args.file, source_date=args.source_date,
                                 edition=args.edition, categories=args.category, reduction_reason=args.reduction_reason)
        result = {"runId": args.run, "status": result["status"], "outcome": result.get("outcome"), "source": args.source}
    elif args.action == "acquire":
        from refresh_acquire import acquire
        result = acquire(root, args.run, args.source, allow_network=args.allow_network, act_user=args.act_user, act_db=args.act_db)
        result = {"runId": args.run, "status": result["status"], "outcome": result.get("outcome"), "source": args.source}
    else:
        raise RefreshError("Unsupported action")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RefreshError, ValueError, OSError, KeyError) as exc:
        print("Refresh failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

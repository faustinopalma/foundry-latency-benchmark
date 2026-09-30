from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import zipfile

from analyze_benchmark import campaign_groups
from run_campaign import campaign_profiles, profile_conditions, window_profiles


def public_workflow(row):
    from math import isfinite

    def choice(value, options):
        if value not in options:
            raise ValueError("Unexpected public dimension")
        return value

    def number(value):
        if value is None:
            return None
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError("Invalid observation number")
        return value

    def timestamp(value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Observation timestamp has no timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    calls = []
    for call in row["calls"]:
        cleaned = {key: choice(call[key], (True, False)) for key in ("success", "correct", "completed", "schema_valid")}
        cleaned.update(error={} if call["error"] is not None else None, refusal=bool(call["refusal"]), expected={}, output_text="{}", stage=choice(call["stage"], (1, 2)))
        cleaned.update({key: number(call.get(key)) for key in ("http_status", "duration_ms", "first_content_ms", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "estimated_visible_token_interval_ms")})
        calls.append(cleaned)
    return {
        "phase": choice(row["phase"], ("measured", "warmup")),
        "window": choice(row["window"], ("window-0", "window-1", "window-2")),
        "client_region": choice(row["client_region"], ("swedencentral", "italynorth")),
        "deployment": {"region": choice(row["deployment"]["region"], ("swedencentral", "italynorth")), "model": choice(row["deployment"]["model"], ("gpt-5.4", "gpt-5.4-mini")), "sku": choice(row["deployment"]["sku"], ("GlobalStandard", "DataZoneStandard"))},
        "profile": {"name": choice(row["profile"]["name"], {profile["name"] for profile in campaign_profiles()})},
        "condition": {"api": choice(row["condition"]["api"], ("chat", "responses")), "stream": choice(row["condition"]["stream"], (True, False))},
        "started_utc": timestamp(row["started_utc"]), "ended_utc": timestamp(row["ended_utc"]),
        "success": choice(row["success"], (True, False)), "workflow_ms": number(row["workflow_ms"]), "case_index": number(row["case_index"]), "calls": calls,
    }


def load_snapshot(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive member")
        receipt = json.loads(archive.read("storage-verification.json"))
        if receipt.get("all_equal") is not True or not receipt.get("files"):
            raise ValueError("Missing nonempty verification receipt")
        run_id = receipt["run_id"]
        if not re.fullmatch(r"[a-z0-9-]+", run_id):
            raise ValueError("Invalid evidence run identifier")
        prefix = f"results/{run_id}/"
        cloud_prefix = f"runs/{run_id}/"
        expected = set()
        rows = []
        manifest = None
        for entry in receipt["files"]:
            if not entry["name"].startswith(cloud_prefix):
                raise ValueError("Unexpected receipt prefix")
            member = prefix + entry["name"][len(cloud_prefix):]
            if member in expected:
                raise ValueError("Duplicate receipt member")
            expected.add(member)
            payload = archive.read(member).removesuffix(b"\n")
            if len(payload) != entry["bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                raise ValueError("Evidence hash mismatch")
            if member == prefix + "manifest.json":
                manifest = json.loads(payload)
            elif member.startswith(prefix + "workflows/"):
                rows.append(public_workflow(json.loads(payload)))
        if expected != {name for name in names if name.startswith(prefix) and name.endswith(".json")} or manifest is None:
            raise ValueError("Evidence inventory mismatch")
        if manifest.get("request_protocol") != "schema-constrained-note-v2" or manifest.get("pilot") is not False:
            raise ValueError("Preview requires measured scale-campaign protocol v2")
        snapshot = json.loads(archive.read("snapshot.json")) if "snapshot.json" in names else None
        captured = {"verified_records": len(expected), "scope": "captured_snapshot" if snapshot else "final_export", "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if snapshot:
            if snapshot.get("verified_records") != len(expected):
                raise ValueError("Snapshot record count mismatch")
            captured.update(inventory_started_utc=snapshot["inventory_started_utc"], inventory_ended_utc=snapshot["inventory_ended_utc"])
        else:
            if receipt.get("inventory_scope") != "final_full_inventory" or "process-status.json" not in names:
                raise ValueError("Final export requires full inventory and process status")
            exit_code = json.loads(archive.read("process-status.json")).get("exit_code")
            if type(exit_code) is not int or exit_code not in (0, -15):
                raise ValueError("Unexpected campaign process exit")
            if exit_code == -15 and not any(member.startswith(prefix + "schedule/revision-applied-") for member in expected):
                raise ValueError("Terminated schedule requires a verified revision receipt")
            captured["process_exit_code"] = exit_code
        return run_id, rows, captured


def validate_completed_campaign(rows, sources):
    if len(sources) != 4 or any(source["scope"] != "final_export" for source in sources):
        raise ValueError("Complete campaign requires four final verified exports")
    expected = set()
    for window_index, client_region, resource_region, model, sku in itertools.product(
        (0, 1), ("swedencentral", "italynorth"), ("swedencentral", "italynorth"),
        ("gpt-5.4", "gpt-5.4-mini"), ("GlobalStandard", "DataZoneStandard"),
    ):
        for profile in window_profiles(client_region, resource_region, window_index):
            for condition in profile_conditions(profile):
                expected.update((f"window-{window_index}", client_region, resource_region, model, sku, profile["name"], condition["api"], condition["stream"], case_index) for case_index in range(100))
    measured = [row for row in rows if row["phase"] == "measured"]
    observed = {(row["window"], row["client_region"], row["deployment"]["region"], row["deployment"]["model"], row["deployment"]["sku"], row["profile"]["name"], row["condition"]["api"], row["condition"]["stream"], row["case_index"]) for row in measured}
    if len(measured) != len(expected) or observed != expected:
        raise ValueError("Complete campaign requires every case in the revised two-window matrix exactly once")


def build_dataset(paths, complete=False):
    rows = []
    sources = []
    seen_runs = set()
    identities = set()
    for path in paths:
        run_id, selected, source = load_snapshot(path)
        if run_id in seen_runs:
            raise ValueError("Duplicate source run")
        seen_runs.add(run_id)
        sources.append(source)
        for row in selected:
            identity = (row["window"], row["client_region"], *row["deployment"].values(), row["profile"]["name"], *row["condition"].values(), row["phase"], row["case_index"])
            if identity in identities:
                raise ValueError("Duplicate public observation")
            identities.add(identity)
        rows.extend(selected)
    if complete:
        validate_completed_campaign(rows, sources)
    conditions, comparisons = campaign_groups(rows)
    measured = [row for row in rows if row["phase"] == "measured"]
    failures = {}
    for condition in conditions:
        for category, count in condition["failure_categories"].items():
            failures[category] = failures.get(category, 0) + count
    return {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "preliminary": not complete,
            "request_protocol": "schema-constrained-note-v2", "planned_per_condition": 100,
            "started_utc": min(row["started_utc"] for row in measured), "ended_utc": max(row["ended_utc"] for row in measured),
            "attempted_workflows": len(measured), "correct_workflows": sum(row["success"] for row in measured), "warmup_workflows_excluded": len(rows) - len(measured),
            "attempted_calls": sum(condition["attempted_calls"] for condition in conditions), "failure_categories_calls": failures,
            "sources": sources, "conditions": conditions, "comparisons": comparisons}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("preview-data.json"))
    parser.add_argument("--template", type=Path)
    parser.add_argument("--complete", action="store_true", help="Require all 22,400 cases in the revised overnight-plus-morning campaign")
    args = parser.parse_args()
    data = build_dataset(args.snapshots, complete=args.complete)
    serialized = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if args.template:
        template = args.template.read_text(encoding="utf-8")
        if template.count("__PUBLIC_DATA__") != 1:
            raise ValueError("Expected exactly one public-data placeholder")
        args.output.write_text(template.replace("__PUBLIC_DATA__", serialized.replace("<", "\\u003c")), encoding="utf-8")
    else:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output.name, "attempted_workflows": data["attempted_workflows"], "correct_workflows": data["correct_workflows"], "conditions": len(data["conditions"]), "verified_records": sum(source["verified_records"] for source in data["sources"])}))


if __name__ == "__main__":
    main()
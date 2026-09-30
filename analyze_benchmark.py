from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import random
import statistics
import zipfile

from benchmark import canonical_json, digest, quantile


def distribution(values):
    values = [value for value in values if value is not None]
    return {"n": len(values), "mean": statistics.mean(values) if values else None,
            "p50": quantile(values, 0.5), "p90": quantile(values, 0.9),
            "p95": quantile(values, 0.95), "stdev": statistics.stdev(values) if len(values) > 1 else None}


def paired_difference(left, right):
    left_by_case = {row["case_index"]: row for row in left if row["success"]}
    right_by_case = {row["case_index"]: row for row in right if row["success"]}
    indexes = sorted(left_by_case.keys() & right_by_case.keys())
    differences = [left_by_case[index]["workflow_ms"] - right_by_case[index]["workflow_ms"] for index in indexes]
    result = {"pairs": len(differences), "left_attempted": len(left), "right_attempted": len(right),
              "mean_difference_ms": statistics.mean(differences) if differences else None,
              "median_difference_ms": statistics.median(differences) if differences else None,
              "bootstrap_95_percent_interval_ms": None}
    if len(differences) >= 20:
        generator = random.Random(20260930)
        means = [statistics.mean(generator.choices(differences, k=len(differences))) for _ in range(5000)]
        result["bootstrap_95_percent_interval_ms"] = [quantile(means, 0.025), quantile(means, 0.975)]
    return result


def failure_detail(call):
    try:
        actual = json.loads(call["output_text"])
    except (ValueError, TypeError):
        actual = None
    differences = {}
    if isinstance(actual, dict):
        for name, expected in call["expected"].items():
            if name not in actual or actual[name] != expected or type(actual[name]) is not type(expected):
                differences[name] = {"expected": expected, "actual": actual.get(name), "missing": name not in actual}
        for name in actual.keys() - call["expected"].keys():
            differences[name] = {"unexpected": actual[name]}
    if call["error"] is not None:
        category = "technical_error"
    elif call["refusal"]:
        category = "refusal"
    elif not call["completed"]:
        category = "incomplete_generation"
    elif not call["schema_valid"]:
        category = "invalid_schema"
    elif not call["correct"]:
        category = "incorrect_fields"
    else:
        category = "missing_usage"
    return {"category": category, "stage": call["stage"], "differences": differences, "error": call["error"]}


def distribution_with_intervals(values):
    values = [value for value in values if value is not None]
    result = distribution(values)
    result["bootstrap_95_percent_intervals_ms"] = None
    if len(values) >= 20:
        generator = random.Random(20260930)
        estimates = {"p50": [], "p90": [], "p95": []}
        for _ in range(1000):
            sample = generator.choices(values, k=len(values))
            for name, probability in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95)):
                estimates[name].append(quantile(sample, probability))
        result["bootstrap_95_percent_intervals_ms"] = {name: [quantile(samples, 0.025), quantile(samples, 0.975)] for name, samples in estimates.items()}
    return result


def campaign_groups(records):
    groups = defaultdict(list)
    for row in records:
        if row["phase"] != "measured":
            continue
        key = (row["window"], row["client_region"], row["deployment"]["region"], row["deployment"]["model"], row["deployment"]["sku"], row["profile"]["name"], row["condition"]["api"], row["condition"]["stream"])
        groups[key].append(row)
    if not groups:
        raise ValueError("No measured campaign workflows found")
    summaries = []
    for key, rows in sorted(groups.items()):
        calls = [call for row in rows for call in row["calls"]]
        correct = [row for row in rows if row["success"]]
        correct_calls = [call for call in calls if call["success"]]
        failures = defaultdict(int)
        for call in calls:
            if not call["success"]:
                failures[failure_detail(call)["category"]] += 1
        summary = dict(zip(("window", "client_region", "resource_region", "model", "sku", "profile", "api", "stream"), key))
        summary.update(started_utc=min(row["started_utc"] for row in rows), ended_utc=max(row["ended_utc"] for row in rows),
                       attempted_workflows=len(rows), correct_workflows=len(correct), attempted_calls=len(calls), correct_calls=len(correct_calls),
                       failure_categories=dict(failures), rate_limited=sum(call.get("http_status") == 429 for call in calls),
                       workflow_ms_correct_only=distribution_with_intervals([row["workflow_ms"] for row in correct]),
                       call_ms_correct_only=distribution_with_intervals([call["duration_ms"] for call in correct_calls]),
                       first_content_ms_correct_only=distribution_with_intervals([call.get("first_content_ms") for call in correct_calls]),
                       cache_observed_calls=sum(call.get("cached_tokens") is not None for call in calls), cache_hit_calls=sum((call.get("cached_tokens") or 0) > 0 for call in calls),
                       reported_input_tokens=sum(call.get("input_tokens") or 0 for call in calls), reported_output_tokens=sum(call.get("output_tokens") or 0 for call in calls),
                       missing_usage_calls=sum(call.get("input_tokens") is None or call.get("output_tokens") is None for call in calls))
        for field in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "estimated_visible_token_interval_ms"):
            summary[field] = distribution([call.get(field) for call in correct_calls])
        summaries.append(summary)
    comparisons = []
    for key, rows in sorted(groups.items()):
        for position, left, right, label in ((1, "italynorth", "swedencentral", "Italy client minus Sweden client"), (2, "italynorth", "swedencentral", "Italy resource minus Sweden resource"), (4, "DataZoneStandard", "GlobalStandard", "EU Data Zone minus Global"), (6, "responses", "chat", "Responses minus Chat Completions")):
            other = (*key[:position], right, *key[position + 1:])
            if key[position] == left and other in groups:
                comparisons.append({"comparison": label, "left_condition": key, "right_condition": other, **paired_difference(rows, groups[other])})
    return summaries, comparisons


def analyze_campaign(roots):
    from run_campaign import profile_conditions, window_profiles
    manifests = []
    records = []
    windows = []
    planned = 0
    expected_records = set()
    for root in roots:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifests.append(manifest)
        for window_index, window in enumerate(manifest["windows"]):
            window_index = window.get("window_index", window_index)
            for deployment in manifest["config"]["deployments"]:
                for profile in window_profiles(manifest["client_region"], deployment["region"], window_index, manifest["config"].get("pilot_profiles") if manifest["pilot"] else None):
                    repeats = max(2, profile["concurrency"]) if manifest["pilot"] else manifest["config"]["repeats"]
                    planned += repeats * len(profile_conditions(profile))
                    for condition in profile_conditions(profile):
                        expected_records.update((manifest["run_id"], window["name"], deployment["label"], profile["name"], condition["api"], condition["stream"], index) for index in range(repeats))
            for state in ("started", "ended"):
                path = root / "windows" / f"{window['name']}-{state}.json"
                if path.exists():
                    windows.append({"run_id": manifest["run_id"], "state": state, **json.loads(path.read_text(encoding="utf-8"))})
        records.extend(json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "workflows").rglob("*.json")))
    run_ids = [manifest["run_id"] for manifest in manifests]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("Duplicate campaign run supplied")
    if len({manifest.get("request_protocol", "prompt-note-v1") for manifest in manifests}) != 1:
        raise ValueError("Cannot pool different campaign request protocols")
    if len(expected_records) != planned:
        raise ValueError("Campaign plan contains duplicate conditions")
    actual_records = set()
    for row in records:
        if row["phase"] not in {"measured", "warmup"}:
            raise ValueError("Unknown workflow phase")
        if row["phase"] == "measured":
            identity = (row["run_id"], row["window"], row["deployment"]["label"], row["profile"]["name"], row["condition"]["api"], row["condition"]["stream"], row["case_index"])
            if identity not in expected_records or identity in actual_records:
                raise ValueError("Unexpected or duplicate measured campaign record")
            actual_records.add(identity)
    summaries, comparisons = campaign_groups(records)
    measured = [row for row in records if row["phase"] == "measured"]
    return {"manifests": manifests, "windows": windows, "planned_workflows": planned, "attempted_workflows": len(measured), "correct_workflows": sum(row["success"] for row in measured),
            "warmup_workflows": len(records) - len(measured), "sample_count_complete": actual_records == expected_records, "missing_measured_records": len(expected_records - actual_records), "runs_supplied": run_ids,
            "peer_runs_expected": sorted({run_id for manifest in manifests for run_id in manifest["config"]["peer_run_ids"]}), "started_utc": min(row["started_utc"] for row in measured), "ended_utc": max(row["ended_utc"] for row in measured), "conditions": summaries, "comparisons": comparisons}


def validate_pilot_archives(paths, source_root):
    from run_campaign import profile_conditions, window_profiles
    receipts = []
    client_regions = set()
    configurations = []
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate archive members")
            receipt = json.loads(archive.read("storage-verification.json"))
            if receipt["all_equal"] is not True or not receipt["files"]:
                raise ValueError("Missing Blob verification")
            run_id = receipt["run_id"]
            prefix = f"results/{run_id}/"
            manifest = json.loads(archive.read(prefix + "manifest.json"))
            summary = json.loads(archive.read(prefix + "summary.json"))
            status = json.loads(archive.read("process-status.json"))
            if manifest.get("request_protocol") != "schema-constrained-note-v2" or manifest["pilot"] is not True or summary["complete"] is not True or status["exit_code"] != 0:
                raise ValueError("Pilot did not complete with the current request protocol")
            region = manifest["client_region"]
            if region in client_regions:
                raise ValueError("Duplicate pilot client region")
            client_regions.add(region)
            configurations.append(manifest["config"])
            verified_names = set()
            for entry in receipt["files"]:
                cloud_prefix = f"runs/{run_id}/"
                if not entry["name"].startswith(cloud_prefix):
                    raise ValueError("Unexpected Blob evidence prefix")
                member = prefix + entry["name"][len(cloud_prefix):]
                payload = archive.read(member).removesuffix(b"\n")
                if len(payload) != entry["bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                    raise ValueError("Pilot Blob receipt hash mismatch")
                verified_names.add(member)
            if verified_names != {name for name in names if name.startswith(prefix) and name.endswith(".json")}:
                raise ValueError("Pilot archive inventory mismatch")
            for name in ("benchmark.py", "run_benchmark.py", "run_campaign.py", "requirements.txt"):
                expected_hash = manifest["source_sha256"][name]
                if hashlib.sha256(archive.read(name)).hexdigest() != expected_hash or hashlib.sha256((source_root / name).read_bytes()).hexdigest() != expected_hash:
                    raise ValueError("Pilot sources differ from the sources to be executed")
            expected = set()
            for window_index, window in enumerate(manifest["windows"]):
                for deployment in manifest["config"]["deployments"]:
                    for profile in window_profiles(region, deployment["region"], window_index, manifest["config"].get("pilot_profiles")):
                        for condition in profile_conditions(profile):
                            expected.update((window["name"], deployment["label"], profile["name"], condition["api"], condition["stream"], index) for index in range(max(2, profile["concurrency"])))
            observed = set()
            correct_conditions = set()
            successful_calls = 0
            technical_errors = 0
            cache_hits = defaultdict(int)
            for name in sorted(verified_names):
                if not name.startswith(prefix + "workflows/"):
                    continue
                row = json.loads(archive.read(name))
                for event in [row, *row["calls"]]:
                    start = datetime.fromisoformat(event["started_utc"])
                    end = datetime.fromisoformat(event["ended_utc"])
                    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or end < start:
                        raise ValueError("Invalid actual UTC observation interval")
                for call in row["calls"]:
                    if hashlib.sha256(canonical_json(call["wire_request"]).encode()).hexdigest() != call["request_sha256"]:
                        raise ValueError("Submitted request differs from the recorded planned request")
                    technical_errors += int(call["error"] is not None)
                if row["phase"] != "measured":
                    continue
                identity = (row["window"], row["deployment"]["label"], row["profile"]["name"], row["condition"]["api"], row["condition"]["stream"], row["case_index"])
                if identity not in expected or identity in observed:
                    raise ValueError("Unexpected or duplicate pilot workflow")
                observed.add(identity)
                if row["success"]:
                    correct_conditions.add(identity[:-1])
                    successful_calls += len(row["calls"])
                cache_hits[row["profile"]["name"]] += sum((call.get("cached_tokens") or 0) > 0 for call in row["calls"])
            if observed != expected or {identity[:-1] for identity in expected} != correct_conditions or technical_errors:
                raise ValueError("Pilot coverage, correctness calibration or technical-error gate failed")
            if region == "swedencentral":
                required_profiles = {"geography", "context-32000", "generation-100", "generation-500", "generation-1500", "load-16", "cache-unique", "cache-shared"}
                if not required_profiles.issubset({identity[2] for identity in expected}) or cache_hits["cache-shared"] == 0 or cache_hits["cache-unique"] != 0:
                    raise ValueError("Representative workload or cache calibration missing")
            receipts.append({"run_id": run_id, "archive": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "verified_files": len(verified_names), "measured_workflows": len(observed), "successful_calls": successful_calls, "client_region": region})
    if client_regions != {"swedencentral", "italynorth"} or len({digest(config) for config in configurations}) != 1:
        raise ValueError("Both matching regional pilot configurations are required")
    return {"validated": True, "protocol": "schema-constrained-note-v2", "archives": receipts, "configuration_sha256": digest(configurations[0])}


def analyze(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "workflows").rglob("*.json"))]
    records = [record for record in records if record["phase"] != "warmup"]
    if not records:
        raise ValueError("No measured workflows found")
    groups = defaultdict(list)
    for record in records:
        condition = record["condition"]
        key = (record["deployment"]["model"], record["deployment"]["sku"], condition["api"], condition["auth"], condition["stream"], condition["effort"])
        groups[key].append(record)
    summaries = []
    for key, rows in sorted(groups.items()):
        correct = [row for row in rows if row["success"]]
        calls = [call for row in correct for call in row["calls"]]
        all_calls = [call for row in rows for call in row["calls"]]
        summary = {"model": key[0], "sku": key[1], "api": key[2], "auth": key[3], "stream": key[4], "effort": key[5],
                   "attempted_workflows": len(rows), "correct_workflows": len(correct),
                   "attempted_calls": len(all_calls), "correct_calls": sum(call["correct"] for call in all_calls),
                   "technical_errors": sum(call["error"] is not None for call in all_calls),
                   "incorrect_without_technical_error": sum(not call["correct"] and call["error"] is None for call in all_calls),
                   "workflow_ms_correct_only": distribution([row["workflow_ms"] for row in correct]),
                   "workflow_ms_all_attempted": distribution([row["workflow_ms"] for row in rows]),
                   "local_workflow_overhead_ms": distribution([row["workflow_ms"] - sum(call["duration_ms"] for call in row["calls"]) for row in correct]),
                   "stage1_ms": distribution([row["calls"][0]["duration_ms"] for row in correct]),
                   "stage2_ms": distribution([row["calls"][1]["duration_ms"] for row in correct])}
        for field in ["duration_ms", "first_content_ms", "headers_ms", "content_span_ms", "stream_tail_ms", "estimated_visible_token_interval_ms", "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "visible_tokens_local", "content_event_count"]:
            summary[field] = distribution([call.get(field) for call in calls])
        summary["token_provider_ms"] = distribution([duration for call in calls for duration in call["token_provider_ms"]])
        summaries.append(summary)
    comparisons = []
    for key, rows in sorted(groups.items()):
        candidates = []
        if key[1] == "DataZoneStandard":
            candidates.append(("EU minus Global", (key[0], "GlobalStandard", *key[2:])))
        if key[2] == "responses":
            candidates.append(("Responses minus Chat Completions", (*key[:2], "chat", *key[3:])))
        if key[3] == "managed_identity":
            candidates.append(("Managed Identity minus API key", (*key[:3], "key", *key[4:])))
        if key[5] == "low":
            candidates.append(("Low reasoning minus none", (*key[:5], "none")))
        for label, other in candidates:
            if other in groups:
                comparisons.append({"comparison": label, "left_condition": key, "right_condition": other, **paired_difference(rows, groups[other])})
    failures = [{"deployment": row["deployment"]["name"], "case_index": row["case_index"], "condition": row["condition"], **failure_detail(call)}
                for row in records for call in row["calls"] if not call["success"]]
    return {"manifest": manifest, "attempted_workflows": len(records), "planned_workflows": manifest["planned_measured_workflows"],
            "correct_workflows": sum(row["success"] for row in records), "conditions": summaries, "comparisons": comparisons, "failure_details": failures}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--label", default="analysis")
    parser.add_argument("--campaign", action="store_true")
    parser.add_argument("--peer-root", type=Path)
    parser.add_argument("--validate-pilot-archives", type=Path, nargs=2)
    args = parser.parse_args()
    if not args.label or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in args.label):
        raise ValueError("Invalid analysis label")
    if args.validate_pilot_archives:
        print(json.dumps(validate_pilot_archives(args.validate_pilot_archives, args.root)))
        return
    result = analyze_campaign([args.root] + ([args.peer_root] if args.peer_root else [])) if args.campaign else analyze(args.root)
    output = args.root / f"{args.label}.json"
    if output.exists():
        raise FileExistsError("Analysis already exists; preserve the previous artifact")
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.campaign:
        lines = ["# Campaign Results", "", f"{result['correct_workflows']}/{result['attempted_workflows']} attempted workflows were correct; {result['planned_workflows']} were planned. Sample-count complete: {result['sample_count_complete']}.", "", f"Observed measured interval: {result['started_utc']} to {result['ended_utc']} (UTC). Scheduled and actual window boundaries are retained separately in the JSON analysis.", "", "Latencies include only correct workflows. Failure categories and rate limits remain explicit; cache hits require positive service-reported cached tokens. JSON includes P50/P90/P95 exploratory bootstrap intervals where at least 20 observations exist, plus paired comparisons. These intervals do not correct temporal dependence or multiple comparisons.", "", "| Window | Client | Resource | Model / SKU | Profile / API / stream | Correct/attempted | Failure categories | P50 ms | P90 ms | P95 ms | Cache hit/observed calls | First UTC | Last UTC |", "|---|---|---|---|---|---|---|---:|---:|---:|---:|---|---|"]
        for row in result["conditions"]:
            duration = row["workflow_ms_correct_only"]
            formatted = ["n/a" if duration[name] is None else f"{duration[name]:.1f}" for name in ("p50", "p90", "p95")]
            lines.append(f"| {row['window']} | {row['client_region']} | {row['resource_region']} | {row['model']} / {row['sku']} | {row['profile']} / {row['api']} / {row['stream']} | {row['correct_workflows']}/{row['attempted_workflows']} | {json.dumps(row['failure_categories'])} | {' | '.join(formatted)} | {row['cache_hit_calls']}/{row['cache_observed_calls']} | {row['started_utc']} | {row['ended_utc']} |")
        (args.root / f"{args.label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"attempted": result["attempted_workflows"], "correct": result["correct_workflows"], "planned": result["planned_workflows"], "started_utc": result["started_utc"], "ended_utc": result["ended_utc"], "analysis": str(output)}))
        return
    lines = ["# Measured Benchmark Results", "", f"{result['correct_workflows']}/{result['attempted_workflows']} attempted two-call workflows produced both exact expected answers; {result['planned_workflows']} workflows were planned.", "", "Durations below describe correct workflows only. Technical failures and incorrect answers remain in the denominators and in analysis.json. Each workflow contains two dependent calls; the total is neither time to first token nor time between tokens.", "", "| Model | Deployment | API | Stream | Correct/attempted | Mean ms | P50 ms | P90 ms | Input tokens/call | Output tokens/call |", "|---|---|---|---|---:|---:|---:|---:|---:|---:|"]
    def number(value):
        return "n/a" if value is None else f"{value:.1f}"
    for row in result["conditions"]:
        duration = row["workflow_ms_correct_only"]
        lines.append(f"| {row['model']} | {row['sku']} | {row['api']} ({row['auth']}, reasoning={row['effort']}) | {row['stream']} | {row['correct_workflows']}/{row['attempted_workflows']} | {number(duration['mean'])} | {number(duration['p50'])} | {number(duration['p90'])} | {number(row['input_tokens']['mean'])} | {number(row['output_tokens']['mean'])} |")
    lines.extend(["", "## Paired Comparisons", "", "Differences are left minus right for matching case indexes where both workflows were correct. Negative values mean the left condition was faster. Intervals are exploratory paired bootstrap intervals, not service guarantees; temporal dependence and multiple comparisons are not corrected.", "", "| Comparison | Left condition | Pairs | Mean difference ms | 95% interval ms |", "|---|---|---:|---:|---|"])
    for comparison in result["comparisons"]:
        interval = comparison["bootstrap_95_percent_interval_ms"]
        interval_text = "not estimated (<20 pairs)" if interval is None else f"[{number(interval[0])}, {number(interval[1])}]"
        lines.append(f"| {comparison['comparison']} | {', '.join(map(str, comparison['left_condition']))} | {comparison['pairs']} | {number(comparison['mean_difference_ms'])} | {interval_text} |")
    if result["failure_details"]:
        lines.extend(["", "## Unsuccessful Calls", "", "| Deployment | Case | Stage | API | Stream | Classification | Differing fields |", "|---|---:|---:|---|---|---|---|"])
        for failure in result["failure_details"]:
            lines.append(f"| {failure['deployment']} | {failure['case_index']} | {failure['stage']} | {failure['condition']['api']} | {failure['condition']['stream']} | {failure['category']} | {', '.join(failure['differences']) or 'see raw observation'} |")
    lines.extend(["", "## Interpretation Limits", "", "This is a synthetic controlled workload with explicit prompts, SDK configuration and timer boundaries. One Azure host in Sweden Central supplies four concurrent workers, each sequential within its deployment. This is not a load-capacity or SLA test.", "", "Streaming first_content_ms measures the first non-empty visible content event at the client. estimated_visible_token_interval_ms divides the visible-content span by the locally tokenized visible output minus one; SSE events can contain multiple tokens, so this is not directly observed per-token TBT. Non-streaming calls cannot provide either measurement.", "", "The manifest identifies the authentication methods actually executed. Absence of an API-key comparison is not evidence of equivalence or of a Managed Identity latency penalty. New-credential token diagnostics can still benefit from platform-side token caching.", ""])
    (args.root / f"{args.label}.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"attempted": result["attempted_workflows"], "correct": result["correct_workflows"], "planned": result["planned_workflows"], "analysis": str(output)}))


if __name__ == "__main__":
    main()
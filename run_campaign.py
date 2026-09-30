from __future__ import annotations

import argparse
import base64
import concurrent.futures
from copy import deepcopy
import hashlib
import importlib.metadata
import itertools
import json
import platform
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from benchmark import SCHEMA, VALIDATION_NOTE, build_messages, canonical_json, digest, make_case, request_body
from run_benchmark import EvidenceStore, MeasuredClient, utc_now


def campaign_request(deployment: str, api: str, index: int, nonce: str, tokenizer: Any,
                     input_target: int = 1500, output_target: int = 100,
                     cache_prefix: str | None = None, streaming: bool = False,
                     stage: int = 1, previous: str | None = None) -> tuple[dict[str, Any], dict[str, Any], int]:
    if input_target not in (1500, 8000, 32000) or output_target not in (100, 500, 1500):
        raise ValueError("Unsupported campaign workload size")
    case = make_case(index)
    note = VALIDATION_NOTE if output_target == 100 else "Order validation record." + " audited" * (output_target - 76)
    expected = {**case["expected"], "stage": stage, "validation_note": note}
    schema = deepcopy(SCHEMA)
    schema["properties"]["validation_note"]["enum"] = [note]
    extra_schema_tokens = len(tokenizer.encode(canonical_json(schema))) - len(tokenizer.encode(canonical_json(SCHEMA)))
    messages = build_messages(case, stage, nonce, previous)
    messages[0]["content"] = messages[0]["content"].replace(VALIDATION_NOTE, note)
    prefix = f"Cache group {cache_prefix}. " if cache_prefix else f"Unique request {nonce}. "
    prefix += "The following neutral reference material does not change the order rules. "
    content_tokens = sum(len(tokenizer.encode(message["content"])) for message in messages)
    padding_tokens = input_target - content_tokens - len(tokenizer.encode(prefix)) - extra_schema_tokens
    if padding_tokens < 0:
        raise ValueError("Input target is smaller than the required task instructions")
    unit = " Reference material describes catalog auditing and delivery validation."
    repeats = padding_tokens // len(tokenizer.encode(unit)) + 2
    padding = tokenizer.decode(tokenizer.encode(unit * repeats)[:padding_tokens])
    messages[0]["content"] = prefix + padding + "\n" + messages[0]["content"]
    body = request_body(deployment, api, messages, streaming)
    if api == "chat":
        body["response_format"]["json_schema"]["schema"] = schema
    else:
        body["text"]["format"]["schema"] = schema
    body["max_completion_tokens" if api == "chat" else "max_output_tokens"] = max(512, output_target * 2 + 256)
    measured_content_tokens = sum(len(tokenizer.encode(message["content"])) for message in messages)
    return body, expected, measured_content_tokens


def campaign_profiles() -> list[dict[str, Any]]:
    profiles = [{"name": "geography", "suite": "geography", "input_target": 1500, "output_target": 100, "concurrency": 1, "stages": 2}]
    profiles += [{"name": f"context-{target}", "suite": "context", "input_target": target, "output_target": 100, "concurrency": 1, "stages": 1} for target in (1500, 8000, 32000)]
    profiles += [{"name": f"generation-{target}", "suite": "generation", "input_target": 8000, "output_target": target, "concurrency": 1, "stages": 1} for target in (100, 500, 1500)]
    profiles += [{"name": f"load-{concurrency}", "suite": "load", "input_target": 1500, "output_target": 100, "concurrency": concurrency, "stages": 1} for concurrency in (1, 4, 8, 16)]
    profiles += [{"name": f"cache-{mode}", "suite": "cache", "input_target": 8000, "output_target": 100, "concurrency": 1, "stages": 1, "cache_mode": mode} for mode in ("unique", "shared")]
    return profiles


def campaign_windows(start_utc: str, first_window_index: int = 0, window_count: int = 3) -> list[dict[str, Any]]:
    started = datetime.fromisoformat(start_utc)
    if started.tzinfo is None or started.utcoffset() != timedelta(0):
        raise ValueError("The campaign start must include an explicit UTC offset")
    if type(first_window_index) is not int or type(window_count) is not int or not 0 <= first_window_index <= 2 or not 1 <= window_count <= 3 - first_window_index:
        raise ValueError("Invalid campaign window range")
    return [{"name": f"window-{first_window_index + position}", "window_index": first_window_index + position, "scheduled_start_utc": (started + timedelta(hours=12 * position)).isoformat(), "offset_hours": 12 * position} for position in range(window_count)]


def window_profiles(client_region: str, resource_region: str, window_index: int, selected: list[str] | None = None) -> list[dict[str, Any]]:
    profiles = campaign_profiles()
    profiles = profiles if window_index == 0 and client_region == resource_region == "swedencentral" else profiles[:1]
    return [profile for profile in profiles if selected is None or profile["name"] in selected]


def profile_conditions(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"api": api, "stream": streaming, "effort": "none", "auth": "managed_identity"} for api, streaming in itertools.product(("chat", "responses"), (False, True) if profile["suite"] == "geography" else (True,))]


def run_campaign_workflow(client: Any, deployment: dict[str, Any], condition: dict[str, Any], profile: dict[str, Any], index: int,
                          run_id: str, window_name: str, tokenizer: Any, barrier: Any = None) -> dict[str, Any]:
    started_utc = utc_now()
    started = time.perf_counter()
    calls = []
    previous = None
    preparation_ms = 0.0
    barrier_wait_ms = 0.0
    for stage in range(1, profile["stages"] + 1):
        preparation_started = time.perf_counter()
        nonce = digest([run_id, window_name, deployment["label"], profile["name"], condition, index, stage])[:32]
        prefix = digest([run_id, window_name, deployment["label"], condition])[:32] if profile.get("cache_mode") == "shared" else None
        body, expected, content_tokens = campaign_request(deployment["name"], condition["api"], index, nonce, tokenizer, profile["input_target"], profile["output_target"], prefix, condition["stream"], stage, previous)
        schema = body["response_format"]["json_schema"]["schema"] if condition["api"] == "chat" else body["text"]["format"]["schema"]
        schema_tokens = len(tokenizer.encode(canonical_json(schema)))
        preparation_ms += (time.perf_counter() - preparation_started) * 1000
        if stage == 1 and barrier is not None:
            barrier_started = time.perf_counter()
            barrier.wait(timeout=60)
            barrier_wait_ms = (time.perf_counter() - barrier_started) * 1000
        call = client.invoke(body, condition["api"], expected)
        call.update(stage=stage, prepared_content_tokens=content_tokens, prepared_schema_tokens=schema_tokens)
        calls.append(call)
        if not call["success"]:
            break
        previous = call["output_text"]
    ended = time.perf_counter()
    return {"run_id": run_id, "window": window_name, "case_index": index, "case_sha256": digest(make_case(index)), "profile": profile,
            "condition": condition, "deployment": deployment, "started_utc": started_utc, "ended_utc": utc_now(), "workflow_ms": (ended - started) * 1000,
            "preparation_ms": preparation_ms, "barrier_wait_ms": barrier_wait_ms, "calls": calls,
            "success": len(calls) == profile["stages"] and all(call["success"] for call in calls)}


def wait_until(target: float, deadline: float) -> float:
    started = time.monotonic()
    if target > deadline:
        raise TimeoutError("The next scheduled operation exceeds the technical deadline")
    delay = max(0.0, target - started)
    threading.Event().wait(delay)
    if time.monotonic() > deadline:
        raise TimeoutError("Campaign technical deadline exceeded")
    return (time.monotonic() - started) * 1000


def run_deployment(config: dict[str, Any], deployment: dict[str, Any], client_region: str, window: dict[str, Any], window_index: int,
                   run_id: str, store: EvidenceStore, tokenizer: Any, pilot: bool, deadline: float, advanced: bool = False) -> dict[str, Any]:
    totals = {"attempted": 0, "correct": 0, "calls": 0, "technical_errors": 0, "rate_limited": 0, "incorrect": 0}
    profiles = window_profiles(client_region, deployment["region"], window_index, config.get("pilot_profiles") if pilot else None)[1:] if advanced else campaign_profiles()[:1]
    next_admission = time.monotonic()
    for profile in profiles:
        profile_started = utc_now()
        conditions = profile_conditions(profile)
        repeats = max(2, profile["concurrency"]) if pilot else config["repeats"]
        concurrency = min(profile["concurrency"], repeats)
        clients = []
        results = []
        try:
            for _ in range(concurrency):
                clients.append(MeasuredClient(deployment["endpoint"], "managed_identity", config["identity_client_id"], "", tokenizer))
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                for phase, count in (("warmup", concurrency), ("measured", repeats)):
                    for offset in range(0, count, concurrency):
                        shuffled = list(conditions)
                        random.Random(digest([window["name"], profile["name"], phase, offset])).shuffle(shuffled)
                        for condition in shuffled:
                            indices = list(range(offset, min(offset + concurrency, count)))
                            quota_fraction = 0.4 if profile["suite"] == "geography" else 0.8
                            estimated_tokens = (profile["input_target"] * 2 + max(512, profile["output_target"] * 2 + 256) + 256) * profile["stages"] * len(indices)
                            admission_wait_ms = wait_until(next_admission, deadline)
                            admitted = time.monotonic()
                            next_admission = admitted + max(60 * estimated_tokens / (deployment["capacity"] * 1000 * quota_fraction), 60 * len(indices) * profile["stages"] / (deployment["capacity"] * quota_fraction))
                            barrier = threading.Barrier(len(indices)) if len(indices) > 1 else None
                            futures = [pool.submit(run_campaign_workflow, clients[position], deployment, condition, profile, index if phase == "measured" else 10000 + index,
                                                   run_id, window["name"], tokenizer, barrier) for position, index in enumerate(indices)]
                            for future in futures:
                                row = future.result()
                                row.update(phase=phase, client_region=client_region, scheduled_window_start_utc=window["scheduled_start_utc"], admission_wait_ms=admission_wait_ms)
                                store.write(f"workflows/{window['name']}/{deployment['label']}/{profile['name']}/{phase}-{row['case_index']:04d}-{digest(condition)[:10]}.json", row)
                                if phase == "measured":
                                    results.append(row)
                                    totals["attempted"] += 1
                                    totals["correct"] += int(row["success"])
                                    totals["calls"] += len(row["calls"])
                                    totals["technical_errors"] += sum(call.get("error") is not None for call in row["calls"])
                                    totals["rate_limited"] += sum(call.get("http_status") == 429 for call in row["calls"])
                                    totals["incorrect"] += sum(call.get("completed", False) and not call.get("correct", False) and not call.get("refusal", False) and call.get("error") is None for call in row["calls"])
                            if offset % 16 == 0:
                                print(canonical_json({"utc": utc_now(), "window": window["name"], "deployment": deployment["label"], "profile": profile["name"], "phase": phase, "through_case": indices[-1]}), flush=True)
            store.write(f"blocks/{window['name']}-{deployment['label']}-{profile['name']}.json", {"started_utc": profile_started, "ended_utc": utc_now(), "profile": profile, "deployment": deployment["label"],
                        "attempted": len(results), "correct": sum(row["success"] for row in results), "planned": repeats * len(conditions), "complete": len(results) == repeats * len(conditions)})
        finally:
            for client in clients:
                client.close()
    return {"deployment": deployment["label"], **totals}


def wait_for_geography(store: EvidenceStore, peer_run_ids: list[str], window_name: str, deadline: float) -> None:
    for run_id in peer_run_ids:
        blob = store.remote.get_blob_client(f"runs/{run_id}/windows/{window_name}-geography.json")
        while not blob.exists():
            wait_until(time.monotonic() + 15, deadline)
        result = json.loads(blob.download_blob().readall())
        if result.get("complete") is not True:
            raise RuntimeError(f"Geographic phase did not finish for {run_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="campaign.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--client-region", choices=("swedencentral", "italynorth"), required=True)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", args.run_id):
        parser.error("Invalid run identifier")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    selected = config.get("pilot_profiles")
    if selected is not None and (not args.pilot or not isinstance(selected, list) or "geography" not in selected or len(set(selected)) != len(selected) or set(selected) - {profile["name"] for profile in campaign_profiles()}):
        parser.error("Profile subsets are allowed only for pilots and must include geography")
    if type(config["repeats"]) is not int or not 100 <= config["repeats"] <= 200:
        parser.error("The full campaign requires 100 to 200 attempted workflows per condition")
    deployments = config["deployments"]
    if len(deployments) != 8 or len({deployment["label"] for deployment in deployments}) != 8:
        parser.error("Expected eight unique regional deployments")
    expected_matrix = set(itertools.product(("swedencentral", "italynorth"), (("gpt-5.4", "2026-03-05"), ("gpt-5.4-mini", "2026-03-17")), ("GlobalStandard", "DataZoneStandard")))
    actual_matrix = {(deployment["region"], (deployment["model"], deployment["version"]), deployment["sku"]) for deployment in deployments}
    if actual_matrix != expected_matrix or len({deployment["capacity"] for deployment in deployments}) != 1 or any(deployment["capacity"] <= 0 for deployment in deployments):
        parser.error("Regional matrix, model versions or matched capacities are invalid")
    windows = campaign_windows(utc_now() if args.pilot else config["start_utc"], config.get("first_window_index", 0), config.get("window_count", 3))
    if args.pilot:
        windows = windows[:1]
    root = Path("results") / args.run_id
    if root.exists():
        parser.error("Run output already exists; use a new run ID")
    import tiktoken
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    tokenizer = tiktoken.get_encoding("o200k_base")
    started_utc = utc_now()
    snapshots = {name: Path(name).read_bytes() for name in ("benchmark.py", "run_benchmark.py", "run_campaign.py", "requirements.txt", "campaign.json", "config.json")}
    packages = {name: importlib.metadata.version(name) for name in ("openai", "httpx", "azure-identity", "azure-storage-blob", "tiktoken")}
    failures = []
    with ManagedIdentityCredential(client_id=config["identity_client_id"]) as credential, BlobServiceClient(config["storage_endpoint"], credential=credential) as service:
        store = EvidenceStore(root, service.get_container_client("benchmark"))
        manifest = {"run_id": args.run_id, "started_utc": started_utc, "client_region": args.client_region, "pilot": args.pilot, "config": config,
                    "request_protocol": "schema-constrained-note-v2",
                    "windows": windows, "profiles": campaign_profiles(), "python": platform.python_version(), "platform": platform.platform(), "packages": packages,
                    "source_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in snapshots.items()}, "sdk_retries": 0, "wall_clock": "UTC ISO 8601", "duration_clock": "time.perf_counter", "spending_cap": None}
        store.write("manifest.json", manifest)
        store.write("source-snapshot.json", {name: base64.b64encode(content).decode("ascii") for name, content in snapshots.items()})
        for window in windows:
            store.write(f"schedule/{window['name']}.json", {**window, "recorded_utc": utc_now(), "state": "scheduled"})
        for window_position, window in enumerate(windows):
            window_index = window["window_index"]
            scheduled = datetime.fromisoformat(window["scheduled_start_utc"])
            while datetime.now(timezone.utc) < scheduled:
                remaining = (scheduled - datetime.now(timezone.utc)).total_seconds()
                print(canonical_json({"utc": utc_now(), "state": "scheduled", "window": window["name"], "scheduled_start_utc": window["scheduled_start_utc"]}), flush=True)
                threading.Event().wait(min(300, max(0, remaining)))
            actual_start = utc_now()
            store.write(f"windows/{window['name']}-started.json", {**window, "started_utc": actual_start, "schedule_lateness_ms": max(0, (datetime.fromisoformat(actual_start) - scheduled).total_seconds() * 1000), "state": "running"})
            deadline = time.monotonic() + (7200 if args.pilot else 36000)
            results = []
            window_failures = []
            for advanced in (False, True):
                if advanced:
                    try:
                        wait_for_geography(store, config["peer_run_ids"], window["name"], deadline)
                    except Exception as error:
                        failure = {"utc": utc_now(), "window": window["name"], "error_type": type(error).__name__, "message": str(error)}
                        window_failures.append(failure)
                        store.write(f"errors/{window['name']}-synchronization.json", failure)
                        break
                    if window_index != 0 or args.client_region != "swedencentral":
                        break
                selected = [deployment for deployment in deployments if not advanced or deployment["region"] == "swedencentral"]
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected)) as pool:
                    tasks = {pool.submit(run_deployment, config, deployment, args.client_region, window, window_index, args.run_id, store, tokenizer, args.pilot, deadline, advanced): deployment["label"] for deployment in selected}
                    for future in concurrent.futures.as_completed(tasks):
                        try:
                            results.append({"advanced": advanced, **future.result()})
                        except Exception as error:
                            failure = {"utc": utc_now(), "window": window["name"], "deployment": tasks[future], "error_type": type(error).__name__, "message": str(error)}
                            window_failures.append(failure)
                            store.write(f"errors/{window['name']}-{tasks[future]}-{advanced}.json", failure)
                if not advanced:
                    store.write(f"windows/{window['name']}-geography.json", {"ended_utc": utc_now(), "complete": not window_failures, "results": results})
                    if window_failures:
                        break
            failures.extend(window_failures)
            complete = not window_failures
            store.write(f"windows/{window['name']}-ended.json", {**window, "started_utc": actual_start, "ended_utc": utc_now(), "complete": complete, "results": results})
            print(canonical_json({"utc": utc_now(), "window": window["name"], "complete": complete, "results": results}), flush=True)
            if not complete:
                break
        store.write("summary.json", {"run_id": args.run_id, "started_utc": started_utc, "ended_utc": utc_now(), "complete": not failures, "windows_planned": len(windows), "windows_attempted": window_position + 1, "worker_errors": failures})
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
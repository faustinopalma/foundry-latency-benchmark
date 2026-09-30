from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import itertools
import json
import os
import platform
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark import ContentTiming, build_messages, canonical_json, digest, make_case, request_body, validate_answer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_usage(usage: dict[str, Any] | None, api: str) -> dict[str, Any]:
    usage = usage or {}
    input_name, output_name = ("prompt_tokens", "completion_tokens") if api == "chat" else ("input_tokens", "output_tokens")
    input_details = usage.get(f"{input_name}_details") or {}
    output_details = usage.get(f"{output_name}_details") or {}
    return {"input_tokens": usage.get(input_name), "output_tokens": usage.get(output_name),
            "cached_tokens": input_details.get("cached_tokens"), "reasoning_tokens": output_details.get("reasoning_tokens"),
            "usage_raw": usage}


class EvidenceStore:
    def __init__(self, root: Path, remote: Any = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = remote
        self.lock = threading.Lock()

    def write(self, name: str, value: Any) -> None:
        content = canonical_json(value)
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(content + "\n")
            if self.remote:
                self.remote.upload_blob(name=f"runs/{self.root.name}/{name}", data=content, overwrite=False)


class MeasuredClient:
    def __init__(self, endpoint: str, auth: str, identity_client_id: str, key: str, tokenizer: Any):
        import httpx
        from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
        from openai import OpenAI

        self.current: dict[str, Any] = {}
        self.auth = auth
        self.tokenizer = tokenizer
        self.credential = None
        if auth == "managed_identity":
            self.credential = ManagedIdentityCredential(client_id=identity_client_id)
            provider = get_bearer_token_provider(self.credential, "https://cognitiveservices.azure.com/.default")

            def measured_provider() -> str:
                started = time.perf_counter()
                try:
                    return provider()
                finally:
                    self.current.setdefault("token_provider_ms", []).append((time.perf_counter() - started) * 1000)

            secret: Any = measured_provider
        else:
            secret = key
        transport = httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0),
                                 event_hooks={"request": [self.on_request], "response": [self.on_response]})
        self.client = OpenAI(base_url=endpoint, api_key=secret, max_retries=0, http_client=transport)

    def on_request(self, request: Any) -> None:
        self.current.setdefault("http_request_ms", []).append((time.perf_counter() - self.current["started"]) * 1000)
        self.current["wire_payload_sha256"] = hashlib.sha256(request.content).hexdigest()
        self.current["wire_request"] = json.loads(request.content)

    def on_response(self, response: Any) -> None:
        self.current["headers_ms"] = (time.perf_counter() - self.current["started"]) * 1000
        self.current["http_status"] = response.status_code
        allowed = {"apim-request-id", "x-request-id", "x-ms-region", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens", "retry-after", "openai-processing-ms"}
        self.current["response_headers"] = {name: value for name, value in response.headers.items() if name.lower() in allowed}

    def invoke(self, body: dict[str, Any], api: str, expected: dict[str, Any]) -> dict[str, Any]:
        self.current = {"started": time.perf_counter(), "started_utc": utc_now(), "token_provider_ms": []}
        timing = ContentTiming(self.current["started"])
        usage = None
        finish = None
        response_id = None
        returned_model = None
        refusal = False
        stream_error = None
        event_types: dict[str, int] = {}
        completed = False
        try:
            method = self.client.chat.completions.create if api == "chat" else self.client.responses.create
            result = method(**body)
            if body["stream"]:
                with result as stream:
                    for event in stream:
                        observed = time.perf_counter()
                        if api == "chat":
                            event_types["chat.chunk"] = event_types.get("chat.chunk", 0) + 1
                            response_id = event.id
                            returned_model = event.model
                            if event.usage:
                                usage = event.usage.model_dump()
                            for choice in event.choices:
                                timing.observe(choice.delta.content, observed)
                                refusal = refusal or bool(getattr(choice.delta, "refusal", None))
                                if choice.finish_reason:
                                    finish = choice.finish_reason
                                    completed = finish == "stop"
                        else:
                            event_types[event.type] = event_types.get(event.type, 0) + 1
                            if event.type == "response.output_text.delta":
                                timing.observe(event.delta, observed)
                            elif event.type == "response.refusal.delta":
                                refusal = True
                            elif event.type in {"response.completed", "response.incomplete", "response.failed"}:
                                final = event.response
                                response_id = final.id
                                returned_model = final.model
                                usage = final.usage.model_dump() if final.usage else None
                                finish = final.status
                                completed = event.type == "response.completed" and finish == "completed"
                            elif event.type == "error":
                                stream_error = {"code": getattr(event, "code", None), "message": getattr(event, "message", None)}
            else:
                response_id = result.id
                returned_model = result.model
                usage = result.usage.model_dump() if result.usage else None
                if api == "chat":
                    choice = result.choices[0]
                    timing.observe(choice.message.content, time.perf_counter())
                    refusal = bool(choice.message.refusal)
                    finish = choice.finish_reason
                    completed = finish == "stop"
                else:
                    timing.observe(result.output_text, time.perf_counter())
                    finish = result.status
                    completed = finish == "completed"
                    refusal = any(content.type == "refusal" for output in result.output if output.type == "message" for content in output.content)
            ended = time.perf_counter()
            error = stream_error
        except Exception as exception:
            ended = time.perf_counter()
            error = {"type": type(exception).__name__, "status_code": getattr(exception, "status_code", None)}
            error_body = getattr(exception, "body", None)
            if isinstance(error_body, dict):
                error["service_error"] = error_body.get("error", error_body)
        ended_utc = utc_now()
        text = "".join(timing.fragments)
        visible_tokens = len(self.tokenizer.encode(text))
        result_record = {key: value for key, value in self.current.items() if key != "started"}
        result_record.update(timing.finish(ended, visible_tokens, body["stream"]))
        result_record.update(normalize_usage(usage, api))
        result_record.update(validate_answer(text, expected))
        result_record.update(response_id=response_id, returned_model=returned_model, finish_reason=finish,
                             ended_utc=ended_utc,
                             refusal=refusal, error=error, completed=completed, output_text=text,
                             visible_tokens_local=visible_tokens, event_types=event_types,
                             content_events=[{"ms": (stamp - timing.started) * 1000, "characters": len(fragment)} for stamp, fragment in zip(timing.timestamps, timing.fragments)] if body["stream"] else [],
                             request_sha256=digest(body), expected=expected)
        result_record["success"] = completed and error is None and not refusal and result_record["correct"] and usage is not None
        return result_record

    def close(self) -> None:
        self.client.close()
        if self.credential:
            self.credential.close()


def prepare_messages(case: dict[str, Any], stage: int, nonce: str, previous: str | None, tokenizer: Any) -> list[dict[str, str]]:
    messages = build_messages(case, stage, nonce, previous)
    count = sum(len(tokenizer.encode(message["content"])) for message in messages)
    if count < 1380:
        padding = tokenizer.decode(tokenizer.encode(" Catalog reference information is provided only for the requested order." * 150)[:1380 - count])
        messages[0]["content"] += padding
    return messages


def run_workflow(client: MeasuredClient, deployment: dict[str, Any], condition: dict[str, Any], index: int,
                 phase: str, run_id: str, tokenizer: Any) -> dict[str, Any]:
    case = make_case(index)
    calls = []
    previous = None
    started = time.perf_counter()
    started_utc = utc_now()
    for stage in (1, 2):
        nonce = digest([run_id, deployment["name"], condition, index, phase, stage])[:32]
        messages = prepare_messages(case, stage, nonce, previous, tokenizer)
        expected = {**case["expected"], "stage": stage}
        body = request_body(deployment["name"], condition["api"], messages, condition["stream"], condition["effort"])
        if condition["effort"] != "none":
            body["max_completion_tokens" if condition["api"] == "chat" else "max_output_tokens"] = 2048
        record = client.invoke(body, condition["api"], expected)
        record["stage"] = stage
        calls.append(record)
        if not record["success"]:
            break
        previous = record["output_text"]
    ended = time.perf_counter()
    return {"run_id": run_id, "phase": phase, "case_index": index, "case_sha256": digest(case),
            "deployment": deployment, "condition": condition, "started_utc": started_utc,
            "ended_utc": utc_now(),
            "workflow_ms": (ended - started) * 1000, "calls": calls,
            "success": len(calls) == 2 and all(call["success"] for call in calls)}


def auth_diagnostic(client_id: str, repeats: int) -> list[dict[str, Any]]:
    from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
    rows = []
    for index in range(repeats):
        started = time.perf_counter()
        with ManagedIdentityCredential(client_id=client_id) as credential:
            constructed = time.perf_counter()
            provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
            first = time.perf_counter()
            token = provider()
            acquired = time.perf_counter()
            cached = provider()
            ended = time.perf_counter()
            rows.append({"index": index, "constructor_ms": (constructed - started) * 1000,
                         "first_provider_ms": (acquired - first) * 1000, "cached_provider_ms": (ended - acquired) * 1000,
                         "same_token": token == cached})
    return rows


def main() -> None:
    import tiktoken
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["pilot", "main", "sensitivity"], required=True)
    parser.add_argument("--repeats", type=int, default=60)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 60:
        raise ValueError("Repeat count must be between 1 and 60")
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    client_id = os.environ["AZURE_CLIENT_ID"]
    auth_mode = os.environ.get("BENCHMARK_AUTH", "both")
    if auth_mode not in {"both", "managed_identity"}:
        raise ValueError("Unknown authentication selection")
    auth_modes = ["key", "managed_identity"] if auth_mode == "both" else ["managed_identity"]
    key = os.environ.get("BENCHMARK_API_KEY", "")
    if not client_id or ("key" in auth_modes and not key):
        raise ValueError("Selected authentication modes must be configured")
    tokenizer = tiktoken.get_encoding("o200k_base")
    storage_credential = ManagedIdentityCredential(client_id=client_id)
    service = BlobServiceClient(config["storage_endpoint"], credential=storage_credential)
    remote = service.get_container_client("benchmark")
    store = EvidenceStore(Path("results") / args.run_id, remote)
    deployments = config["deployments"]
    conditions = [{"api": api, "auth": auth, "stream": streaming, "effort": "none"}
                  for api, auth, streaming in itertools.product(["chat", "responses"], auth_modes, [False, True])]
    if args.phase == "sensitivity":
        deployments = [deployment for deployment in deployments if deployment["sku"] == "DataZoneStandard"]
        conditions = [{"api": api, "auth": "managed_identity", "stream": True, "effort": effort}
                      for api, effort in itertools.product(["chat", "responses"], ["none", "low"])]
    manifest = {"run_id": args.run_id, "started_utc": utc_now(), "phase": args.phase, "repeats": args.repeats,
                "config": config, "conditions": conditions, "python": platform.python_version(),
                "platform": platform.platform(), "machine": platform.machine(),
                "packages": {name: importlib.metadata.version(name) for name in ["openai", "azure-identity", "azure-storage-blob", "httpx", "tiktoken"]},
                "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ["benchmark.py", "run_benchmark.py"]},
                "workers": len(deployments), "sdk_max_retries": 0, "request_timeout_seconds": 90,
                "planned_measured_workflows": len(deployments) * len(conditions) * args.repeats}
    store.write("manifest.json", manifest)
    print(canonical_json({"event": "started", "run_id": args.run_id, "planned": manifest["planned_measured_workflows"], "packages": manifest["packages"]}), flush=True)
    store.write("auth-diagnostic.json", auth_diagnostic(client_id, 10 if args.phase == "pilot" else 30))
    deadline = time.monotonic() + 7200

    def worker(deployment: dict[str, Any]) -> dict[str, Any]:
        clients = {auth: MeasuredClient(config["endpoint"], auth, client_id, key, tokenizer) for auth in auth_modes}
        totals = {"attempted": 0, "successful": 0, "calls": 0, "technical_failures": 0}
        try:
            warmup_rounds = 0 if args.phase == "pilot" else 1
            for index in range(-warmup_rounds, args.repeats):
                phase = "warmup" if index < 0 else args.phase
                ordered = list(conditions)
                seed = int(digest([deployment["name"], index, args.phase])[:8], 16)
                random.Random(seed).shuffle(ordered)
                for condition in ordered:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Experiment exceeded two-hour execution budget")
                    record = run_workflow(clients[condition["auth"]], deployment, condition, index, phase, args.run_id, tokenizer)
                    name = f"workflows/{deployment['name']}/{phase}-{index:03d}-{digest(condition)[:12]}.json"
                    store.write(name, record)
                    if index >= 0:
                        totals["attempted"] += 1
                        totals["successful"] += int(record["success"])
                        totals["calls"] += len(record["calls"])
                        totals["technical_failures"] += sum(call["error"] is not None for call in record["calls"])
                    if totals["technical_failures"] >= 5:
                        raise RuntimeError("Stopped deployment after five technical failures")
                print(canonical_json({"event": "block_complete", "deployment": deployment["name"], "block": index, **totals}), flush=True)
            return {"deployment": deployment["name"], **totals}
        finally:
            for client in clients.values():
                client.close()

    summary: dict[str, Any] = {"run_id": args.run_id, "workers": [], "errors": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(deployments)) as pool:
        futures = {pool.submit(worker, deployment): deployment["name"] for deployment in deployments}
        for future in concurrent.futures.as_completed(futures):
            try:
                summary["workers"].append(future.result())
            except Exception as exception:
                summary["errors"].append({"deployment": futures[future], "type": type(exception).__name__, "message": str(exception)[:300]})
    summary["completed_utc"] = utc_now()
    summary["completed"] = not summary["errors"] and sum(row["attempted"] for row in summary["workers"]) == manifest["planned_measured_workflows"]
    summary["all_correct"] = summary["completed"] and all(row["attempted"] == row["successful"] for row in summary["workers"])
    store.write("completion.json", summary)
    print(canonical_json({"event": "finished", **summary}), flush=True)
    service.close()
    storage_credential.close()
    if not summary["completed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
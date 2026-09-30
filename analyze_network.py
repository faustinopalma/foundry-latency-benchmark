import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from analyze_benchmark import distribution, paired_difference


FIELDS = ("dns_ms", "tcp_connect_ms", "tls_ms", "tcp_rtt_before_tls_ms", "tcp_rtt_after_response_ms", "duration_ms", "wait_headers_after_send_ms", "body_read_ms", "token_provider_ms")


def summarize(rows):
    return {"attempted": len(rows), "http_statuses": sorted({row.get("http_status", 0) for row in rows}),
            "transport_errors": sum(row["error"] is not None for row in rows),
            "confirmed_reuse": sum(row["connection_reused"] for row in rows),
            "correct": sum(row.get("success", False) for row in rows),
            "expected_probe_response": sum(row.get("expected_response", False) for row in rows),
            "peers": sorted({row["peer"] for row in rows if "peer" in row}),
            "tls_versions": sorted({row["tls_version"] for row in rows if "tls_version" in row}),
            "connection_setup_ms": distribution([sum(row[field] for field in ("dns_ms", "tcp_connect_ms", "tls_ms")) for row in rows if all(row.get(field) is not None for field in ("dns_ms", "tcp_connect_ms", "tls_ms"))]),
            **{field: distribution([row.get(field) for row in rows]) for field in FIELDS}}


def compare(rows):
    groups = {mode: [{"case_index": row["sample"], "workflow_ms": row["duration_ms"], "success": row.get("success", row.get("expected_response", False))} for row in rows if row["mode"] == mode] for mode in ("new", "reused")}
    return paired_difference(groups["new"], groups["reused"])


def analyze(root):
    manifest = json.loads((root / "manifest.json").read_text())
    completion = json.loads((root / "completion.json").read_text())
    probes = [json.loads(path.read_text()) for path in sorted((root / "probes").glob("*.json"))]
    inference = [json.loads(path.read_text()) for path in sorted((root / "inference").glob("*.json"))]
    if len(probes) != manifest["probe_pairs"] * 2 or len(inference) != completion["measured_inference_calls"]:
        raise ValueError("Incomplete sample denominators")
    if {(row["sample"], row["mode"]) for row in probes} != {(index, mode) for index in range(manifest["probe_pairs"]) for mode in ("new", "reused")}:
        raise ValueError("Probe samples missing or duplicated")
    groups = defaultdict(list)
    for row in inference:
        groups[(row["deployment"]["model"], row["deployment"]["sku"], row["api"])].append(row)
    inference_summary = []
    for key, rows in sorted(groups.items()):
        if len(rows) != manifest["inference_repeats"] * 2 or len({(row["sample"], row["mode"]) for row in rows}) != len(rows):
            raise ValueError("Inference condition missing or duplicated")
        inference_summary.append({"model": key[0], "sku": key[1], "api": key[2],
                                  "modes": {mode: summarize([row for row in rows if row["mode"] == mode]) for mode in ("new", "reused")},
                                  "new_minus_reused_ms_correct_pairs": compare(rows)})
    if inference and len(groups) != 8:
        raise ValueError("Expected all four deployments and both APIs")
    return {"manifest": manifest, "completion": completion, "probes": {mode: summarize([row for row in probes if row["mode"] == mode]) for mode in ("new", "reused")},
            "probe_new_minus_reused_ms": compare(probes), "inference": inference_summary,
            "inference_all": summarize(inference),
            "usage": {field: sum(row.get(field) or 0 for row in inference) for field in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")},
            "incorrect_answers": [{"sample": row["sample"], "deployment": row["deployment"]["name"], "api": row["api"], "mode": row["mode"], "output": row.get("output_text"), "http_status": row.get("http_status"), "transport_error": row["error"]} for row in inference if not row["success"]]}


def verify_export(export, run_id):
    receipt = json.loads((export / "storage-verification.json").read_text())
    if receipt["run_id"] != run_id or receipt["all_equal"] is not True:
        raise ValueError("Cloud receipt identity mismatch")
    root = export / "results" / run_id
    paths = sorted(root.rglob("*.json"))
    expected = {item["name"]: item for item in receipt["files"]}
    if not paths or len(paths) != len(expected):
        raise ValueError("Cloud receipt file count mismatch")
    for path in paths:
        name = f"runs/{run_id}/{path.relative_to(root).as_posix()}"
        if hashlib.sha256(path.read_bytes().removesuffix(b"\n")).hexdigest() != expected[name]["sha256"]:
            raise ValueError(f"Cloud hash mismatch: {name}")
    if json.loads((export / "process-status.json").read_text())["exit_code"] != 0:
        raise ValueError("Remote experiment failed")
    return len(paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", default="network-local-20260930a")
    parser.add_argument("--azure", default="network-20260930a")
    parser.add_argument("--output", default="evidence/network-analysis-20260930a.json")
    args = parser.parse_args()
    export = Path("evidence") / args.azure
    count = verify_export(export, args.azure)
    result = {"local": analyze(Path("evidence") / args.local), "azure": analyze(export / "results" / args.azure), "verified_cloud_files": count,
              "archive_sha256": hashlib.sha256(Path(f"evidence/{args.azure}.zip").read_bytes()).hexdigest()}
    with Path(args.output).open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print(f"Verified {count} cloud-matched files; analyzed PC/Azure probes and {result['azure']['inference_all']['attempted']} model calls")


if __name__ == "__main__":
    main()
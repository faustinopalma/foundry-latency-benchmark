from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import random
import socket
import ssl
import struct
import time
from pathlib import Path
from urllib.parse import urlsplit

from benchmark import canonical_json, digest, make_case, request_body, validate_answer
from run_benchmark import EvidenceStore, normalize_usage, prepare_messages, utc_now


def linux_rtt_ms(connection):
    if platform.system() != "Linux" or connection is None:
        return None
    try:
        info = connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
        return struct.unpack_from("=I", info, 68)[0] / 1000
    except (OSError, AttributeError, struct.error):
        return None


class TimedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        started = time.perf_counter()
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        self.measurement["dns_ms"] = (time.perf_counter() - started) * 1000
        self.measurement["resolved_addresses"] = sorted({item[4][0] for item in addresses})
        family, kind, protocol, _, address = addresses[0]
        raw = socket.socket(family, kind, protocol)
        raw.settimeout(self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            started = time.perf_counter()
            raw.connect(address)
            self.measurement["tcp_connect_ms"] = (time.perf_counter() - started) * 1000
            self.measurement["peer"] = raw.getpeername()[0]
            self.measurement["tcp_rtt_before_tls_ms"] = linux_rtt_ms(raw)
            started = time.perf_counter()
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.measurement["tls_ms"] = (time.perf_counter() - started) * 1000
            self.measurement["tls_version"] = self.sock.version()
            self.measurement["tls_session_reused"] = self.sock.session_reused
        except Exception:
            raw.close()
            raise

    def measure(self, method, path, body=None, bearer=None):
        payload = canonical_json(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "foundry-network-benchmark/1", "Cache-Control": "no-cache"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        self.measurement = {"started_utc": utc_now(), "dns_ms": None, "tcp_connect_ms": None, "tls_ms": None,
                            "connected_before_request": self.sock is not None, "request_bytes": len(payload or b""), "error": None}
        started = time.perf_counter()
        content = b""
        try:
            self.request(method, path, payload, headers)
            sent = time.perf_counter()
            response = self.getresponse()
            received_headers = time.perf_counter()
            content = response.read()
            ended = time.perf_counter()
            self.measurement.update(http_status=response.status, response_bytes=len(content), response_sha256=hashlib.sha256(content).hexdigest(),
                                    send_complete_ms=(sent - started) * 1000, response_headers_ms=(received_headers - started) * 1000,
                                    wait_headers_after_send_ms=(received_headers - sent) * 1000, body_read_ms=(ended - received_headers) * 1000,
                                    tcp_rtt_after_response_ms=linux_rtt_ms(self.sock), connection_open_after_response=self.sock is not None,
                                    response_headers={name.lower(): value for name, value in response.getheaders() if name.lower() in
                                                      {"apim-request-id", "x-request-id", "x-ms-region", "openai-processing-ms", "server-timing", "retry-after"}})
        except Exception as error:
            ended = time.perf_counter()
            self.measurement["error"] = {"type": type(error).__name__, "message": str(error)}
            self.close()
        self.measurement["duration_ms"] = (ended - started) * 1000
        self.measurement["connection_reused"] = self.measurement["connected_before_request"] and self.measurement["tcp_connect_ms"] is None and self.measurement["error"] is None
        return dict(self.measurement), content


def run(config, store, samples, inference_repeats, vantage):
    endpoint = urlsplit(config["endpoint"])
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    started = time.perf_counter()
    generator = random.Random(20260930)

    def connection():
        return TimedHTTPSConnection(endpoint.hostname, 443, timeout=30, context=context)

    store.write("manifest.json", {"started_utc": utc_now(), "vantage": vantage, "platform": platform.platform(), "python": platform.python_version(),
                                "openssl": ssl.OPENSSL_VERSION, "endpoint": config["endpoint"], "probe_pairs": samples, "inference_repeats": inference_repeats,
                                "protocol": "HTTP/1.1", "proxy_mode": "direct socket; environment proxy settings not used", "retries": 0,
                                "sources": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ["run_network.py", "benchmark.py", "run_benchmark.py"]},
                                "limits": ["DNS includes operating-system and recursive resolver caches; no cache flush", "TCP connect approximates one RTT plus host scheduling", "TLS includes round trips and cryptography", "Lightweight HTTP includes gateway processing", "Linux TCP_INFO RTT is a smoothed transport estimate to the TCP peer, not hidden model routing", "Inference timer excludes token acquisition, request construction and answer validation"]})
    warm = connection()
    initial, _ = warm.measure("GET", endpoint.path + "models")
    store.write("probe-warmup.json", initial)
    for index in range(samples):
        modes = ["new", "reused"]
        generator.shuffle(modes)
        for mode in modes:
            active = connection() if mode == "new" else warm
            record, _ = active.measure("GET", endpoint.path + "models")
            record.update(sample=index, mode=mode, kind="unauthenticated_models_probe", expected_status=401)
            record["expected_response"] = record.get("http_status") == 401 and record["error"] is None
            store.write(f"probes/{index:03d}-{mode}.json", record)
            if mode == "new":
                active.close()
        if time.perf_counter() - started > 900:
            raise TimeoutError("Network probe budget exceeded")
    warm.close()
    if inference_repeats:
        import tiktoken
        from azure.identity import ManagedIdentityCredential, get_bearer_token_provider

        tokenizer = tiktoken.get_encoding("o200k_base")
        with ManagedIdentityCredential(client_id=config["identity_client_id"]) as credential:
            provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
            clients = {(deployment["name"], api): connection() for deployment in config["deployments"] for api in ("chat", "responses")}
            conditions = [(deployment, api, mode) for deployment in config["deployments"] for api in ("chat", "responses") for mode in ("new", "reused")]
            try:
                for index in range(-1, inference_repeats):
                    order = list(conditions)
                    generator.shuffle(order)
                    for deployment, api, mode in order:
                        if index == -1 and mode == "new":
                            continue
                        case = make_case(max(index, 0))
                        nonce = digest([store.root.name, index, deployment["name"], api, mode])[:32]
                        messages = prepare_messages(case, 1, nonce, None, tokenizer)
                        body = request_body(deployment["name"], api, messages, False, "none")
                        token_started = time.perf_counter()
                        token = provider()
                        token_ms = (time.perf_counter() - token_started) * 1000
                        active = connection() if mode == "new" else clients[(deployment["name"], api)]
                        record, content = active.measure("POST", endpoint.path + ("chat/completions" if api == "chat" else "responses"), body, token)
                        record.update(sample=index, mode=mode, api=api, deployment=deployment, token_provider_ms=token_ms, request=body, correct=False)
                        try:
                            result = json.loads(content)
                            text = result["choices"][0]["message"]["content"] if api == "chat" else "".join(part["text"] for output in result["output"] if output["type"] == "message" for part in output["content"] if part["type"] == "output_text")
                            record.update(validate_answer(text, case["expected"]))
                            record.update(normalize_usage(result.get("usage"), api))
                            record["output_text"] = text
                            record["completed"] = result["choices"][0]["finish_reason"] == "stop" if api == "chat" else result["status"] == "completed"
                        except (ValueError, KeyError, TypeError, IndexError) as error:
                            record["parse_error"] = type(error).__name__
                        record["success"] = record["correct"] and record.get("completed", False) and record["error"] is None and record.get("http_status") == 200
                        folder = "warmup" if index < 0 else "inference"
                        store.write(f"{folder}/{index:03d}-{deployment['name']}-{api}-{mode}.json", record)
                        if mode == "new":
                            active.close()
                        if record["error"] or record.get("http_status") != 200:
                            raise RuntimeError("Inference transport or HTTP failure; stopped without retry")
                        if time.perf_counter() - started > 1800:
                            raise TimeoutError("Campaign budget exceeded")
            finally:
                for client in clients.values():
                    client.close()
    store.write("completion.json", {"completed_utc": utc_now(), "probe_pairs": samples, "measured_inference_calls": inference_repeats * len(config["deployments"]) * 4})
    print(f"Completed {samples} probe pairs and {inference_repeats * len(config['deployments']) * 4} inference calls", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--inference-repeats", type=int, default=0)
    parser.add_argument("--cloud", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 <= args.inference_repeats <= 30:
        raise ValueError("Sample count outside bounded experiment")
    config = json.loads(Path("config.json").read_text(encoding="utf-8-sig"))
    root = Path("results" if args.cloud else "evidence") / args.run_id
    if root.exists():
        raise FileExistsError(root)
    if args.cloud:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        with ManagedIdentityCredential(client_id=config["identity_client_id"]) as credential:
            with BlobServiceClient(config["storage_endpoint"], credential=credential) as service:
                run(config, EvidenceStore(root, service.get_container_client("benchmark")), args.samples, args.inference_repeats, "Azure Sweden Central ACI")
    else:
        if args.inference_repeats:
            raise ValueError("Managed identity inference is restricted to the Azure runner")
        run(config, EvidenceStore(root), args.samples, 0, "Local Windows PC")


if __name__ == "__main__":
    main()
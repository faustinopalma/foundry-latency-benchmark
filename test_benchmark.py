import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmark import ContentTiming, build_messages, make_case, quantile, request_body, validate_answer
from run_benchmark import EvidenceStore, MeasuredClient, normalize_usage, run_workflow
from analyze_benchmark import distribution, failure_detail, paired_difference
from run_network import TimedHTTPSConnection, linux_rtt_ms
from analyze_network import summarize, compare
from run_campaign import campaign_profiles, campaign_request, campaign_windows, profile_conditions, run_campaign_workflow, run_deployment, wait_for_geography, window_profiles


class CharacterTokenizer:
    def encode(self, text):
        return list(text.encode("utf-8"))

    def decode(self, tokens):
        return bytes(tokens).decode("utf-8")


class BenchmarkTests(unittest.TestCase):
    def test_evidence_transfer_stays_below_terminal_line_limit_and_retries(self):
        import base64
        import hashlib
        from export_evidence import stream_archive
        payload = bytes(range(256)) * 30
        with patch("builtins.print") as output, patch("builtins.input", side_effect=["NEXT 0", "RETRY", "NEXT 1", "NEXT 2", "NEXT 3"]):
            stream_archive(payload)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertTrue(all(len(line) < 4096 for line in lines))
        self.assertEqual(lines[0], "EVIDENCE_BEGIN " + hashlib.sha256(payload).hexdigest())
        self.assertEqual(lines[-1], "EVIDENCE_END")
        chunks = {}
        for line in lines[1:-1]:
            marker, index, expected_hash, encoded, end = line.split()
            self.assertEqual((marker, end), ("CHUNK", "ENDCHUNK"))
            chunk = base64.urlsafe_b64decode(encoded)
            self.assertEqual(hashlib.sha256(chunk).hexdigest(), expected_hash)
            chunks[int(index)] = chunk
        self.assertEqual(len(lines[1:-1]), 5)
        self.assertEqual(b"".join(chunks[index] for index in range(4)), payload)
        with patch("builtins.print") as output, patch("builtins.input", return_value="RETRY"):
            with self.assertRaisesRegex(RuntimeError, "could not be transferred intact"):
                stream_archive(payload)
            self.assertNotIn("EVIDENCE_END", [call.args[0] for call in output.call_args_list])

    def test_public_final_export_requires_receipt_integrity_and_process_state(self):
        import hashlib
        import zipfile
        from build_preview import load_snapshot
        payload = b'{"request_protocol":"schema-constrained-note-v2","pilot":false}'
        receipt = {"run_id": "unit", "all_equal": True, "inventory_scope": "final_full_inventory", "files": [{"name": "runs/unit/manifest.json", "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}]}
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unit.zip"
            for scenario in ("valid", "tampered", "missing status", "snapshot receipt", "unrecorded cancellation"):
                with self.subTest(scenario=scenario):
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr("results/unit/manifest.json", payload if scenario != "tampered" else payload + b" ")
                        archive.writestr("storage-verification.json", json.dumps({**receipt, "inventory_scope": "captured_blob_inventory" if scenario == "snapshot receipt" else "final_full_inventory"}))
                        if scenario != "missing status":
                            archive.writestr("process-status.json", json.dumps({"exit_code": -15 if scenario == "unrecorded cancellation" else 0}))
                    if scenario == "valid":
                        run_id, rows, source = load_snapshot(archive_path)
                        self.assertEqual((run_id, rows, source["verified_records"], source["scope"], source["process_exit_code"]), ("unit", [], 1, "final_export", 0))
                    else:
                        with self.assertRaises(ValueError):
                            load_snapshot(archive_path)

    def test_complete_public_campaign_requires_exact_revised_matrix(self):
        from itertools import product
        from build_preview import validate_completed_campaign
        rows = []
        for window, runner, resource, model, sku in product(("window-0", "window-1"), ("swedencentral", "italynorth"), ("swedencentral", "italynorth"), ("gpt-5.4", "gpt-5.4-mini"), ("GlobalStandard", "DataZoneStandard")):
            profiles = campaign_profiles() if window == "window-0" and runner == resource == "swedencentral" else campaign_profiles()[:1]
            for profile in profiles:
                for api, stream, case_index in product(("chat", "responses"), (False, True) if profile["name"] == "geography" else (True,), range(100)):
                    rows.append({"phase": "measured", "window": window, "client_region": runner, "deployment": {"region": resource, "model": model, "sku": sku}, "profile": profile, "condition": {"api": api, "stream": stream}, "case_index": case_index})
        sources = [{"scope": "final_export"} for _ in range(4)]
        self.assertEqual(len(rows), 22400)
        validate_completed_campaign(rows, sources)
        for name, invalid in (("missing", rows[:-1]), ("duplicate", rows[:-1] + rows[:1]), ("extra", rows + rows[:1]), ("overnight only", [row for row in rows if row["window"] == "window-0"])):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "exactly once"):
                validate_completed_campaign(invalid, sources)
        for invalid_sources in (sources[:3], [{"scope": "captured_snapshot"}] + sources[1:]):
            with self.assertRaisesRegex(ValueError, "four final verified exports"):
                validate_completed_campaign(rows, invalid_sources)

    def test_snapshot_freezes_verified_bytes_while_run_is_active(self):
        import io
        import zipfile
        from export_evidence import build_archive
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / 'results' / 'test'
            root.mkdir(parents=True)
            original = b'{"run_id":"test"}'
            (root / 'manifest.json').write_bytes(original + b'\n')
            (root / 'pending.json').write_bytes(b'{')
            for name in ['benchmark.py', 'run_benchmark.py', 'requirements.txt', 'config.json']:
                (workspace / name).write_text('{}', encoding='utf-8')
            container = Mock()
            container.list_blobs.return_value = [SimpleNamespace(name='runs/test/manifest.json')]

            def download(name):
                (root / 'later.json').write_text('{}', encoding='utf-8')
                (root / 'manifest.json').write_bytes(b'{"changed":true}')
                return SimpleNamespace(readall=lambda: original)

            container.download_blob.side_effect = download
            payload = build_archive(workspace, 'test', container, 'morning')
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.read('results/test/manifest.json'), original + b'\n')
                self.assertNotIn('results/test/later.json', archive.namelist())
                self.assertNotIn('results/test/pending.json', archive.namelist())
                self.assertNotIn('process-status.json', archive.namelist())
                snapshot = json.loads(archive.read('snapshot.json'))
                self.assertTrue(snapshot['preliminary'])
                self.assertFalse(snapshot['full_run_inventory_claimed'])
                self.assertEqual(snapshot['verified_records'], 1)
                self.assertTrue(snapshot['inventory_started_utc'].endswith('+00:00'))
                receipt = json.loads(archive.read('storage-verification.json'))
                self.assertEqual(receipt['inventory_scope'], 'captured_blob_inventory')
                self.assertTrue(receipt['all_equal'])

    def test_snapshot_refuses_mismatched_evidence_and_invalid_labels(self):
        from export_evidence import build_archive
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / 'results' / 'test'
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text('{}', encoding='utf-8')
            container = Mock()
            container.list_blobs.return_value = [SimpleNamespace(name='runs/test/manifest.json')]
            container.download_blob.return_value.readall.return_value = b'{"different":true}'
            with self.assertRaisesRegex(RuntimeError, 'Cloud evidence mismatch'):
                build_archive(workspace, 'test', container, 'morning')
            with self.assertRaisesRegex(ValueError, 'Invalid snapshot label'):
                build_archive(workspace, 'test', container, '../overwrite')

    def test_final_export_still_requires_finished_process_and_full_inventory(self):
        from export_evidence import build_archive
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / 'results' / 'test'
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text('{}', encoding='utf-8')
            container = Mock()
            container.list_blobs.return_value = [SimpleNamespace(name='runs/test/manifest.json')]
            with self.assertRaisesRegex(RuntimeError, 'final export refused'):
                build_archive(workspace, 'test', container)
            (workspace / 'process-status.json').write_text('{"exit_code":0}', encoding='utf-8')
            (root / 'extra.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'inventories differ'):
                build_archive(workspace, 'test', container)

    def test_campaign_analysis_rejects_duplicate_case_records(self):
        import tempfile
        from pathlib import Path
        from analyze_benchmark import analyze_campaign
        manifest = {"run_id": "unit-it", "client_region": "italynorth", "pilot": True, "windows": [{"name": "window-0"}],
                    "config": {"deployments": [{"label": "it-mini", "region": "italynorth"}], "peer_run_ids": ["unit-it"]}}
        row = {"run_id": "unit-it", "window": "window-0", "phase": "measured", "deployment": {"label": "it-mini"}, "profile": {"name": "geography"}, "condition": {"api": "chat", "stream": False}, "case_index": 0}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "workflows").mkdir()
            for name in ("first", "duplicate"):
                (root / "workflows" / f"{name}.json").write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected or duplicate"):
                analyze_campaign([root])

    def test_campaign_analysis_counts_single_followup_as_geography_only(self):
        import tempfile
        from pathlib import Path
        from analyze_benchmark import analyze_campaign
        manifest = {"run_id": "unit-sc", "client_region": "swedencentral", "pilot": False, "windows": campaign_windows("2026-10-01T05:30:00+00:00", 1, 1),
                    "config": {"deployments": [{"label": "sc-mini", "region": "swedencentral"}], "peer_run_ids": ["unit-sc"], "repeats": 100}}
        row = {"run_id": "unit-sc", "window": "window-1", "phase": "measured", "deployment": {"label": "sc-mini"}, "profile": {"name": "geography"}, "condition": {"api": "chat", "stream": False}, "case_index": 0,
               "success": True, "started_utc": "2026-10-01T05:30:00+00:00", "ended_utc": "2026-10-01T05:30:01+00:00"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "workflows").mkdir()
            (root / "workflows" / "first.json").write_text(json.dumps(row), encoding="utf-8")
            with patch("analyze_benchmark.campaign_groups", return_value=([], [])):
                result = analyze_campaign([root])
            self.assertEqual(result["planned_workflows"], 400)
            self.assertEqual(result["missing_measured_records"], 399)

    def test_public_preview_drops_private_fields_and_error_text(self):
        from build_preview import public_workflow
        call = {"success": False, "correct": False, "completed": False, "schema_valid": False, "error": {"message": "private sentinel"}, "refusal": False, "stage": 1, "duration_ms": 100, "wire_request": {"secret": "private sentinel"}}
        row = {"run_id": "private sentinel", "phase": "measured", "window": "window-0", "client_region": "swedencentral", "deployment": {"region": "italynorth", "model": "gpt-5.4", "sku": "GlobalStandard", "endpoint": "private sentinel"},
               "profile": {"name": "geography"}, "condition": {"api": "chat", "stream": False}, "started_utc": "2026-09-30T20:00:00+00:00", "ended_utc": "2026-09-30T20:00:01+00:00", "success": False, "workflow_ms": 100, "case_index": 0, "calls": [call]}
        public = public_workflow(row)
        self.assertNotIn("private sentinel", json.dumps(public))
        self.assertEqual(public["calls"][0]["error"], {})
        self.assertNotIn("run_id", public)
        self.assertNotIn("wire_request", public["calls"][0])
        row["client_region"] = "private sentinel"
        with self.assertRaises(ValueError):
            public_workflow(row)

    def test_campaign_analysis_keeps_failures_cache_and_windows_distinct(self):
        from analyze_benchmark import campaign_groups
        call = {"success": True, "correct": True, "completed": True, "schema_valid": True, "error": None, "refusal": False, "http_status": 200,
                "stage": 1, "expected": {"answer": 1}, "output_text": '{"answer":1}', "duration_ms": 100, "input_tokens": 1500, "output_tokens": 100, "cached_tokens": 0}
        row = {"phase": "measured", "window": "window-0", "client_region": "swedencentral", "deployment": {"region": "swedencentral", "model": "gpt-5.4", "sku": "DataZoneStandard"},
               "profile": {"name": "geography"}, "condition": {"api": "chat", "stream": False}, "started_utc": "2026-09-30T23:59:59+00:00", "ended_utc": "2026-10-01T00:00:01+00:00",
               "success": True, "workflow_ms": 100, "case_index": 0, "calls": [call]}
        bad = {**row, "case_index": 1, "success": False, "calls": [{**call, "success": False, "correct": False, "output_text": '{"answer":2}', "cached_tokens": None}]}
        peer = {**row, "client_region": "italynorth", "calls": [{**call, "cached_tokens": 1024}]}
        summaries, comparisons = campaign_groups([row, bad, peer, {**row, "window": "window-1"}, {**row, "phase": "warmup"}])
        self.assertEqual(len(summaries), 3)
        baseline = next(summary for summary in summaries if summary["client_region"] == "swedencentral" and summary["window"] == "window-0")
        self.assertEqual(baseline["attempted_workflows"], 2)
        self.assertEqual(baseline["correct_workflows"], 1)
        self.assertEqual(baseline["failure_categories"], {"incorrect_fields": 1})
        self.assertEqual(baseline["cache_observed_calls"], 1)
        self.assertEqual(baseline["cache_hit_calls"], 0)
        self.assertEqual(baseline["workflow_ms_correct_only"]["n"], 1)
        self.assertEqual(baseline["ended_utc"], "2026-10-01T00:00:01+00:00")
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["pairs"], 1)
        self.assertEqual(comparisons[0]["right_attempted"], 2)

    def test_percentile_intervals_require_sufficient_correct_observations(self):
        from analyze_benchmark import distribution_with_intervals
        self.assertIsNone(distribution_with_intervals([10] * 19)["bootstrap_95_percent_intervals_ms"])
        self.assertEqual(distribution_with_intervals([10] * 20)["bootstrap_95_percent_intervals_ms"], {"p50": [10, 10], "p90": [10, 10], "p95": [10, 10]})

    def test_campaign_concurrent_workers_have_independent_clients(self):
        import threading
        profile = {"name": "load-4", "suite": "load", "input_target": 8000, "output_target": 100, "concurrency": 4, "stages": 1}
        clients = []
        records = {}

        def create_client(*args):
            client = Mock()
            lock = threading.Lock()

            def invoke(body, api, expected):
                self.assertTrue(lock.acquire(blocking=False), "A client was used concurrently")
                try:
                    return {"success": True, "correct": True, "completed": True, "error": None, "http_status": 200, "output_text": json.dumps(expected)}
                finally:
                    lock.release()

            client.invoke.side_effect = invoke
            clients.append(client)
            return client

        store = Mock()
        store.write.side_effect = lambda name, row: records.setdefault(name, row)
        deployment = {"label": "sc-mini-eu", "name": "mini-eu", "endpoint": "https://example.invalid", "region": "swedencentral", "capacity": 100}
        with patch("run_campaign.MeasuredClient", side_effect=create_client), patch("run_campaign.window_profiles", return_value=[campaign_profiles()[0], profile]), patch("run_campaign.wait_until", return_value=0), patch("builtins.print"):
            result = run_deployment({"identity_client_id": "identity", "repeats": 100}, deployment, "swedencentral", {"name": "window-0", "scheduled_start_utc": "2026-09-30T20:00:00+00:00"}, 0, "test-sc", store, CharacterTokenizer(), True, float("inf"), True)
        self.assertEqual(len(clients), 4)
        self.assertEqual([client.invoke.call_count for client in clients], [4, 4, 4, 4])
        self.assertTrue(all(client.close.call_count == 1 for client in clients))
        self.assertEqual(result["attempted"], 8)
        self.assertEqual(result["correct"], 8)
        self.assertEqual(len(records), 17)

    def test_geographic_gate_rejects_incomplete_peer(self):
        store = Mock()
        blob = store.remote.get_blob_client.return_value
        blob.exists.return_value = True
        blob.download_blob.return_value.readall.return_value = b'{"complete":false}'
        with self.assertRaises(RuntimeError):
            wait_for_geography(store, ["peer"], "window-0", 100)
        blob.download_blob.return_value.readall.return_value = b'{"complete":true}'
        wait_for_geography(store, ["peer"], "window-0", 100)

    def test_campaign_schedule_covers_two_calendar_days_in_utc(self):
        windows = campaign_windows("2026-09-30T20:00:00+00:00")
        self.assertEqual([window["scheduled_start_utc"] for window in windows], ["2026-09-30T20:00:00+00:00", "2026-10-01T08:00:00+00:00", "2026-10-01T20:00:00+00:00"])
        with self.assertRaises(ValueError):
            campaign_windows("2026-09-30T20:00:00")
        with self.assertRaises(ValueError):
            campaign_windows("2026-09-30T20:00:00+02:00")

    def test_single_followup_window_preserves_geography_only(self):
        windows = campaign_windows("2026-10-01T05:30:00+00:00", first_window_index=1, window_count=1)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["name"], "window-1")
        self.assertEqual(windows[0]["scheduled_start_utc"], "2026-10-01T05:30:00+00:00")
        self.assertEqual([profile["name"] for profile in window_profiles("swedencentral", "swedencentral", windows[0]["window_index"])], ["geography"])
        for first, count in ((-1, 1), (1, 0), (1, 3), (True, 1), (1, False), (3, 1)):
            with self.subTest(first=first, count=count), self.assertRaises(ValueError):
                campaign_windows("2026-10-01T05:30:00+00:00", first, count)

    def test_advanced_campaign_blocks_do_not_contaminate_later_geographic_windows(self):
        self.assertEqual(len(window_profiles("swedencentral", "swedencentral", 0)), 13)
        for client, region, window in (("italynorth", "swedencentral", 0), ("swedencentral", "westeurope", 0), ("swedencentral", "swedencentral", 1)):
            self.assertEqual(len(window_profiles(client, region, window)), 1)
        self.assertEqual(len(profile_conditions(campaign_profiles()[0])), 4)
        self.assertEqual(len(profile_conditions(campaign_profiles()[1])), 2)

    def test_campaign_workflow_stops_after_incorrect_first_call(self):
        client = Mock()
        client.invoke.return_value = {"success": False, "output_text": "wrong answer"}
        profile = {**campaign_profiles()[0], "input_target": 8000}
        row = run_campaign_workflow(client, {"name": "deployment", "label": "model-eu"}, {"api": "chat", "stream": False}, profile, 7, "test", "window-0", CharacterTokenizer())
        self.assertEqual(client.invoke.call_count, 1)
        self.assertFalse(row["success"])
        self.assertEqual(len(row["calls"]), 1)
        self.assertTrue(row["started_utc"].endswith("+00:00"))
        self.assertTrue(row["ended_utc"].endswith("+00:00"))

    def test_calls_record_utc_start_and_end_on_success_and_failure(self):
        expected = make_case(1)["expected"]
        for failed in (False, True):
            with self.subTest(failed=failed):
                client = MeasuredClient.__new__(MeasuredClient)
                client.tokenizer = CharacterTokenizer()
                client.client = Mock()
                client.client.responses.create.return_value = SimpleNamespace(id="response", model="model", usage=Mock(model_dump=lambda: {"input_tokens": 1500, "output_tokens": 100}), output_text=json.dumps(expected), status="completed", output=[])
                if failed:
                    client.client.responses.create.side_effect = TimeoutError()
                dates = ["2026-09-30T23:59:59.900000+00:00", "2026-10-01T00:00:00.150000+00:00"]
                clock = [10.0, 10.25] if failed else [10.0, 10.2, 10.25]
                with patch("run_benchmark.utc_now", side_effect=dates), patch("run_benchmark.time.perf_counter", side_effect=clock):
                    record = client.invoke({"stream": False}, "responses", expected)
                self.assertEqual(record["started_utc"], dates[0])
                self.assertEqual(record["ended_utc"], dates[1])
                self.assertEqual(record["duration_ms"], 250)
                self.assertEqual(record["success"], not failed)

    def test_workflow_records_calendar_boundaries(self):
        client = Mock()
        client.invoke.return_value = {"success": True, "output_text": "actual answer"}
        dates = ["2026-09-30T23:59:58+00:00", "2026-10-01T00:00:02+00:00"]
        with patch("run_benchmark.utc_now", side_effect=dates), patch("run_benchmark.time.perf_counter", side_effect=[10, 14]):
            record = run_workflow(client, {"name": "deployment"}, {"api": "chat", "stream": False, "effort": "none"}, 1, "main", "test", CharacterTokenizer())
        self.assertEqual(record["started_utc"], dates[0])
        self.assertEqual(record["ended_utc"], dates[1])
        self.assertEqual(record["workflow_ms"], 4000)

    def test_campaign_prefix_is_shared_only_for_cache_treatment(self):
        tokenizer = CharacterTokenizer()
        first, expected, _ = campaign_request("deployment", "chat", 1, "nonce-one", tokenizer, 8000, cache_prefix="group")
        second, _, _ = campaign_request("deployment", "chat", 2, "nonce-two", tokenizer, 8000, cache_prefix="group")
        self.assertEqual(first["messages"][0]["content"][:2048], second["messages"][0]["content"][:2048])
        cold, _, _ = campaign_request("deployment", "chat", 1, "nonce-three", tokenizer, 8000)
        self.assertNotEqual(first["messages"][0]["content"][:100], cold["messages"][0]["content"][:100])
        self.assertTrue(validate_answer(json.dumps(expected), expected)["correct"])

    def test_campaign_output_length_preserves_business_fields(self):
        from benchmark import SCHEMA, canonical_json
        body, expected, count = campaign_request("deployment", "responses", 7, "nonce", CharacterTokenizer(), 32000, 1500)
        for field, value in make_case(7)["expected"].items():
            if field != "validation_note":
                self.assertEqual(expected[field], value)
        self.assertGreater(len(expected["validation_note"]), 1000)
        self.assertIn(expected["validation_note"], body["input"][0]["content"])
        self.assertGreater(body["max_output_tokens"], 1500)
        schema = body["text"]["format"]["schema"]
        self.assertEqual(schema["properties"]["validation_note"]["enum"], [expected["validation_note"]])
        self.assertNotIn("enum", SCHEMA["properties"]["validation_note"])
        extra = len(CharacterTokenizer().encode(canonical_json(schema))) - len(CharacterTokenizer().encode(canonical_json(SCHEMA)))
        self.assertLess(abs(count + extra - 32000), 5)

    def test_pilot_profile_subset_preserves_geography_and_requested_calibration(self):
        selected = ["geography", "generation-100", "generation-500", "generation-1500"]
        self.assertEqual([profile["name"] for profile in window_profiles("swedencentral", "swedencentral", 0, selected)], selected)
        self.assertEqual([profile["name"] for profile in window_profiles("italynorth", "swedencentral", 0, selected)], ["geography"])

    def test_campaign_stage_two_uses_actual_previous_answer(self):
        body, expected, _ = campaign_request("deployment", "chat", 1, "nonce", CharacterTokenizer(), 8000, stage=2, previous="actual response")
        self.assertEqual(json.loads(body["messages"][1]["content"])["previous_result"], "actual response")
        self.assertEqual(expected["stage"], 2)

    def test_campaign_profiles_cover_each_requested_axis(self):
        profiles = campaign_profiles()
        self.assertEqual(len(profiles), len({profile["name"] for profile in profiles}))
        self.assertEqual({profile["suite"] for profile in profiles}, {"geography", "context", "generation", "load", "cache"})
        self.assertEqual({profile["concurrency"] for profile in profiles if profile["suite"] == "load"}, {1, 4, 8, 16})

    def test_network_measurement_keeps_wait_separate_from_setup(self):
        client = TimedHTTPSConnection("example.invalid")
        response = Mock(status=401)
        response.read.return_value = b"{}"
        response.getheaders.return_value = []
        with patch.object(client, "request"), patch.object(client, "getresponse", return_value=response), patch("run_network.time.perf_counter", side_effect=[10, 10.1, 10.5, 10.6]):
            record, _ = client.measure("GET", "/")
        self.assertAlmostEqual(record["duration_ms"], 600)
        self.assertAlmostEqual(record["wait_headers_after_send_ms"], 400)
        self.assertAlmostEqual(record["body_read_ms"], 100)
        self.assertIsNone(record["tcp_connect_ms"])
        self.assertFalse(record["connection_reused"])

    def test_linux_rtt_is_kernel_microseconds_and_unavailable_is_missing(self):
        import struct
        transport = Mock()
        transport.getsockopt.return_value = b"\0" * 68 + struct.pack("=I", 3250) + b"\0" * 32
        with patch("run_network.platform.system", return_value="Linux"), patch("run_network.socket.TCP_INFO", 11, create=True):
            self.assertEqual(linux_rtt_ms(transport), 3.25)
            transport.getsockopt.side_effect = OSError("unsupported")
            self.assertIsNone(linux_rtt_ms(transport))
        self.assertIsNone(linux_rtt_ms(None))

    def test_metadata_and_empty_chunks_are_not_first_content(self):
        timing = ContentTiming(10.0)
        timing.observe(None, 10.1)
        timing.observe("", 10.2)
        timing.observe("abc", 10.5)
        timing.observe("def", 10.8)
        result = timing.finish(11.0, 4, True)
        self.assertAlmostEqual(result["first_content_ms"], 500)
        self.assertAlmostEqual(result["duration_ms"], 1000)
        self.assertAlmostEqual(result["estimated_visible_token_interval_ms"], 100)
        self.assertAlmostEqual(result["stream_tail_ms"], 200)
        self.assertEqual(result["content_event_count"], 2)

    def test_nonstreaming_cannot_measure_first_token_or_token_interval(self):
        timing = ContentTiming(1.0)
        timing.observe("complete response", 4.0)
        result = timing.finish(4.0, 10, False)
        self.assertEqual(result["duration_ms"], 3000)
        self.assertIsNone(result["first_content_ms"])
        self.assertIsNone(result["estimated_visible_token_interval_ms"])

    def test_single_content_event_cannot_measure_token_interval(self):
        timing = ContentTiming(0.0)
        timing.observe("all tokens at once", 1.0)
        self.assertIsNone(timing.finish(1.1, 20, True)["estimated_visible_token_interval_ms"])

    def test_wrong_answer_is_not_success_even_when_json_is_valid(self):
        expected = make_case(5)["expected"]
        wrong = {**expected, "total": expected["total"] + 1}
        self.assertEqual(validate_answer(json.dumps(wrong), expected), {"json_valid": True, "schema_valid": True, "correct": False})
        self.assertTrue(validate_answer(json.dumps(expected), expected)["correct"])
        self.assertFalse(validate_answer(json.dumps({**expected, "stage": True}), expected)["schema_valid"])
        self.assertFalse(validate_answer("refusal", expected)["correct"])

    def test_second_call_uses_actual_output(self):
        case = make_case(1)
        with self.assertRaises(ValueError):
            build_messages(case, 2, "nonce")
        messages = build_messages(case, 2, "nonce", "actual first answer")
        self.assertEqual(json.loads(messages[1]["content"])["previous_result"], "actual first answer")

    def test_api_requests_preserve_equivalent_settings(self):
        messages = build_messages(make_case(1), 1, "nonce")
        chat = request_body("deployment", "chat", messages, True)
        responses = request_body("deployment", "responses", messages, True)
        self.assertEqual(chat["messages"], responses["input"])
        self.assertEqual(chat["response_format"]["json_schema"]["schema"], responses["text"]["format"]["schema"])
        self.assertEqual(chat["max_completion_tokens"], responses["max_output_tokens"])
        self.assertEqual(chat["reasoning_effort"], responses["reasoning"]["effort"])
        self.assertFalse(chat["store"])
        self.assertFalse(responses["store"])
        self.assertNotIn("previous_response_id", responses)

    def test_pooled_percentile_is_computed_from_observations(self):
        self.assertEqual(quantile([0, 10, 20, 30, 100], 0.5), 20)
        self.assertAlmostEqual(quantile([0, 10, 20, 30, 100], 0.9), 72)
        self.assertIsNone(quantile([], 0.5))

    def test_usage_keeps_hidden_reasoning_and_cached_tokens_separate(self):
        chat = normalize_usage({"prompt_tokens": 1500, "completion_tokens": 120, "prompt_tokens_details": {"cached_tokens": 1000}, "completion_tokens_details": {"reasoning_tokens": 20}}, "chat")
        response = normalize_usage({"input_tokens": 1500, "output_tokens": 120, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens_details": {"reasoning_tokens": 20}}, "responses")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens"):
            self.assertEqual(chat[name], response[name])
        self.assertIsNone(normalize_usage(None, "chat")["input_tokens"])

    def test_evidence_refuses_to_overwrite_an_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = EvidenceStore(Path(folder))
            store.write("observation.json", {"duration": 15})
            with self.assertRaises(FileExistsError):
                store.write("observation.json", {"duration": 12})
            self.assertEqual(json.loads((Path(folder) / "observation.json").read_text()), {"duration": 15})

    def test_paired_comparison_preserves_sign_and_attempted_denominators(self):
        left = [{"case_index": index, "workflow_ms": index + 100, "success": True} for index in range(30)]
        right = [{"case_index": index, "workflow_ms": index + 80, "success": True} for index in range(30)]
        result = paired_difference(left, right)
        self.assertEqual(result["bootstrap_95_percent_interval_ms"], [20, 20])
        left[0]["success"] = False
        result = paired_difference(left, right)
        self.assertEqual(result["pairs"], 29)
        self.assertEqual(result["left_attempted"], 30)
        self.assertEqual(result["right_attempted"], 30)
        self.assertEqual(result["mean_difference_ms"], 20)

    def test_missing_measurements_are_not_zero_latency(self):
        result = distribution([None, None])
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["mean"])
        self.assertIsNone(result["p90"])
        self.assertIsNone(paired_difference([], [])["bootstrap_95_percent_interval_ms"])

    def test_failure_details_distinguish_wrong_values_from_transport_errors(self):
        call = {"output_text": '{"total":21}', "expected": {"total": 20}, "error": None,
                "refusal": False, "completed": True, "schema_valid": True, "correct": False, "stage": 1}
        detail = failure_detail(call)
        self.assertEqual(detail["category"], "incorrect_fields")
        self.assertEqual(detail["differences"]["total"], {"expected": 20, "actual": 21, "missing": False})
        call["error"] = {"type": "timeout"}
        self.assertEqual(failure_detail(call)["category"], "technical_error")
        transport_rows = [dict(sample=0, mode="new", http_status=200, error=None, connection_reused=False, duration_ms=120, success=False),
                  dict(sample=0, mode="reused", http_status=200, error=None, connection_reused=True, duration_ms=80, success=True)]
        summary = summarize(transport_rows)
        self.assertEqual((summary["correct"], summary["attempted"]), (1, 2))
        self.assertEqual(summary["connection_setup_ms"]["n"], 0)
        self.assertEqual(compare(transport_rows)["pairs"], 0)


if __name__ == "__main__":
    unittest.main()
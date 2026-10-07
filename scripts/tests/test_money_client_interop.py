import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).absolute().parents[2]
spec = importlib.util.spec_from_file_location("money_client_interop", ROOT / "scripts/money_client_interop.py")
interop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(interop)


class InteropTest(unittest.TestCase):
    def setUp(self):
        build = interop.owned(ROOT, Path("build"))
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="interop-unit-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_id = "a" * 32
        self.expected = interop.transport(self.run_id, [
            {"wire": {"amount": "synthetic-first", "currency": "INR"}},
            {"wire": {"amount": "synthetic-second", "currency": "JPY"}},
        ])

    def rejected(self, value, cause, **kwargs):
        with self.assertRaisesRegex(interop.InteropError, "^" + cause + "$"):
            interop.validate_transport(value, self.expected, **kwargs)


class CatalogTest(InteropTest):
    def test_loading_a_pinned_helper_does_not_write_into_its_source_inventory(self):
        path = self.root / "helper.py"
        path.write_bytes(b"answer = 42\n")
        with mock.patch.object(sys, "dont_write_bytecode", False):
            self.assertEqual(interop.module_at("synthetic_helper", path).answer, 42)
        self.assertEqual(set(interop.inventory(self.root)), {"helper.py"})

    def test_loading_a_pinned_helper_does_not_execute_unbound_cached_bytecode(self):
        path = self.root / "helper.py"
        path.write_bytes(b"answer = 42\n")
        metadata = path.stat()
        py_compile.compile(str(path), doraise=True)
        path.write_bytes(b"answer = 43\n")
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(interop.module_at("synthetic_helper", path).answer, 43)

    def test_offline_acquisition_evidence_cannot_claim_a_different_source_or_mode(self):
        record = {
            "schema": "pennilogic.api-money-client-acquisition/1", "source_ref": interop.SOURCE,
            "source_tree": interop.SOURCE_TREE, "catalog_sha256": interop.CATALOG_SHA256,
            "inputs": 39, "mode": "offline-hash-and-git-blob-verified", "requests": 0, "reused_provider_inputs": 0,
        }
        path = self.root / interop.AREA / "acquisition.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(interop.json_bytes(record))
        interop.verify_acquisition(self.root)
        for key, value in (("source_ref", "0" * 40), ("mode", "authenticated-bypass"), ("inputs", 0), ("requests", True)):
            changed = {**record, key: value}
            path.write_bytes(interop.json_bytes(changed))
            with self.subTest(key=key):
                with self.assertRaises(interop.InteropError):
                    interop.verify_acquisition(self.root)

    def test_new_catalog_binds_all_three_generators_without_changing_the_old_catalog(self):
        data = interop.catalog()
        bindings = interop.source_bindings(data)
        self.assertEqual(len(bindings), 39)
        old = interop.sources.validate_catalog(interop.sources.CATALOG)
        self.assertEqual(len(interop.sources.input_bindings(old)), 11)
        for entry in old["sources"][0]["files"]:
            self.assertEqual(bindings[entry["path"]], entry)
        for language in interop.LANGUAGES:
            self.assertIn("generator/" + language + ".json", bindings)
            self.assertTrue(any(name.startswith("runtime/" + language + "/") for name in bindings))
        self.assertIn("smoke/python/requirements.txt", bindings)
        self.assertIn("package-lock.json", bindings)
        self.assertIn("smoke/kotlin/gradle/verification-metadata.xml", bindings)

    def test_catalog_tampering_wrong_source_and_versions_never_repin_themselves(self):
        target = self.root / interop.FIXTURES / "source-inputs.json"
        target.parent.mkdir(parents=True)
        original = json.loads((ROOT / interop.FIXTURES / "source-inputs.json").read_bytes())
        for field in ("schema", "consumer", "sources"):
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data[field] = None
                target.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(interop.InteropError, "catalog-binding"):
                    interop.catalog(self.root)

    def test_catalog_line_endings_are_portable_but_source_hashes_are_not_relaxed(self):
        target = self.root / interop.FIXTURES / "source-inputs.json"
        target.parent.mkdir(parents=True)
        raw = (ROOT / interop.FIXTURES / "source-inputs.json").read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(raw.replace(b"\n", b"\r\n"))
        self.assertEqual(len(interop.catalog(self.root)["sources"][0]["files"]), 39)
        with self.assertRaises(interop.sources.MaterializationError):
            interop.sources.check_bytes(b"tampered", interop.catalog()["sources"][0]["files"][0])

    def test_acquisition_reuses_native_bounded_clients_and_exact_provider_inputs(self):
        data = interop.catalog()
        clients = []

        class Client:
            def __init__(self, catalog):
                self.catalog = catalog
                self.budget = mock.Mock(requests=3 + len(catalog["sources"][0]["files"]), bytes=100)
                clients.append(self)

        class Snapshot:
            root = interop.SOURCE_TREE

            def __init__(self, client, source, organization):
                self.source = source
                if organization != 335295566:
                    raise AssertionError("organization")

            def blob(self, entry):
                return entry["path"].encode("ascii")

        with mock.patch.object(interop.sources, "verify_inputs"), mock.patch.object(interop.sources, "verify_provider"), \
                mock.patch.object(interop.sources, "ReadOnlyClient", Client), \
                mock.patch.object(interop.sources, "GitSnapshot", Snapshot), \
                mock.patch.object(interop.sources, "check_bytes") as check, \
                mock.patch.object(interop, "read_file", return_value=b"verified-shared-input"):
            contents, record = interop.acquire(data, self.root)
        self.assertEqual(len(contents), 39)
        self.assertEqual(check.call_count, 10)
        self.assertEqual([len(client.catalog["sources"][0]["files"]) for client in clients], [28, 1])
        self.assertEqual(record["requests"], 35)
        self.assertEqual(record["mode"], "native-public-git-object-get")
        self.assertTrue(all(client.budget.requests <= interop.sources.MAX_REQUESTS for client in clients))

    def test_acquisition_rejects_an_unexpected_tree_before_downloading_a_blob(self):
        data = interop.catalog()
        client = mock.Mock()
        snapshot = mock.Mock(root="0" * 40)
        with mock.patch.object(interop.sources, "verify_inputs"), mock.patch.object(interop.sources, "verify_provider"), \
                mock.patch.object(interop.sources, "ReadOnlyClient", return_value=client), \
                mock.patch.object(interop.sources, "GitSnapshot", return_value=snapshot), \
                mock.patch.object(interop.sources, "check_bytes"), \
                mock.patch.object(interop, "read_file", return_value=b"verified-shared-input"):
            with self.assertRaisesRegex(interop.InteropError, "source-tree-binding"):
                interop.acquire(data, self.root)
        snapshot.blob.assert_not_called()

    def test_immutable_source_inventory_never_accepts_an_extra_executable(self):
        folder = self.root / "snapshot"
        folder.mkdir()
        (folder / "fixture.txt").write_bytes(b"fixture")
        entries = {"fixture.txt": {"bytes": 7, "sha256": interop.digest(b"fixture")}}
        interop.sources.verify_directory(self.root, Path("snapshot"), entries)
        (folder / "injected.py").write_bytes(b"raise SystemExit(0)\n")
        with self.assertRaises(interop.sources.MaterializationError):
            interop.sources.verify_directory(self.root, Path("snapshot"), entries)

    def test_default_generation_is_checked_before_the_accepted_generator_can_replace_it(self):
        source = self.root / interop.AREA / "producer"
        interop.verify_default_outputs(self.root, source, None)
        base = source / "build"
        for language in interop.LANGUAGES:
            (base / "generated" / language).mkdir(parents=True)
        (base / "generated-verify").mkdir()
        with mock.patch.object(interop, "generated") as verify:
            interop.verify_default_outputs(self.root, source, None)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual([call.kwargs for call in verify.call_args_list], [{"scratch": False}] * 3)
        with mock.patch.object(interop, "generated", side_effect=interop.InteropError("default-generation-tampered")):
            with self.assertRaisesRegex(interop.InteropError, "default-generation-tampered"):
                interop.verify_default_outputs(self.root, source, None)
        (base / "generated-verify" / "partial").mkdir()
        with self.assertRaisesRegex(interop.InteropError, "default-output-partial"):
            interop.verify_default_outputs(self.root, source, None)

    def test_partial_or_unknown_default_output_is_not_silently_deleted(self):
        source = self.root / interop.AREA / "producer"
        base = source / "build"
        (base / "generated" / "kotlin").mkdir(parents=True)
        with self.assertRaisesRegex(interop.InteropError, "default-output-partial"):
            interop.verify_default_outputs(self.root, source, None)
        (base / "unowned.txt").write_bytes(b"preserve")
        with self.assertRaisesRegex(interop.InteropError, "default-output-inventory"):
            interop.verify_default_outputs(self.root, source, None)
        self.assertEqual((base / "unowned.txt").read_bytes(), b"preserve")


class TransportTest(InteropTest):
    def test_seeded_rows_need_no_invented_names_to_retain_canonical_order(self):
        values = [
            {"wire": {"amount": "synthetic", "currency": ("INR", "JPY", "KWD")[index % 3]}, "minor_units": "synthetic"}
            for index in range(interop.COUNT)
        ]
        for index in range(30):
            values[index]["name"] = "boundary-" + str(index)
        data = {
            "schema_version": 1, "generated_count": 10000, "boundary_count": 30, "count": interop.COUNT,
            "round_trip_sha256": interop.CANONICAL_SHA256, "values": values,
        }
        with mock.patch.object(interop, "read_file", return_value=interop.json_bytes(data)), \
                mock.patch.object(interop, "digest", return_value=interop.CORPUS_SHA256):
            actual = interop.corpus(self.root)
        self.assertEqual(len(actual["values"]), interop.COUNT)
        self.assertEqual(sum("name" in row for row in actual["values"]), 30)

    def test_exact_identity_order_currency_and_strings_are_preserved(self):
        self.assertIs(interop.validate_transport(self.expected, self.expected), self.expected)

    def test_missing_malformed_truncated_duplicate_and_nonfinite_json_is_refused(self):
        target = self.root / "input.json"
        with self.assertRaises(FileNotFoundError):
            interop.read_json(target)
        for content in (
            b"", b"{", b'{"cases":[]', b'{"schema":1,"schema":1}',
            b'{"cases":[{"envelope":{"total":{"amount":"first","amount":"second"}}}]}',
            b'{"count":NaN}', b'{"count":Infinity}', b"\xef\xbb\xbf{}", b"\xff",
        ):
            with self.subTest(size=len(content)):
                target.write_bytes(content)
                with self.assertRaises(interop.sources.MaterializationError):
                    interop.read_json(target)

    def test_wrong_version_source_fixture_and_stale_run_are_refused(self):
        for field in ("schema", "source_ref", "fixture_sha256", "run_id"):
            with self.subTest(field=field):
                value = copy.deepcopy(self.expected)
                value[field] = "wrong"
                self.rejected(value, "transport-" + field.replace("_", "-"))

    def test_missing_extra_and_empty_evidence_is_refused(self):
        value = copy.deepcopy(self.expected)
        del value["cases"]
        self.rejected(value, "transport-shape")
        value = copy.deepcopy(self.expected)
        value["skipped"] = 0
        self.rejected(value, "transport-shape")
        for rows in ([], self.expected["cases"][:1], self.expected["cases"] * 2):
            value = copy.deepcopy(self.expected)
            value["cases"] = rows
            self.rejected(value, "transport-count")

    def test_case_counters_must_be_exact_integers_not_boolean_or_string(self):
        for count in (0, -1, True, "2", 2.0):
            value = copy.deepcopy(self.expected)
            value["case_count"] = count
            self.rejected(value, "transport-case-count")

    def test_reordering_or_duplicate_identity_is_not_a_round_trip(self):
        value = copy.deepcopy(self.expected)
        value["cases"].reverse()
        self.rejected(value, "case-order")
        value = copy.deepcopy(self.expected)
        value["cases"][1]["id"] = value["cases"][0]["id"]
        self.rejected(value, "case-order")

    def test_changed_value_currency_or_numeric_money_is_not_equality(self):
        for field, replacement in (("amount", "different"), ("currency", "KWD"), ("amount", 1), ("amount", None)):
            with self.subTest(field=field, replacement_type=type(replacement).__name__):
                value = copy.deepcopy(self.expected)
                value["cases"][0]["envelope"]["total"][field] = replacement
                self.rejected(value, "wire-disagreement")

    def test_missing_money_time_and_extra_envelope_members_are_refused(self):
        for key in ("total", "recorded_at", "booked_on"):
            value = copy.deepcopy(self.expected)
            del value["cases"][0]["envelope"][key]
            self.rejected(value, "envelope-shape")
        value = copy.deepcopy(self.expected)
        value["cases"][0]["envelope"]["unowned"] = "extra"
        self.rejected(value, "envelope-shape")

    def test_disagreement_mode_does_not_relax_identity_or_cardinality(self):
        value = copy.deepcopy(self.expected)
        value["cases"][0]["envelope"]["total"] = value["cases"][1]["envelope"]["total"]
        interop.validate_transport(value, self.expected, compare_values=False)
        value["cases"][0]["id"] = "wrong"
        self.rejected(value, "case-order", compare_values=False)

    def test_all_rejections_must_execute_and_retain_every_case_identity(self):
        rejected = copy.deepcopy(self.expected)
        rejected["schema"] = interop.REJECTIONS_SCHEMA
        rejected["cases"] = [{"id": row["id"], "status": "rejected"} for row in rejected["cases"]]
        interop.validate_transport(rejected, self.expected, rejections=True)
        for status in ("skipped", "passed", None, False):
            value = copy.deepcopy(rejected)
            value["cases"][0]["status"] = status
            self.rejected(value, "case-unexecuted", rejections=True)

    def test_no_zero_case_transport_can_qualify_itself(self):
        empty = interop.transport(self.run_id, [])
        with self.assertRaisesRegex(interop.InteropError, "transport-count"):
            interop.validate_transport(empty, empty)


class ExecutionEvidenceTest(InteropTest):
    def records(self):
        return [
            {
                "label": label, "executed": True, "readiness_probe": False,
                "elapsed_seconds": 0.01,
                "exit_code": 1 if label.endswith("-refuse-disagreement") else 0,
                "expected_exit": 1 if label.endswith("-refuse-disagreement") else 0,
                "argv": ["synthetic-command"], "cwd": str(self.root),
                "stdout": {"path": "synthetic.stdout.txt", "bytes": 0, "sha256": interop.digest(b"")},
                "stderr": {"path": "synthetic.stderr.txt", "bytes": 0, "sha256": interop.digest(b"")},
            }
            for label in interop.required_commands()
        ]

    def test_bounded_reads_allocate_for_the_file_not_the_maximum_jar_allowance(self):
        path = self.root / "fixture.txt"
        path.write_bytes(b"fixture")
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"fixture"
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(interop.read_file(path, 256 * 1024 * 1024), b"fixture")
        stream.read.assert_called_once_with(8)

    def test_a_file_growing_or_shrinking_during_a_read_is_refused(self):
        path = self.root / "fixture.txt"
        path.write_bytes(b"fixture")
        for result in (b"short", b"fixture-extra"):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.read.return_value = result
            with mock.patch.object(Path, "open", return_value=stream):
                with self.assertRaisesRegex(interop.InteropError, "input-changed"):
                    interop.read_file(path)

    def test_all_three_real_execution_legs_and_recoveries_are_mandatory(self):
        records = self.records()
        interop.validate_commands(records)
        for index in range(len(records)):
            with self.subTest(index=index):
                missing = records[:index] + records[index + 1:]
                with self.assertRaisesRegex(interop.InteropError, "commands-unexecuted"):
                    interop.validate_commands(missing)

    def test_unexecuted_failed_skipped_or_duplicate_commands_are_refused(self):
        for field, value in (("executed", False), ("exit_code", 1), ("exit_code", None), ("readiness_probe", True)):
            records = self.records()
            records[0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(interop.InteropError):
                    interop.validate_commands(records)
        records = self.records()
        records.append(records[0])
        with self.assertRaisesRegex(interop.InteropError, "commands-duplicate"):
            interop.validate_commands(records)
        records = self.records()
        records[0]["skipped"] = True
        with self.assertRaisesRegex(interop.InteropError, "commands-shape"):
            interop.validate_commands(records)

    def test_a_self_consistent_digest_cannot_hide_zero_or_skipped_runtime_receipts(self):
        for change in ({"case_count": 0}, {"case_count": 10030.0}, {"status": "skipped"}, {"run_id": "b" * 32}, {"extra": "field"}):
            with self.subTest(field=next(iter(change))):
                directory = Path("probe-" + str(len(list(self.root.iterdir()))))
                records = self.records()
                for index, record in enumerate(records):
                    prefix = directory / "commands" / f"{index:02d}-{record['label']}"
                    for name in ("stdout", "stderr"):
                        content = b""
                        if record["label"] == "backend-emit" and name == "stdout":
                            value = {**interop.receipt("backend", "emit", interop.COUNT, self.run_id), **change}
                            content = interop.json_bytes(value)
                        path = Path(str(prefix) + "." + name + ".txt")
                        interop.write_file(self.root, path, content)
                        record[name] = {"path": path.as_posix(), "bytes": len(content), "sha256": interop.digest(content)}
                    interop.write_json(self.root, Path(str(prefix) + ".json"), record)
                with self.assertRaisesRegex(interop.InteropError, "command-receipt"):
                    interop.verify_commands(self.root, directory, records, self.run_id)

    def test_protocol_comparison_does_not_conflate_booleans_floats_and_integers(self):
        self.assertTrue(interop.Runner.matches(b'{"count":0}', {"count": 0}))
        for content in (b'{"count":false}', b'{"count":0.0}', b'{"count":"0"}'):
            self.assertFalse(interop.Runner.matches(content, {"count": 0}))

    def test_planted_disagreement_must_fail_not_merely_exit(self):
        records = self.records()
        failure = next(record for record in records if record["label"].endswith("-refuse-disagreement"))
        failure["exit_code"] = failure["expected_exit"] = 0
        with self.assertRaisesRegex(interop.InteropError, "commands-failed"):
            interop.validate_commands(records)

    def test_invalid_command_timings_are_refused(self):
        for duration in (-1, True, float("inf"), float("nan"), 600, None):
            records = self.records()
            records[0]["elapsed_seconds"] = duration
            with self.assertRaisesRegex(interop.InteropError, "commands-duration"):
                interop.validate_commands(records)

    def test_runtime_errors_cannot_copy_raw_payloads_into_logs_or_reports(self):
        runner = interop.Runner(self.root, Path("run"))
        secret = b"synthetic-must-not-appear-in-evidence"
        result = subprocess.CompletedProcess(["bridge"], 1, secret, secret)
        with mock.patch.object(interop.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(interop.InteropError, "command-protocol"):
                runner.call("conversion", ["bridge"], protocol=({"status": "passed"}, None))
        self.assertIsNone(runner.records[0]["stdout"]["path"])
        self.assertIsNone(runner.records[0]["stderr"]["path"])
        self.assertEqual(runner.records[0]["stdout"]["sha256"], interop.digest(secret))
        for path in (self.root / "run").rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes())

    def test_a_missing_command_is_not_reported_as_executed_or_successful(self):
        runner = interop.Runner(self.root, Path("run"))
        with mock.patch.object(interop.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(interop.InteropError, "command-failed-missing"):
                runner.call("missing", ["not-an-installed-tool"])
        self.assertFalse(runner.records[0]["executed"])
        self.assertIsNone(runner.records[0]["exit_code"])

    def test_failure_invalidates_previous_success_before_execution(self):
        prior = self.root / interop.AREA / "current.json"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(interop.json_bytes({"schema": interop.REPORT_SCHEMA, "status": "passed", "run_id": "b" * 32}))
        result = subprocess.CompletedProcess(["worker"], 1)
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", return_value=result), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        current = interop.read_json(prior)
        self.assertEqual(current["status"], "refused")
        self.assertNotEqual(current["run_id"], "b" * 32)

    def test_an_unexecuted_worker_cannot_qualify_by_returning_zero(self):
        result = subprocess.CompletedProcess(["worker"], 0)
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", return_value=result), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json")["status"], "refused")

    def test_a_second_writer_preserves_the_active_run_and_its_lock(self):
        with interop.exclusive_run(self.root, self.run_id):
            initial = {"schema": interop.REPORT_SCHEMA, "status": "running", "run_id": self.run_id}
            interop.write_json(self.root, interop.AREA / "current.json", initial)
            with mock.patch.object(interop, "ROOT", self.root), \
                    mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(interop.main(), 1)
            self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json"), initial)
            self.assertEqual(interop.read_json(self.root / interop.AREA / "run.lock"), {"run_id": self.run_id})
        self.assertFalse((self.root / interop.AREA / "run.lock").exists())

    def test_a_stale_lock_is_refused_not_deleted_or_reused(self):
        interop.write_json(self.root, interop.AREA / "run.lock", {"run_id": "b" * 32})
        with self.assertRaisesRegex(interop.InteropError, "run-busy-or-stale-lock"):
            with interop.exclusive_run(self.root, self.run_id):
                self.fail("A stale lock was reused")
        self.assertEqual(interop.read_json(self.root / interop.AREA / "run.lock"), {"run_id": "b" * 32})

    def test_timeout_invalidates_evidence_without_touching_another_process(self):
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", side_effect=interop.process_budget.BudgetExceeded(None)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json")["status"], "refused")

    def test_linked_hardlinked_and_escaping_evidence_is_refused(self):
        original = self.root / "original.json"
        original.write_bytes(b"{}")
        os.link(original, self.root / "linked.json")
        with self.assertRaisesRegex(interop.InteropError, "input-kind"):
            interop.read_json(original)
        with self.assertRaises(interop.sources.MaterializationError):
            interop.owned(self.root, Path("../outside"))
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(interop.sources.MaterializationError):
                interop.owned(self.root, Path("output.json"))

    def test_empty_missing_or_duplicate_classpaths_cannot_qualify_a_backend(self):
        for entries in ([], ["relative.jar"], [str(self.root / "absent.jar")], [str(self.root), str(self.root)]):
            with self.assertRaises(interop.InteropError):
                interop.classpath(entries)


if __name__ == "__main__":
    unittest.main()

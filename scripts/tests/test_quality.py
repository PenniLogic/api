import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

SPEC = importlib.util.spec_from_file_location("quality", Path(__file__).resolve().parents[1] / "quality.py")
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


class GateSelfTestAdmissionCopyTest(unittest.TestCase):
    def test_real_temp_copy_verifies_admission_offline_before_the_first_gradle_call(self):
        class CopyVerified(Exception):
            pass

        calls = []

        def inspect_copy(*tasks, root, **_kwargs):
            calls.append(tasks)
            self.assertEqual(("test", "spotlessCheck"), tasks)
            self.assertNotEqual(quality.ROOT, root)
            for name in (
                "scripts/prepare_database_admission.py", "scripts/materialize_money_sources.py",
                "src/main/resources/database-admission-installation.json",
            ):
                self.assertEqual(
                    hashlib.sha256((quality.ROOT / name).read_bytes()).hexdigest(),
                    hashlib.sha256((root / name).read_bytes()).hexdigest(),
                    "the self-test copy changed its pinned executable or resource",
                )

            def snapshot(directory):
                return {
                    path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in directory.rglob("*") if path.is_file()
                }

            self.assertEqual(snapshot(quality.ROOT / "build/database-admission"), snapshot(root / "build/database-admission"))
            before = snapshot(root)
            environment = {
                key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}
            }
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(root / "scripts/prepare_database_admission.py"), "verify"],
                cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
            )
            self.assertEqual(0, result.returncode, "the real copied installation must verify without a fetch")
            self.assertFalse(result.stderr, "offline verification emitted diagnostics")
            self.assertEqual(
                {"event": "database_admission_installation", "status": "verified", "payloads": 5},
                json.loads(result.stdout),
            )
            self.assertEqual(before, snapshot(root), "offline verification mutated the self-test copy")
            raise CopyVerified

        with tempfile.TemporaryDirectory(prefix="api-admission-copy-test-") as temporary:
            with patch.object(quality, "gradle", side_effect=inspect_copy), self.assertRaises(CopyVerified):
                quality.gate_self_test(Path(temporary))
            self.assertEqual([], list(Path(temporary).iterdir()))
        self.assertEqual([("test", "spotlessCheck")], calls)


class CoverageGateTest(unittest.TestCase):
    def counters(self, covered=8, total=10):
        return {kind: {"covered": covered, "total": total} for kind in ("LINE", "BRANCH")}

    def test_exact_floors_pass(self):
        quality.enforce(self.counters(), [True] * 9 + [False])

    def test_each_repository_floor_rejects_undercoverage(self):
        for kind in ("LINE", "BRANCH"):
            with self.subTest(kind=kind):
                counters = self.counters()
                counters[kind]["covered"] = 7
                with self.assertRaisesRegex(ValueError, kind):
                    quality.enforce(counters, [True])

    def test_changed_floor_cannot_be_hidden_by_high_repository_coverage(self):
        with self.assertRaisesRegex(ValueError, "Changed"):
            quality.enforce(self.counters(10), [True] * 8 + [False] * 2)

    def test_no_changed_executable_lines_is_valid(self):
        quality.enforce(self.counters(), [])

    def test_regression_fails_even_above_floor(self):
        with self.assertRaisesRegex(ValueError, "decreased"):
            quality.enforce(self.counters(9), [True], self.counters(10))

    def test_fraction_comparison_does_not_round_away_regression(self):
        with self.assertRaisesRegex(ValueError, "decreased"):
            quality.enforce(self.counters(90000, 100001), [], self.counters(9))

    def test_invalid_counters_fail_closed(self):
        for covered, total in [(0, 0), (-1, 10), (11, 10), (8.0, 10), (True, 1)]:
            with self.subTest(covered=covered, total=total), self.assertRaises(ValueError):
                quality.enforce(self.counters(covered, total), [])

    def test_diff_handles_additions_deletions_and_single_line_hunks(self):
        diff = "@@ -2,0 +3,2 @@\n+x\n+y\n@@ -8 +10 @@\n-x\n+y\n@@ -14,2 +15,0 @@\n-x\n-y\n"
        self.assertEqual({3, 4, 10}, quality.changed_line_numbers(diff))

    def test_report_requires_real_line_and_branch_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.xml"
            path.write_text('<report><counter type="LINE" missed="0" covered="10"/></report>')
            with self.assertRaisesRegex(ValueError, "BRANCH"):
                quality.read_report(path)

    def test_report_selects_executable_lines_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.xml"
            path.write_text(
                '<report><package name="example"><sourcefile name="App.kt">'
                '<line nr="1" mi="0" ci="2"/><line nr="2" mi="1" ci="0"/>'
                '<line nr="3" mi="0" ci="0"/></sourcefile></package>'
                '<counter type="LINE" missed="1" covered="1"/>'
                '<counter type="BRANCH" missed="1" covered="1"/></report>'
            )
            counters, lines = quality.read_report(path)
            self.assertEqual({"covered": 1, "total": 2}, counters["LINE"])
            self.assertEqual({1: True, 2: False}, lines["src/main/kotlin/example/App.kt"])

    def test_coverage_requires_full_explicit_base(self):
        for base in [None, "", "main", "--help", "a" * 39]:
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "--base"):
                quality.check_coverage(base)

    @patch("sys.stdout", new_callable=io.StringIO)
    def test_missing_or_skipped_junit_tests_fail_closed(self, output):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                quality.test_metrics(root)
            reports = root / "build/test-results/test"
            reports.mkdir(parents=True)
            for counts in [
                'tests="1" skipped="1" failures="0" errors="0"',
                'tests="1" skipped="0" failures="1" errors="0"',
                'tests="1" skipped="0" failures="0" errors="1"',
            ]:
                (reports / "TEST-sample.xml").write_text(f"<testsuite {counts}/>")
                with self.subTest(counts=counts), self.assertRaises(ValueError):
                    quality.test_metrics(root)
            (reports / "TEST-sample.xml").write_text(
                '<testsuite tests="1" skipped="0" failures="0" errors="0"/>'
            )
            quality.test_metrics(root)
            self.assertEqual(
                {"event": "tests", "count": 1, "skipped": 0, "failed": 0},
                json.loads(output.getvalue().splitlines()[-1]),
            )

    def test_missing_source_in_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/main/kotlin/example"
            source.mkdir(parents=True)
            (source / "Missing.kt").write_text("package example\nclass Missing\n")
            with (
                patch.object(quality, "ROOT", root),
                patch.object(quality, "run", return_value=""),
                patch.object(quality, "read_report", return_value=(self.counters(), {})),
                self.assertRaisesRegex(ValueError, "inventory"),
            ):
                quality.check_coverage("a" * 40)


class MoneyCoverageQualificationTest(unittest.TestCase):
    def report(self, directory, classes=("Money", "CurrencyRegistry"), sources=("Money.kt", "CurrencyRegistry.kt")):
        class_nodes = "".join(f'<class name="{quality.MONEY_PACKAGE}/{name}"/>' for name in classes)
        source_nodes = "".join(f'<sourcefile name="{name}"/>' for name in sources)
        counters = (
            '<counter type="LINE" missed="3" covered="97"/>'
            '<counter type="BRANCH" missed="7" covered="93"/>'
        )
        path = Path(directory) / "money.xml"
        path.write_text(
            f'<report><package name="{quality.MONEY_PACKAGE}">{class_nodes}{source_nodes}'
            f'{counters}</package>{counters}</report>', encoding="utf-8",
        )
        return path

    def test_complete_authoritative_class_and_source_inventories_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.report(temporary)
            classes = {f"{quality.MONEY_PACKAGE}/{name}" for name in ("Money", "CurrencyRegistry")}
            sources = {"Money.kt", "CurrencyRegistry.kt"}
            counters = quality.read_money_coverage(path, classes, sources)
            self.assertEqual({"covered": 97, "total": 100}, counters["LINE"])
            self.assertEqual({"covered": 93, "total": 100}, counters["BRANCH"])
            for missing in classes:
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "class inventory"):
                    quality.read_money_coverage(path, classes - {missing}, sources)
            for missing in sources:
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "source inventory"):
                    quality.read_money_coverage(path, classes, sources - {missing})

    def test_unrelated_package_or_disagreeing_aggregate_cannot_mask_money_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.report(temporary)
            original = path.read_text(encoding="utf-8")
            classes = {f"{quality.MONEY_PACKAGE}/{name}" for name in ("Money", "CurrencyRegistry")}
            sources = {"Money.kt", "CurrencyRegistry.kt"}
            path.write_text(original.replace(quality.MONEY_PACKAGE, "unrelated/package"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authoritative package"):
                quality.read_money_coverage(path, classes, sources)
            path.write_text(original.replace('missed="3"', 'missed="0"', 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "counters differ"):
                quality.read_money_coverage(path, classes, sources)

    def test_money_thresholds_come_from_the_bound_strategy_not_local_defaults(self):
        class SyntheticProvider:
            BUNDLE = Path("synthetic")
            STRATEGY_FILE = Path("test-strategy.json")

            def __init__(self, package):
                self.package = package

            def read_strategy(self, _):
                return json.dumps({
                    "packages": [self.package], "floor_policy": {"money_path_ratchet": True},
                }).encode()

        package = {
            "id": "api.money", "repository": "PenniLogic/api", "money_path": True,
            "line_coverage_floor_percent": 97, "branch_coverage_floor_percent": 93,
            "mutation_score_floor_percent": 90,
        }
        floors, _ = quality.money_policy(SyntheticProvider(package))
        self.assertEqual({"LINE": 97, "BRANCH": 93, "MUTATION": 90}, floors)
        altered = {**package, "line_coverage_floor_percent": 99}
        self.assertEqual(99, quality.money_policy(SyntheticProvider(altered))[0]["LINE"])
        for invalid in (None, 0, 101, True, "97"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "numeric"):
                quality.money_policy(SyntheticProvider({**package, "line_coverage_floor_percent": invalid}))

    def test_exact_money_floors_pass_and_each_one_below_fails(self):
        floors = {"LINE": 97, "BRANCH": 93, "MUTATION": 90}
        counters = {
            "LINE": {"covered": 97, "total": 100},
            "BRANCH": {"covered": 93, "total": 100},
        }
        quality.enforce_money_coverage(counters, floors)
        for kind in ("LINE", "BRANCH"):
            insufficient = {**counters, kind: {"covered": counters[kind]["covered"] - 1, "total": 100}}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, kind):
                quality.enforce_money_coverage(insufficient, floors)

    def test_money_ratchet_refuses_a_decline_even_above_the_declared_floor(self):
        floors = {"LINE": 97, "BRANCH": 93, "MUTATION": 90}
        baseline = {"LINE": {"covered": 100, "total": 100}, "BRANCH": {"covered": 99, "total": 100}}
        unchanged = {"LINE": {"covered": 100, "total": 100}, "BRANCH": {"covered": 99, "total": 100}}
        quality.enforce_money_coverage(unchanged, floors, baseline)
        for kind in ("LINE", "BRANCH"):
            declined = {**unchanged, kind: {"covered": baseline[kind]["covered"] - 1, "total": 100}}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "decreased"):
                quality.enforce_money_coverage(declined, floors, baseline)


class FullMoneyCommandTest(unittest.TestCase):
    base = "37fc85503994fd03f2f41ab5bfd87191db6d9b56"
    tasks = {
        "build": ("build", "installDist"),
        "coverage": ("jacocoTestReport", "jacocoTestCoverageVerification", "moneyCoverageReport", "moneyMutation"),
        "money-mutation": ("moneyMutation",),
    }

    def setUp(self):
        self.clock = 1000
        self.calls = Mock()
        for name in ("gradle", "test_metrics", "check_coverage", "check_money_coverage"):
            self.enterContext(patch.object(quality, name, getattr(self.calls, name)))
        self.loader = self.enterContext(patch.object(
            quality, "script_module", return_value=Mock(check_latest=self.calls.check_latest),
        ))
        self.provider = self.enterContext(patch.object(quality, "money_provider_module"))
        self.budget = self.enterContext(patch.object(quality, "money_budget", return_value={"enforced_seconds": 600}))
        self.enterContext(patch.object(quality.time, "monotonic", side_effect=lambda: self.clock))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def invoke(self, command, *extra):
        with patch.object(sys, "argv", ["quality.py", command, "--base", self.base, *extra]):
            return quality.main()

    def expected_calls(self, command, write_baseline=False):
        checks = {
            "build": [call.test_metrics()],
            "coverage": [
                call.check_coverage(self.base, write_baseline),
                call.check_money_coverage(self.base, write_baseline),
            ],
            "money-mutation": [],
        }
        return [call.gradle(*self.tasks[command], budget=600), *checks[command], call.check_latest()]

    def test_full_commands_verify_freshness_after_their_required_graph_and_checks(self):
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.loader.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                self.loader.assert_called_once_with("money_mutation")

    def test_missing_or_stale_final_evidence_blocks_each_full_command_and_restores(self):
        for command in self.tasks:
            for error in (
                FileNotFoundError("Synthetic missing mutation record"),
                ValueError("Synthetic stale mutation inputs"),
            ):
                with self.subTest(command=command, error=type(error).__name__):
                    self.calls.reset_mock()
                    self.calls.check_latest.side_effect = error
                    self.assertEqual(1, self.invoke(command))
                    self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                    self.assertIn(str(error), self.errors.getvalue())
                    self.calls.check_latest.side_effect = None
                    self.calls.reset_mock()
                    self.assertEqual(0, self.invoke(command))
                    self.assertEqual(self.expected_calls(command), self.calls.mock_calls)

    def test_native_refresh_failure_or_timeout_never_falls_back_to_an_old_report(self):
        for command in self.tasks:
            for error in (
                subprocess.CalledProcessError(7, ["synthetic-gradle", "moneyMutation"]),
                TimeoutError("Synthetic native refresh exhausted the remaining budget"),
            ):
                with self.subTest(command=command, error=type(error).__name__):
                    self.calls.reset_mock()
                    self.calls.gradle.side_effect = error
                    self.assertEqual(1, self.invoke(command))
                    self.assertEqual([call.gradle(*self.tasks[command], budget=600)], self.calls.mock_calls)

    def test_failed_test_or_coverage_checks_prevent_full_command_success(self):
        for command, failed_check in (
            ("build", "test_metrics"), ("coverage", "check_coverage"), ("coverage", "check_money_coverage"),
        ):
            with self.subTest(command=command, failed_check=failed_check):
                self.calls.reset_mock()
                check = getattr(self.calls, failed_check)
                check.side_effect = ValueError("Synthetic qualification failure")
                self.assertEqual(1, self.invoke(command))
                self.calls.check_latest.assert_not_called()
                self.calls.gradle.assert_called_once_with(*self.tasks[command], budget=600)
                check.side_effect = None

    def test_each_full_command_passes_only_its_remaining_budget_to_gradle(self):
        def read_budget(_):
            self.clock += 17
            return {"enforced_seconds": 600}

        self.budget.side_effect = read_budget
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.calls.gradle.assert_called_once_with(*self.tasks[command], budget=583)
                self.calls.check_latest.assert_called_once_with()

    def test_final_freshness_readback_is_inside_the_full_command_deadline(self):
        def finish_native_work(*_, **__):
            self.clock += 599

        def read_latest():
            self.clock += 2

        self.calls.gradle.side_effect = finish_native_work
        self.calls.check_latest.side_effect = read_latest
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke(command))
                self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                self.assertIn("exceeded its enforced elapsed budget", self.errors.getvalue())

    def test_missing_numeric_budget_refuses_before_any_native_work(self):
        self.budget.side_effect = ValueError("Synthetic missing numeric Money budget")
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke(command))
                self.assertEqual([], self.calls.mock_calls)

    def test_coverage_requires_its_base_before_reading_policy_or_starting_work(self):
        with patch.object(sys, "argv", ["quality.py", "coverage"]), self.assertRaises(SystemExit) as error:
            quality.main()
        self.assertEqual(2, error.exception.code)
        self.budget.assert_not_called()
        self.provider.assert_not_called()
        self.assertEqual([], self.calls.mock_calls)

    def test_writing_coverage_baselines_still_refreshes_and_verifies_mutation(self):
        self.assertEqual(0, self.invoke("coverage", "--write-baseline"))
        self.assertEqual(self.expected_calls("coverage", True), self.calls.mock_calls)

    def test_standalone_mutation_report_remains_readonly(self):
        self.assertEqual(0, self.invoke("money-mutation-report"))
        self.assertEqual([call.check_latest()], self.calls.mock_calls)
        self.loader.assert_called_once_with("money_mutation")
        self.budget.assert_not_called()
        self.provider.assert_not_called()

    def test_focused_coverage_does_not_claim_full_mutation_qualification(self):
        for command, expected in (
            ("money-coverage", [call.gradle("moneyCoverageReport"), call.check_money_coverage(self.base, False)]),
            ("money-coverage-report", [call.check_money_coverage(self.base, False)]),
        ):
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.assertEqual(expected, self.calls.mock_calls)
                self.loader.assert_not_called()
                self.budget.assert_not_called()


if __name__ == "__main__":
    unittest.main()

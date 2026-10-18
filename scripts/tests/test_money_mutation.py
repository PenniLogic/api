"""Synthetic report probes test the consumer, never stand in for real PIT results."""

import ast
from collections import Counter
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("money_mutation", SCRIPTS / "money_mutation.py")
mutation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mutation)
quality = mutation.quality
process_budget = quality.script_module("process_budget")

PRIMITIVE_CLASS = "com.pennilogic.money.AcceptedMoneyPrimitiveModelTest"
PRIMITIVE_METHODS = {
    "construction": "constructionMatchesExactModel()",
    "parsing": "parsingMatchesExactModel()",
    "rendering": "renderingMatchesExactModel()",
    "addition": "additionMatchesExactModel()",
    "subtraction": "subtractionMatchesExactModel()",
    "negation": "negationMatchesExactModel()",
    "ordering": "orderingMatchesExactModel()",
    "mixed_currency": "mixedCurrenciesMatchExactModel()",
    "wire": "wireMatchesExactModel()",
}


def primitive_fixture(directory):
    suite = ET.Element("testsuite", {
        "name": PRIMITIVE_CLASS, "tests": str(len(PRIMITIVE_METHODS)), "failures": "0", "errors": "0", "skipped": "0",
    })
    events = []
    for index, (category, method) in enumerate(PRIMITIVE_METHODS.items(), 1):
        ET.SubElement(suite, "testcase", {"classname": PRIMITIVE_CLASS, "name": method})
        events.append({
            "event": "money_primitive_model", "category": category, "comparisons": index, "disagreements": 0,
        })
    ET.SubElement(suite, "system-out").text = "\n".join(json.dumps(event) for event in events)
    path = directory / "TEST-primitive.xml"
    ET.ElementTree(suite).write(path)
    return suite, path


class SyntheticStrategy:
    BUNDLE = Path("synthetic")
    STRATEGY_FILE = Path("strategy.json")

    def __init__(self):
        self.strategy = {
            "packages": [{
                "id": "api.money", "repository": "PenniLogic/api", "money_path": True,
                "line_coverage_floor_percent": 97, "branch_coverage_floor_percent": 93,
                "mutation_score_floor_percent": 90,
            }],
            "floor_policy": {"money_path_ratchet": True},
            "pipeline_budgets": {"money_path_harness_minutes": 20, "pull_request_gate_minutes": 30},
        }

    def read_strategy(self, _):
        return json.dumps(self.strategy).encode()


class GradleTaskTimingTest(unittest.TestCase):
    """Source regressions; real Gradle event validation uses the extracted observer unchanged."""

    @staticmethod
    def observer_source():
        build = (SCRIPTS.parent / "build.gradle.kts").read_text(encoding="utf-8")
        start = build.index("object GradleTaskTiming {")
        end = build.index("\ngroup = ", start)
        imports = "\n".join(line for line in build.splitlines() if line.startswith("import "))
        return imports + "\n\n" + build[start:end]

    def test_supported_completion_service_is_registered_once_without_task_actions(self):
        observer = self.observer_source()
        for required in ("BuildService<BuildServiceParameters.None>", "\n    OperationCompletionListener {",
                         "override fun onFinish(event: FinishEvent)", "if (event !is TaskFinishEvent) return",
                         'registerIfAbsent("gradleTaskTimings", GradleTaskTimingService::class.java)',
                         "events.onTaskCompletion(timings)", "apply<GradleTaskTimingPlugin>()"):
            with self.subTest(required=required):
                self.assertEqual(1, observer.count(required))
        for forbidden in ("doFirst", "doLast", "dependsOn", "finalizedBy", "mustRunAfter", "onlyIf",
                          "outputs.", "TaskExecutionListener", "afterTask", "buildFinished"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, observer)

    def test_success_does_not_include_cache_up_to_date_skip_failure_or_unknown_results(self):
        observer = self.observer_source()
        status = observer.split("        val status =\n", 1)[1].split("        val start = ", 1)[0]
        self.assertEqual([
            "            when (result) {",
            "                is TaskFailureResult -> {",
            '                    "failed"',
            "                }",
            "",
            "                is TaskSkippedResult -> {",
            '                    "skipped"',
            "                }",
            "",
            "                is TaskSuccessResult -> {",
            "                    when {",
            '                        result.isFromCache -> "from_cache"',
            '                        result.isUpToDate -> "up_to_date"',
            '                        else -> "executed_success"',
            "                    }",
            "                }",
            "",
            "                else -> {",
            '                    "unknown"',
            "                }",
            "            }",
        ], status.splitlines())

    def test_time_is_the_result_interval_not_output_arrival_or_a_zero_fallback(self):
        observer = self.observer_source()
        for required in ("val start = result.startTime", "val end = result.endTime",
                         "val measured = start >= 0 && end >= start",
                         '"timing" to if (measured) "measured" else "unavailable"',
                         '"start_ms" to start.takeIf { measured }', '"end_ms" to end.takeIf { measured }',
                         '"wall_ms" to if (measured) end - start else null'):
            with self.subTest(required=required):
                self.assertIn(required, observer)
        for forbidden in ("currentTimeMillis", "nanoTime", "event.eventTime", "coerceAtLeast", "sumOf"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, observer)

    def test_payload_is_bounded_public_task_data_not_result_or_exception_serialization(self):
        observer = self.observer_source()
        self.assertIn('task.length <= 256 && Regex(":[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*").matches(task)', observer)
        payload = observer.split("return groovy.json.JsonOutput.toJson(", 1)[1].split("\n        )", 1)[0]
        keys = [line.split('"')[1] for line in payload.splitlines() if '"' in line]
        self.assertEqual(["event", "task", "status", "timing", "start_ms", "end_ms", "wall_ms"], keys)
        for forbidden in (".failures", ".skipMessage", ".displayName", ".executionReasons", ".toString()",
                          ".absolutePath", "System.getenv", "System.getProperty"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, observer)

    def test_telemetry_failure_reports_a_closed_reason_without_rethrowing(self):
        observer = self.observer_source()
        handler = observer.split("} catch (_: Exception) {", 1)[1].split("\n        }", 1)[0]
        self.assertEqual(
            {"event": "gradle_task_timing_unavailable", "reason": "observation-failed"},
            json.loads(handler.split('"""', 2)[1]),
        )
        self.assertIn("System.err.println(", handler)
        self.assertNotIn("throw", handler)
        self.assertNotIn("stackTrace", handler)
        self.assertIn("Logging.getLogger(GradleTaskTimingService::class.java).lifecycle(GradleTaskTiming.record(event))", observer)


class MutationPolicyTest(unittest.TestCase):
    def result(self, killed, total, **outcomes):
        counts = Counter({name: 0 for name in mutation.STATUSES})
        counts.update(outcomes)
        counts["KILLED"] = killed
        counts["SURVIVED"] = total - sum(counts.values())
        return {"total": total, "counts": dict(counts)}

    def test_exact_floor_passes_and_just_below_fails_without_rounding(self):
        floors, _ = quality.money_policy(SyntheticStrategy())
        mutation.enforce(self.result(90000, 100000), floors)
        with self.assertRaisesRegex(ValueError, "below the accepted floor"):
            mutation.enforce(self.result(89999, 100000), floors)
        mutation.enforce(self.result(90000, 100000), floors)

    def test_floor_is_read_not_replaced_with_a_default(self):
        provider = SyntheticStrategy()
        provider.strategy["packages"][0]["mutation_score_floor_percent"] = 95
        floors, _ = quality.money_policy(provider)
        with self.assertRaisesRegex(ValueError, "below the accepted floor"):
            mutation.enforce(self.result(94, 100), floors)

    def test_each_missing_number_fails_then_restoration_passes(self):
        provider = SyntheticStrategy()
        package = provider.strategy["packages"][0]
        for name in ("mutation_score_floor_percent", "line_coverage_floor_percent", "branch_coverage_floor_percent"):
            value = package.pop(name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "numeric"):
                quality.money_policy(provider)
            package[name] = value
            mutation.enforce(self.result(90, 100), quality.money_policy(provider)[0])
        for value in (None, "90", 90.0, True, 0, 101):
            package["mutation_score_floor_percent"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "numeric"):
                quality.money_policy(provider)

    def test_unlisted_duplicate_or_non_money_package_fails(self):
        for packages in ([], [SyntheticStrategy().strategy["packages"][0]] * 2, [{
            **SyntheticStrategy().strategy["packages"][0], "money_path": False,
        }]):
            provider = SyntheticStrategy()
            provider.strategy["packages"] = packages
            with self.subTest(packages=packages), self.assertRaisesRegex(ValueError, "uniquely"):
                quality.money_policy(provider)

    def test_timeouts_and_no_coverage_do_not_earn_kill_credit(self):
        floors, _ = quality.money_policy(SyntheticStrategy())
        for status in ("TIMED_OUT", "NO_COVERAGE"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "below"):
                mutation.enforce(self.result(89, 100, **{status: 11}), floors)

    def test_errors_fail_even_if_the_numeric_score_would_pass(self):
        floors, _ = quality.money_policy(SyntheticStrategy())
        for status in mutation.ERROR_STATUSES:
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "unresolved"):
                mutation.enforce(self.result(99, 100, **{status: 1}), floors)

    def test_inconsistent_or_non_numeric_denominator_fails(self):
        floors, _ = quality.money_policy(SyntheticStrategy())
        for total in (0, -1, "100", True, 100.0, None):
            with self.subTest(total=total), self.assertRaisesRegex(ValueError, "denominator"):
                mutation.enforce({**self.result(90, 100), "total": total}, floors)
        result = self.result(90, 100)
        result["counts"]["KILLED"] = 100
        with self.assertRaisesRegex(ValueError, "denominator"):
            mutation.enforce(result, floors)

    def test_budget_is_read_with_native_ceiling_preserved(self):
        provider = SyntheticStrategy()
        self.assertEqual(600, quality.money_budget(provider)["enforced_seconds"])
        provider.strategy["pipeline_budgets"]["money_path_harness_minutes"] = 2
        self.assertEqual(120, quality.money_budget(provider)["enforced_seconds"])
        provider.strategy["pipeline_budgets"]["pull_request_gate_minutes"] = 1
        self.assertEqual(60, quality.money_budget(provider)["enforced_seconds"])
        for name in ("money_path_harness_minutes", "pull_request_gate_minutes"):
            for value in (None, "20", True, 0, -1, 20.0):
                provider = SyntheticStrategy()
                provider.strategy["pipeline_budgets"][name] = value
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "numeric"):
                    quality.money_budget(provider)
            del provider.strategy["pipeline_budgets"][name]
            with self.assertRaisesRegex(ValueError, "numeric"):
                quality.money_budget(provider)


class RuntimeFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        names = (
            "build.gradle.kts", "settings.gradle.kts", "gradle.lockfile",
            "gradle/verification-metadata.xml", "scripts/quality.py", "scripts/process_budget.py",
            "scripts/money_mutation.py", "scripts/PitCatalogue.java", "scripts/money_provider.py",
            "scripts/check_money.py", "provider/source/example.kt", "provider/strategy.json",
            "provider/output.kt", "src/main/kotlin/Example.kt", "compiled/Money.class",
            "classpath/Test.class", "tool.jar", "runtime-image",
        )
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic fingerprint input")
        self.executable = self.root / "runtime-image"
        self.executable.chmod(0o755)
        provider = Mock()
        provider.BUNDLE = Path("provider")
        provider.SOURCE_FILES = ("example.kt",)
        provider.STRATEGY_FILE = Path("strategy.json")
        provider.verify_outputs.return_value = [self.root / "provider/output.kt"]
        self.enterContext(patch.object(mutation, "ROOT", self.root))
        self.enterContext(patch.object(quality, "money_provider_module", return_value=provider))
        self.enterContext(patch.object(
            quality, "money_class_inventory", return_value={"Money": self.root / "compiled/Money.class"},
        ))
        self.config = {
            "java": str(self.executable),
            "tool_classpath": [str(self.root / "tool.jar")],
            "test_classpath": [str(self.root / "classpath")],
        }

    def link(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES) or getattr(error, "winerror", None) == 1314:
                self.skipTest("Creating filesystem symlinks is not permitted on this host")
            raise

    def test_selected_python_and_java_binaries_are_both_fingerprinted(self):
        snapshot = mutation.input_snapshot(self.config)
        python = Path(sys.executable).resolve(strict=True)
        for executable in (python, self.executable):
            data = executable.read_bytes()
            self.assertEqual(
                {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, snapshot[str(executable)],
            )

    def test_money_report_inventory_follows_the_authoritative_owner(self):
        directory = self.root / "build/test-results/test"
        directory.mkdir(parents=True)
        suite, money = primitive_fixture(directory)
        general = directory / "TEST-general.xml"
        general.write_text(
            '<testsuite name="com.pennilogic.testing.MoneyWirePropertyTest" tests="1" '
            'skipped="0" failures="0" errors="0"><testcase '
            'classname="com.pennilogic.testing.MoneyWirePropertyTest" name="property()"/></testsuite>',
            encoding="utf-8",
        )
        legacy = self.root / "build/test-results/moneyTest/TEST-primitive.xml"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(money.read_bytes())
        original = mutation.input_snapshot(self.config)
        self.assertEqual(mutation.file_record(money), original[str(money)])
        self.assertNotIn(str(general), original)
        self.assertNotIn(str(legacy), original)
        self.assertEqual("com.pennilogic.money.", mutation.TEST_PACKAGE)
        general.write_text(general.read_text(encoding="utf-8").replace("property()", "anotherProperty()"), encoding="utf-8")
        legacy.write_bytes(b"stale non-owner data must not be read")
        self.assertEqual(original, mutation.input_snapshot(self.config))
        suite.set("timestamp", "synthetic-new-execution")
        ET.ElementTree(suite).write(money)
        current = mutation.input_snapshot(self.config)
        self.assertNotEqual(original[str(money)], current[str(money)])
        self.assertEqual(mutation.file_record(money), current[str(money)])
        money.unlink()
        self.assertNotIn(str(money), mutation.input_snapshot(self.config))
        with self.assertRaisesRegex(ValueError, "executed target tests"):
            quality.money_test_metrics(self.root)

    def test_runtime_link_chain_and_retargeted_endpoint_or_bytes_change_snapshot(self):
        middle, selected = self.root / "middle", self.root / "any-runtime-name"
        self.link(middle, self.executable)
        self.link(selected, middle)
        self.config["java"] = str(selected)
        original = mutation.input_snapshot(self.config)
        self.assertIn(str(self.executable), original)
        self.assertNotIn(str(selected), original)
        self.assertNotIn(str(middle), original)
        replacement = self.root / "another-image"
        replacement.write_bytes(self.executable.read_bytes())
        replacement.chmod(0o755)
        middle.unlink()
        self.link(middle, replacement)
        self.assertNotEqual(original, mutation.input_snapshot(self.config))
        middle.unlink()
        self.link(middle, self.executable)
        self.assertEqual(original, mutation.input_snapshot(self.config))
        self.executable.write_bytes(b"changed synthetic executable")
        self.assertNotEqual(original, mutation.input_snapshot(self.config))

    def test_missing_relative_directory_broken_or_cyclic_runtime_refuses(self):
        broken, cycle = self.root / "broken", self.root / "cycle"
        self.link(broken, self.root / "absent")
        self.link(cycle, cycle)
        for path in (self.root / "absent", Path("runtime-image"), self.root, broken, cycle):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError, RuntimeError)):
                mutation.runtime_executable(path)
        self.assertEqual(self.executable, mutation.runtime_executable(self.executable))

    @unittest.skipIf(os.name == "nt", "Windows chmod does not express POSIX executable permissions")
    def test_non_executable_runtime_refuses_and_permission_restoration_passes(self):
        self.executable.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "executable regular file"):
            mutation.input_snapshot(self.config)
        self.executable.chmod(0o755)
        self.assertIn(str(self.executable), mutation.input_snapshot(self.config))

    def test_source_provider_and_class_files_still_refuse_links(self):
        for name in (
            "build.gradle.kts", "src/main/kotlin/Example.kt", "provider/output.kt",
            "compiled/Money.class", "classpath/Test.class",
        ):
            path = self.root / name
            original = path.read_bytes()
            path.unlink()
            self.link(path, self.executable)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "missing or linked"):
                mutation.input_snapshot(self.config)
            path.unlink()
            path.write_bytes(original)
            mutation.input_snapshot(self.config)

    def test_runtime_resolution_does_not_exempt_linked_classpath_roots_or_descendants(self):
        selected = self.root / "shared-alias"
        self.link(selected, self.executable)
        self.config["java"] = str(selected)
        self.config["tool_classpath"] = [str(selected)]
        with self.assertRaisesRegex(ValueError, "unsafe or unsupported"):
            mutation.input_snapshot(self.config)
        self.config["tool_classpath"] = [str(self.root / "tool.jar")]
        descendant = self.root / "classpath/linked"
        self.link(descendant, self.root / "compiled")
        with self.assertRaisesRegex(ValueError, "contains a link"):
            mutation.input_snapshot(self.config)
        descendant.unlink()
        mutation.input_snapshot(self.config)

    def test_report_outputs_do_not_gain_runtime_link_resolution(self):
        output = self.root / "engine.log"
        self.link(output, self.executable)
        with self.assertRaisesRegex(ValueError, "missing or linked"):
            mutation.file_record(output)
        output.unlink()
        output.write_text(">> Generated 100 mutations Killed 90 (90%)\n")
        binding = mutation.file_record(output)
        mutation.check_native_summary(output, binding, MutationPolicyTest().result(90, 100))


class MutationReportTest(unittest.TestCase):
    target = "com.pennilogic.contracts.money.Money"
    operator = "org.pitest.synthetic.Operator"

    def entry(self, index=0, status="KILLED"):
        node = ET.Element("mutation", {
            "status": status, "detected": "true" if status == "KILLED" else "false",
            "numberOfTestsRun": "0" if status == "NO_COVERAGE" else "1",
        })
        for name, value in {
            "sourceFile": "Money.kt", "mutatedClass": self.target, "mutatedMethod": "plus",
            "methodDescription": "()V", "lineNumber": "1", "mutator": self.operator,
            "killingTest": (
                "com.pennilogic.money.SyntheticTest."
                "[engine:junit-jupiter]/[class:com.pennilogic.money.SyntheticTest]/[method:contract()]"
                if status == "KILLED" else ""
            ),
        }.items():
            ET.SubElement(node, name).text = value
        ET.SubElement(ET.SubElement(node, "indexes"), "index").text = str(index)
        return node

    def read(self, entries, partial="false"):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mutations.xml"
            root = ET.Element("mutations", {"partial": partial})
            root.extend(entries)
            ET.ElementTree(root).write(path)
            return mutation.read_report(
                path, {self.target, self.target + "$Companion"}, {self.operator: "SYNTHETIC"}, {"Money.kt"},
            )

    def test_all_outcomes_and_zero_mutant_classes_are_retained(self):
        result = self.read([self.entry(index, status) for index, status in enumerate(mutation.STATUSES)])
        self.assertEqual(len(mutation.STATUSES), result["total"])
        self.assertEqual({name: 1 for name in mutation.STATUSES}, result["counts"])
        self.assertEqual({"numerator": 1, "denominator": len(mutation.STATUSES)}, result["score"])
        self.assertEqual(0, result["classes"][self.target + "$Companion"])
        self.assertEqual(len(mutation.STATUSES), len(result["outcomes"]))

    def test_partial_line_coverage_flag_is_not_a_mutation_completion_flag(self):
        self.assertTrue(self.read([self.entry()], partial="true")["partial_line_coverage"])
        with self.assertRaisesRegex(ValueError, "complete PIT"):
            self.read([self.entry()], partial="unrecognised")

    def test_empty_duplicate_and_unknown_outcomes_fail(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            self.read([])
        with self.assertRaisesRegex(ValueError, "repeats"):
            self.read([self.entry(), self.entry()])
        with self.assertRaisesRegex(ValueError, "unknown outcome"):
            self.read([self.entry(status="IGNORED")])

    def test_missing_invalid_or_duplicate_identity_fields_fail(self):
        for name in ("sourceFile", "mutatedClass", "mutatedMethod", "methodDescription", "lineNumber", "mutator"):
            entry = self.entry()
            entry.remove(entry.find(name))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "identity"):
                self.read([entry])
        for field in ("numberOfTestsRun",):
            for value in ("-1", "1.0", "", "true"):
                entry = self.entry()
                entry.set(field, value)
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "number"):
                    self.read([entry])
        entry = self.entry()
        ET.SubElement(entry, "lineNumber").text = "2"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.read([entry])

    def test_pruned_or_unrelated_target_and_operator_cannot_qualify(self):
        for field in ("sourceFile", "mutatedClass", "mutator"):
            entry = self.entry()
            entry.find(field).text = "unrelated"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "outside"):
                self.read([entry])

    def test_kills_require_an_executed_money_jupiter_test(self):
        for field, value in (
            ("numberOfTestsRun", "0"), ("detected", "false"), ("killingTest", ""),
            ("killingTest", "[engine:junit-jupiter]/[class:unrelated.Test]/[method:contract()]"),
        ):
            entry = self.entry()
            if field == "killingTest":
                entry.find(field).text = value
            else:
                entry.set(field, value)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "executed"):
                self.read([entry])

    def test_catalogue_keeps_all_switch_factories_and_default_feature_state(self):
        output = (
            "MUTATOR\tEXPERIMENTAL_REMOVE_SWITCH_MUTATOR_[0-99]\torg.pitest.Switch_0\n"
            "MUTATOR\tEXPERIMENTAL_REMOVE_SWITCH_MUTATOR_[0-99]\torg.pitest.Switch_1\n"
            "FEATURE\tfkotlin\ttrue\nFEATURE\tflogcall\ttrue\nFEATURE\tfstati\ttrue\nFEATURE\tfsynthetic\tfalse\n"
        )
        catalogue, features = mutation.read_catalogue(output)
        self.assertEqual(2, len(catalogue))
        self.assertEqual(("-flogcall", "+fkotlin"), mutation.FEATURE_SELECTION)
        self.assertTrue(features["fkotlin"]["enabled"])
        self.assertTrue(features["fkotlin"]["default_enabled"])
        self.assertFalse(features["flogcall"]["enabled"])
        self.assertTrue(features["flogcall"]["default_enabled"])
        self.assertTrue(features["fstati"]["enabled"])
        self.assertFalse(features["fsynthetic"]["enabled"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            mutation.read_catalogue(output + "MUTATOR\tX\torg.pitest.Switch_0\n")
        with self.assertRaises(ValueError):
            mutation.read_catalogue("")

    def test_catalogue_requires_both_native_feature_controls_and_valid_defaults(self):
        mutator = "MUTATOR\tSYNTHETIC\torg.pitest.synthetic.Operator\n"
        for output in (
            "FEATURE\tfkotlin\ttrue\n",
            "FEATURE\tflogcall\ttrue\n",
        ):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "control is unavailable"):
                mutation.read_catalogue(mutator + output)
        for output in (
            "FEATURE\tfkotlin\ttrue\nFEATURE\tfkotlin\ttrue\nFEATURE\tflogcall\ttrue\n",
            "FEATURE\tfkotlin\tunknown\nFEATURE\tflogcall\ttrue\n",
        ):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "feature catalogue"):
                mutation.read_catalogue(mutator + output)

    def test_truncated_xml_never_qualifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mutations.xml"
            path.write_text('<mutations partial="false"><mutation')
            with self.assertRaises(ET.ParseError):
                mutation.read_report(path, {self.target}, {self.operator: "SYNTHETIC"}, {"Money.kt"})


class PrimitiveModelEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "build/test-results/test"
        self.directory.mkdir(parents=True)
        properties = ET.Element("testsuite", {"tests": "3", "failures": "0", "errors": "0", "skipped": "0"})
        for name in ("round_trip", "associativity", "collision_keys"):
            ET.SubElement(properties, "testcase", {"classname": "com.pennilogic.money.SyntheticTest", "name": name})
        ET.SubElement(properties, "system-out").text = "\n".join(
            json.dumps({"event": "money_property_cases", "property": name, "count": 2}, separators=(",", ":"))
            for name in ("round_trip", "associativity", "collision_keys")
        )
        self.properties = self.directory / "TEST-properties.xml"
        ET.ElementTree(properties).write(self.properties)
        self.suite, self.path = primitive_fixture(self.directory)
        self.output = self.suite.find("system-out")
        self.rows = self.output.text.splitlines()

    def save(self, rows=None):
        self.output.text = "\n".join(self.rows if rows is None else rows)
        ET.ElementTree(self.suite).write(self.path)

    def metrics(self):
        return quality.money_test_metrics(self.root)

    def test_real_shape_requires_all_scoped_counts_and_preserves_undelivered_product_oracle(self):
        evidence = self.metrics()
        self.assertEqual(12, evidence["target_tests"])
        self.assertEqual(0, evidence["skipped"])
        self.assertEqual(0, evidence["failed"])
        self.assertEqual({"round_trip": 2, "associativity": 2, "collision_keys": 2}, evidence["property_cases"])
        self.assertEqual({"status": "not_implemented", "disagreements": None}, evidence["independent_oracle"])
        self.assertEqual({
            "status": "passed", "representation": "jdk_big_integer", "comparisons": 45, "disagreements": 0,
            "categories": dict(zip(PRIMITIVE_METHODS, range(1, 10))),
        }, evidence["primitive_model"])

    def test_ordinary_property_suite_remains_in_total_metrics_but_not_money_evidence(self):
        path = self.directory / "TEST-wire-property.xml"
        path.write_text(
            '<testsuite name="com.pennilogic.testing.MoneyWirePropertyTest" tests="1" '
            'skipped="0" failures="0" errors="0"><testcase '
            'classname="com.pennilogic.testing.MoneyWirePropertyTest" name="property()"/></testsuite>',
            encoding="utf-8",
        )
        self.assertEqual({self.path, self.properties}, set(quality.money_test_reports(self.root)))
        self.assertEqual(12, self.metrics()["target_tests"])
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            quality.test_metrics(self.root)
        self.assertEqual({"event": "tests", "count": 13, "skipped": 0, "failed": 0}, json.loads(output.getvalue()))

    def test_legacy_money_task_reports_neither_supply_nor_duplicate_current_evidence(self):
        legacy = self.root / "build/test-results/moneyTest"
        legacy.mkdir(parents=True)
        for path in (self.path, self.properties):
            (legacy / path.name).write_bytes(path.read_bytes())
        self.assertEqual(12, self.metrics()["target_tests"])
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "primitive"):
            self.metrics()
        self.save()
        self.properties.unlink()
        with self.assertRaisesRegex(ValueError, "property case evidence is missing"):
            self.metrics()
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "executed target tests"):
            self.metrics()

    def test_duplicate_owner_reports_are_refused_not_deduplicated(self):
        (self.directory / "TEST-duplicate.xml").write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicate case"):
            self.metrics()

    def test_money_named_report_cannot_hide_unrelated_cases(self):
        for named_by_file in (False, True):
            name = "com.pennilogic.money.UnrelatedTest" if named_by_file else "unrelated"
            path = self.directory / f"TEST-{name}.xml"
            suite_name = "unrelated" if named_by_file else "com.pennilogic.money.UnrelatedTest"
            path.write_text(
                f'<testsuite name="{suite_name}" tests="1" skipped="0" failures="0" errors="0">'
                '<testcase classname="unrelated.Test" name="case()"/></testsuite>',
                encoding="utf-8",
            )
            with self.subTest(named_by_file=named_by_file), self.assertRaisesRegex(ValueError, "unrelated"):
                self.metrics()
            path.unlink()
        mixed = ET.parse(self.properties)
        mixed.getroot().find("testcase").set("classname", "unrelated.Test")
        mixed.write(self.properties)
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.metrics()

    def test_missing_suite_or_each_category_refuses_and_restores(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "primitive"):
            self.metrics()
        self.save()
        for index in range(len(self.rows)):
            self.save(self.rows[:index] + self.rows[index + 1:])
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "primitive"):
                self.metrics()
            self.save()
            self.assertEqual(45, self.metrics()["primitive_model"]["comparisons"])

    def test_duplicate_events_and_fields_cannot_hide_alongside_valid_evidence(self):
        duplicate_field = self.rows[0].replace('"comparisons": 1', '"comparisons": 0, "comparisons": 1')
        escaped_field = self.rows[0].replace('"comparisons": 1', '"comparisons": 0, "\\u0063omparisons": 1')
        escaped_event = self.rows[0].replace("money_primitive_model", "\\u006doney_primitive_model")
        for extra in [*self.rows, duplicate_field, escaped_field, escaped_event]:
            self.save([*self.rows, extra])
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "primitive"):
                self.metrics()
        self.save()
        duplicate = ET.SubElement(self.suite, "system-out")
        duplicate.text = self.rows[0]
        self.save()
        with self.assertRaisesRegex(ValueError, "duplicated"):
            self.metrics()
        self.suite.remove(duplicate)
        self.save()
        self.assertEqual(45, self.metrics()["primitive_model"]["comparisons"])

    def test_missing_invalid_zero_or_disagreeing_counters_refuse(self):
        original = json.loads(self.rows[0])
        for field in ("comparisons", "disagreements"):
            missing = {key: value for key, value in original.items() if key != field}
            self.save([json.dumps(missing), *self.rows[1:]])
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, "primitive"):
                self.metrics()
            values = [None, False, True, "1", 1.0, [], {}, -1]
            values.append(0 if field == "comparisons" else 1)
            for value in values:
                self.save([json.dumps({**original, field: value}), *self.rows[1:]])
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "primitive"):
                    self.metrics()
        for category in (None, [], {}, 1, "", "debt", "allocation"):
            self.save([json.dumps({**original, "category": category}), *self.rows[1:]])
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, "primitive"):
                self.metrics()

    def test_malformed_model_records_never_hide_beside_valid_events_or_echo_payloads(self):
        marker = "synthetic-private-marker"
        invalid = [
            '{"event":"money_primitive_model",',
            '["money_primitive_model"]',
            '["\\u006doney_primitive_model"]',
            '"money_primitive_model"',
            '"\\u006doney_primitive_model"',
            "money_primitive_model " + marker,
            self.rows[0].replace('"comparisons": 1', '"comparisons": NaN'),
            self.rows[0].replace('"comparisons": 1', '"comparisons": Infinity'),
            self.rows[0] + self.rows[0],
            json.dumps({**json.loads(self.rows[0]), "unexpected": marker}),
            self.rows[0].replace("money_primitive_model", "money_primitive_model_invalid"),
            self.rows[0].replace("money_primitive_model", "\\u006doney_primitive_model_invalid"),
            self.rows[0].replace('"money_primitive_model"', '["\\u006doney_primitive_model"]'),
        ]
        for extra in invalid:
            self.save([*self.rows, extra])
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError) as refusal:
                    self.metrics()
                self.assertNotIn(marker, str(refusal.exception))
        self.save()
        self.assertEqual(45, self.metrics()["primitive_model"]["comparisons"])

    def test_model_events_are_bound_to_their_own_executed_test_class_and_method(self):
        case = self.suite.find("testcase")
        for node, field, changed in (
            (self.suite, "name", "com.pennilogic.money.OtherTest"),
            (case, "name", "notTheComparison()"),
            (case, "classname", "com.pennilogic.money.OtherTest"),
        ):
            original = node.get(field)
            node.set(field, changed)
            self.save()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "executed comparison"):
                self.metrics()
            node.set(field, original)
        self.save()
        other_suite = ET.parse(self.properties)
        other_suite.getroot().find("system-out").text += "\n" + self.rows[0]
        other_suite.write(self.properties)
        self.save(self.rows[1:])
        with self.assertRaisesRegex(ValueError, "executed comparison"):
            self.metrics()

    def test_unexecuted_skipped_failed_error_or_duplicate_cases_never_qualify(self):
        first = self.suite.find("testcase")
        for counter, element in (("skipped", "skipped"), ("failures", "failure"), ("errors", "error")):
            self.suite.set(counter, "1")
            status = ET.SubElement(first, element)
            self.save()
            with self.subTest(status=element), self.assertRaises(ValueError):
                self.metrics()
            first.remove(status)
            self.suite.set(counter, "0")
        extra = ET.SubElement(self.suite, "testcase", dict(first.attrib))
        self.suite.set("tests", "10")
        self.save()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.metrics()
        self.suite.remove(extra)
        for case in self.suite.findall("testcase"):
            self.suite.remove(case)
        self.suite.set("tests", "0")
        self.save()
        with self.assertRaisesRegex(ValueError, "executed comparison"):
            self.metrics()


class MutationEvidenceTest(unittest.TestCase):
    def test_provider_properties_without_primitive_model_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "build/test-results/test"
            directory.mkdir(parents=True)
            suite = ET.Element("testsuite", {"tests": "3", "failures": "0", "errors": "0", "skipped": "0"})
            for name in ("round_trip", "associativity", "collision_keys"):
                ET.SubElement(suite, "testcase", {
                    "classname": "com.pennilogic.money.SyntheticTest", "name": name,
                })
            ET.SubElement(suite, "system-out").text = "\n".join(
                json.dumps({"event": "money_property_cases", "property": name, "count": 2}, separators=(",", ":"))
                for name in ("round_trip", "associativity", "collision_keys")
            )
            ET.ElementTree(suite).write(directory / "TEST-synthetic.xml")
            with self.assertRaisesRegex(ValueError, "primitive"):
                quality.money_test_metrics(root)

    def test_standalone_consumer_rejects_below_floor_changed_outputs_and_stale_inputs_then_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "20260101T000000Z-000000000000"
            directory = root / mutation.REPORTS / run_id
            directory.mkdir(parents=True)
            (directory.parent / "latest.json").write_text(json.dumps({"run_id": run_id}))
            report = directory / "mutations.xml"
            catalogue_file = directory / "catalogue.log"
            catalogue_file.write_text(
                "MUTATOR\tSYNTHETIC\torg.pitest.synthetic.Operator\n"
                "FEATURE\tfkotlin\ttrue\nFEATURE\tflogcall\ttrue\n"
            )
            catalogue, features = mutation.read_catalogue(catalogue_file.read_text())
            provider = Mock(wraps=SyntheticStrategy())
            provider.BUNDLE = Path("synthetic")
            provider.STRATEGY_FILE = Path("strategy.json")
            provider.verify_outputs = Mock(return_value=[])
            provider.safe_path = lambda base, relative: base / relative
            floors, strategy_hash = quality.money_policy(provider)
            fixture = MutationReportTest()
            test_evidence = {
                "target_tests": 12, "skipped": 0, "failed": 0,
                "property_cases": {"round_trip": 2, "associativity": 2, "collision_keys": 2},
                "independent_oracle": {"status": "not_implemented", "disagreements": None},
                "primitive_model": {
                    "status": "passed", "representation": "jdk_big_integer", "comparisons": 45, "disagreements": 0,
                    "categories": dict(zip(PRIMITIVE_METHODS, range(1, 10))),
                },
            }

            def store(record):
                (directory / "run.json").write_text(json.dumps(record))

            def save(killed):
                document = ET.Element("mutations", {"partial": "true"})
                document.extend(fixture.entry(index, "KILLED" if index < killed else "SURVIVED") for index in range(100))
                ET.ElementTree(document).write(report)
                (directory / "engine.log").write_text(f">> Generated 100 mutations Killed {killed} ({killed}%)\n")
                result = mutation.read_report(report, {fixture.target}, catalogue, {"Money.kt"})
                java = str(root / "synthetic-java")
                commands = []
                for name, arguments in (
                    ("catalogue", ["SyntheticCatalogue"]),
                    ("engine", ["SyntheticEngine", "--features=-flogcall,+fkotlin"]),
                ):
                    argfile = directory / f"{name}.args"
                    mutation.java_arguments(argfile, arguments)
                    commands.append({"exit_code": 0, "argv": [java, "@" + str(argfile)], "java_arguments": arguments})
                record = {
                    "schema_version": 2, "feature_selection": ["-flogcall", "+fkotlin"],
                    "status": "passed", "strategy_sha256": strategy_hash,
                    "floor_percent": floors, "budget": quality.money_budget(provider), "elapsed_seconds": 1,
                    "inputs": {"synthetic": "bound"}, "config": {"java": java},
                    "outputs": {
                        name: mutation.file_record(directory / name)
                        for name in ("mutations.xml", "catalogue.log", "catalogue.args", "engine.args", "engine.log")
                    },
                    "target_classes": [fixture.target], "target_sources": ["Money.kt"],
                    "catalogue": catalogue, "features": features,
                    "commands": commands,
                    "report": mutation.file_record(report), "result": result,
                    "test_evidence": json.loads(json.dumps(test_evidence)),
                }
                store(record)
                return record

            with (
                patch.object(mutation, "ROOT", root),
                patch.object(quality, "money_provider_module", return_value=provider),
                patch.object(quality, "money_class_inventory", return_value={fixture.target.replace(".", "/"): "synthetic"}),
                patch.object(mutation, "input_snapshot", return_value={"synthetic": "bound"}) as snapshot,
                patch.object(quality, "money_test_metrics", return_value=test_evidence) as metrics,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                save(90)
                mutation.check_latest()
                for replacement in (
                    None, {}, {**test_evidence, "primitive_model": None},
                    {**test_evidence, "primitive_model": {**test_evidence["primitive_model"], "comparisons": 46}},
                    {**test_evidence, "primitive_model": {**test_evidence["primitive_model"], "comparisons": 45.0}},
                    {**test_evidence, "primitive_model": {**test_evidence["primitive_model"], "disagreements": 1}},
                    {**test_evidence, "primitive_model": {**test_evidence["primitive_model"], "disagreements": False}},
                    {**test_evidence, "independent_oracle": {"status": "passed", "disagreements": 0}},
                ):
                    record = save(90)
                    if replacement is None:
                        del record["test_evidence"]
                    else:
                        record["test_evidence"] = replacement
                    store(record)
                    with self.subTest(evidence=replacement), self.assertRaisesRegex(ValueError, "test evidence"):
                        mutation.check_latest()
                    save(90)
                    mutation.check_latest()
                for field in ("test_evidence", "disagreements"):
                    record = save(90)
                    content = json.dumps(record)
                    key = json.dumps(field) + ":"
                    duplicate = key + ("null, " if field == "test_evidence" else "1, ") + key
                    (directory / "run.json").write_text(content.replace(key, duplicate, 1))
                    with self.subTest(duplicate=field), self.assertRaisesRegex(ValueError, "malformed"):
                        mutation.check_latest()
                    save(90)
                    mutation.check_latest()
                metrics.side_effect = ValueError("Money primitive model evidence is missing a required category")
                with self.assertRaisesRegex(ValueError, "primitive"):
                    mutation.check_latest()
                metrics.side_effect = None
                mutation.check_latest()
                for change in ("omit-survivor", "reclassify-kill"):
                    record = save(95)
                    log_binding = record["outputs"]["engine.log"].copy()
                    document = ET.parse(report)
                    if change == "omit-survivor":
                        entry = next(node for node in document.getroot() if node.get("status") == "SURVIVED")
                        document.getroot().remove(entry)
                    else:
                        entry = next(node for node in document.getroot() if node.get("status") == "KILLED")
                        entry.set("status", "SURVIVED")
                        entry.set("detected", "false")
                        entry.find("killingTest").text = ""
                    document.write(report)
                    record["report"] = record["outputs"]["mutations.xml"] = mutation.file_record(report)
                    record["result"] = mutation.read_report(report, {fixture.target}, catalogue, {"Money.kt"})
                    self.assertEqual(log_binding, mutation.file_record(directory / "engine.log"))
                    mutation.enforce(record["result"], floors)
                    store(record)
                    with self.subTest(change=change), self.assertRaisesRegex(ValueError, "PIT console counts disagree"):
                        mutation.check_latest()
                    save(95)
                    mutation.check_latest()
                record = save(90)
                del record["outputs"]["engine.log"]
                store(record)
                with self.assertRaisesRegex(ValueError, "native engine log"):
                    mutation.check_latest()
                save(90)
                mutation.check_latest()
                save(89)
                with self.assertRaisesRegex(ValueError, "below"):
                    mutation.check_latest()
                save(90)
                mutation.check_latest()
                report.write_bytes(report.read_bytes() + b"\n")
                with self.assertRaisesRegex(ValueError, "output changed"):
                    mutation.check_latest()
                save(90)
                snapshot.return_value = {"synthetic": "changed"}
                with self.assertRaisesRegex(ValueError, "stale"):
                    mutation.check_latest()
                snapshot.return_value = {"synthetic": "bound"}
                mutation.check_latest()
                for field, value in (
                    ("schema_version", 1), ("feature_selection", None),
                    ("feature_selection", ["-flogcall", "-fkotlin"]),
                ):
                    record = save(90)
                    record[field] = value
                    store(record)
                    with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "measurement scope"):
                        mutation.check_latest()
                    save(90)
                    mutation.check_latest()
                record = save(90)
                record["features"] = {**features, "fkotlin": {**features["fkotlin"], "enabled": False}}
                store(record)
                with self.assertRaisesRegex(ValueError, "complete successful engine invocation"):
                    mutation.check_latest()
                save(90)
                mutation.check_latest()
                for selections in (
                    [], ["--features=-flogcall,-fkotlin"],
                    ["--features", "-flogcall,+fkotlin"],
                    ["--features=-flogcall,+fkotlin", "--features=-fkotlin"],
                ):
                    record = save(90)
                    arguments = ["SyntheticEngine", *selections]
                    record["commands"][1]["java_arguments"] = arguments
                    mutation.java_arguments(directory / "engine.args", arguments)
                    record["outputs"]["engine.args"] = mutation.file_record(directory / "engine.args")
                    store(record)
                    with self.subTest(selections=selections), self.assertRaisesRegex(ValueError, "feature selection"):
                        mutation.check_latest()
                    save(90)
                    mutation.check_latest()
                for name in ("catalogue", "engine"):
                    record = save(90)
                    argfile = directory / f"{name}.args"
                    mutation.java_arguments(argfile, ["SyntheticOldInvocation", "--features=-flogcall,-fkotlin"])
                    record["outputs"][argfile.name] = mutation.file_record(argfile)
                    store(record)
                    with self.subTest(argfile=name), self.assertRaisesRegex(ValueError, "argument file"):
                        mutation.check_latest()
                    save(90)
                    mutation.check_latest()
                record = save(90)
                record["commands"][1]["argv"][-1] = "@" + str(directory / "unbound.args")
                store(record)
                with self.assertRaisesRegex(ValueError, "argument file"):
                    mutation.check_latest()
                save(90)
                mutation.check_latest()

    def test_property_metrics_require_executed_categories_and_do_not_invent_an_oracle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "build/test-results/test"
            directory.mkdir(parents=True)
            primitive_fixture(directory)
            suite = ET.Element("testsuite", {"tests": "3", "failures": "0", "errors": "0", "skipped": "0"})
            for index in range(3):
                ET.SubElement(suite, "testcase", {"classname": "com.pennilogic.money.SyntheticTest", "name": f"case-{index}"})
            output = ET.SubElement(suite, "system-out")
            events = [
                {"event": "money_property_cases", "property": name, "count": 2}
                for name in ("round_trip", "associativity", "collision_keys")
            ]
            path = directory / "TEST-synthetic.xml"

            def save(rows):
                output.text = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
                ET.ElementTree(suite).write(path)

            save(events)
            evidence = quality.money_test_metrics(root)
            self.assertEqual(12, evidence["target_tests"])
            self.assertEqual({"round_trip": 2, "associativity": 2, "collision_keys": 2}, evidence["property_cases"])
            self.assertEqual({"status": "not_implemented", "disagreements": None}, evidence["independent_oracle"])
            for rows in ([], events[:2], events + [events[0]], [{**events[0], "count": True}, *events[1:]]):
                save(rows)
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    quality.money_test_metrics(root)
            save(events)
            for name, value in (("tests", "0"), ("tests", "-1"), ("tests", ""), ("tests", "4"), ("skipped", "1"), ("errors", "1")):
                previous = suite.get(name)
                suite.set(name, value)
                save(events)
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    quality.money_test_metrics(root)
                suite.set(name, previous)
            save(events)
            self.assertEqual(12, quality.money_test_metrics(root)["target_tests"])
            case = suite.find("testcase")
            case.set("classname", "unrelated.SyntheticTest")
            save(events)
            with self.assertRaisesRegex(ValueError, "unrelated"):
                quality.money_test_metrics(root)

    def test_failed_preflight_replaces_latest_reference_without_overwriting_prior_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / mutation.REPORTS
            previous_id = "20260101T000000Z-000000000000"
            previous = directory / previous_id
            previous.mkdir(parents=True)
            original = '{"status":"synthetic-prior-attempt"}\n'
            (previous / "run.json").write_text(original)
            (directory / "latest.json").write_text(json.dumps({"run_id": previous_id}))
            provider = Mock()
            provider.owned_target.side_effect = lambda base, relative: base / relative
            provider.verify_outputs.side_effect = ValueError("Synthetic missing strategy number")
            with (
                patch.object(mutation, "ROOT", root),
                patch.object(quality, "money_provider_module", return_value=provider),
                patch("sys.stdout", new_callable=io.StringIO),
                self.assertRaisesRegex(ValueError, "missing strategy"),
            ):
                mutation.execute(root / "absent-input.json")
            current_id = json.loads((directory / "latest.json").read_text())["run_id"]
            self.assertNotEqual(previous_id, current_id)
            record = json.loads((directory / current_id / "run.json").read_text())
            self.assertEqual("failed", record["status"])
            self.assertIsNone(record["result"])
            self.assertEqual([], record["commands"])
            self.assertEqual(original, (previous / "run.json").read_text())


class MutationCleanupTimeoutTest(unittest.TestCase):
    def failed_attempt(self, error, *, cli=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        provider = Mock(wraps=SyntheticStrategy())
        provider.BUNDLE, provider.STRATEGY_FILE = Path("synthetic"), Path("strategy.json")
        provider.SOURCE_REF, provider.DOCS_REF = "synthetic-contracts", "synthetic-docs"
        provider.OUTPUT_HASHES = {"Money.kt": "synthetic-source"}
        provider.verify_outputs = Mock(return_value=[])
        provider.owned_target = Mock(side_effect=lambda base, relative: base / relative)
        config = {
            "java": str(root / "synthetic-java.exe"),
            "tool_classpath": [str(root / (name + "-1.0.0.jar")) for name in (
                "pitest", "pitest-entry", "pitest-command-line", "pitest-junit5-plugin", "junit-platform-launcher",
            )],
            "test_classpath": [str(root / "synthetic-tests")],
        }
        input_file = root / "input.json"
        input_file.write_text(json.dumps(config), encoding="utf-8")
        with (
            patch.object(mutation, "ROOT", root),
            patch.object(quality, "money_provider_module", return_value=provider),
            patch.object(quality, "money_class_inventory", return_value={"com/pennilogic/money/Money": "synthetic"}),
            patch.object(quality, "money_test_metrics", return_value={"synthetic": "consumer-control-only"}),
            patch.object(mutation, "input_snapshot", return_value={"synthetic": "bound"}),
            patch.object(quality, "script_module", return_value=process_budget),
            patch.object(process_budget, "run", side_effect=error) as run,
            patch.object(process_budget.subprocess, "Popen", side_effect=AssertionError("No native mutation workload")),
            patch("sys.stdout", new_callable=io.StringIO) as output,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            if cli:
                with patch.object(sys, "argv", ["money_mutation.py", "run", "--input-file", str(input_file)]):
                    self.assertEqual(mutation.main(), 1)
                self.assertIn("Money mutation gate failed", stderr.getvalue())
            else:
                with self.assertRaises(type(error)) as raised:
                    mutation.execute(input_file)
                self.assertIs(raised.exception, error)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs["capture"])
        run_id = json.loads((root / mutation.REPORTS / "latest.json").read_text(encoding="utf-8"))["run_id"]
        directory = root / mutation.REPORTS / run_id
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        event = json.loads(output.getvalue())
        for value in (record, event):
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["failure"], str(error))
        self.assertEqual(event["run_id"], run_id)
        self.assertIsNone(record["result"])
        self.assertEqual(len(record["commands"]), 1)
        self.assertTrue(record["commands"][0]["budget_exceeded"])
        self.assertIsNone(record["commands"][0]["exit_code"])
        self.assertGreaterEqual(record["commands"][0]["elapsed_seconds"], 0)
        return directory, record

    def test_cleanup_timeout_persists_original_failure_without_inventing_output(self):
        error = TimeoutError("Owned process job cleanup exceeded its existing allowance")
        self.assertFalse(hasattr(error, "output"))
        directory, record = self.failed_attempt(error)
        self.assertFalse((directory / "catalogue.log").exists())
        self.assertNotIn("catalogue.log", record["outputs"])

    def test_cli_cleanup_timeout_keeps_terminal_failure_and_nonzero_exit(self):
        directory, record = self.failed_attempt(
            TimeoutError("Owned process job cleanup exceeded its existing allowance"), cli=True,
        )
        self.assertFalse((directory / "catalogue.log").exists())
        self.assertNotIn("catalogue.log", record["outputs"])

    def test_capture_bearing_budget_timeout_preserves_existing_output_contract(self):
        for output in (None, "", "synthetic captured output\n"):
            with self.subTest(output=output):
                directory, record = self.failed_attempt(process_budget.BudgetExceeded(output))
                path = directory / "catalogue.log"
                self.assertEqual(path.read_text(encoding="utf-8"), output or "")
                self.assertEqual(record["outputs"]["catalogue.log"], mutation.file_record(path))


class MutationNativeConsoleTest(unittest.TestCase):
    def test_required_log_binding_and_restoration(self):
        result = MutationPolicyTest().result(90, 100)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "engine.log"
            path.write_text(">> Generated 100 mutations Killed 90 (90%)\n")
            binding = mutation.file_record(path)
            for expected in (
                None, {}, {"bytes": binding["bytes"]}, {"sha256": binding["sha256"]},
                {**binding, "bytes": binding["bytes"] + 1}, {**binding, "sha256": "0" * 64},
            ):
                with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "native engine log"):
                    mutation.check_native_summary(path, expected, result)
                mutation.check_native_summary(path, binding, result)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                mutation.check_native_summary(path, binding, result)

    def test_unique_complete_numeric_summary_and_exact_counts(self):
        result = MutationPolicyTest().result(90, 100)
        summary = ">> Generated 100 mutations Killed 90 (90%)"
        subtotal = ">> Generated 25 Killed 25 (100%)\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "engine.log"
            for output in (
                "", subtotal, ">> Generated 100 mutations",
                ">> Generated mutations Killed 90 (90%)",
                ">> Generated unknown mutations Killed 90 (90%)",
                ">> Generated 100 mutations Killed unknown (90%)",
                ">> Generated 100 mutations Killed -1 (90%)",
                ">> Generated 100.0 mutations Killed 90 (90%)",
                ">> Generated 0100 mutations Killed 90 (90%)",
                summary + "\n" + summary,
                summary + "\n>> Generated 101 mutations Killed 90 (89%)",
                ">> Generated 101 mutations Killed 90 (89%)",
                ">> Generated 100 mutations Killed 89 (89%)",
            ):
                path.write_text(output)
                with self.subTest(output=output), self.assertRaises(ValueError):
                    mutation.check_native_summary(path, mutation.file_record(path), result)
                path.write_text(subtotal + summary + "\n")
                mutation.check_native_summary(path, mutation.file_record(path), result)

    def test_rounded_native_percentage_never_supplies_floor_credit(self):
        floors, _ = quality.money_policy(SyntheticStrategy())
        result = MutationPolicyTest().result(89999, 100000)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "engine.log"
            path.write_text(">> Generated 100000 mutations Killed 89999 (90%)\n")
            mutation.check_native_summary(path, mutation.file_record(path), result)
            with self.assertRaisesRegex(ValueError, "below the accepted floor"):
                mutation.enforce(result, floors)
            result = MutationPolicyTest().result(90000, 100000)
            path.write_text(">> Generated 100000 mutations Killed 90000 (90%)\n")
            mutation.check_native_summary(path, mutation.file_record(path), result)
            mutation.enforce(result, floors)


APPROVED_SHELL_LOOKUP_SETUP = r"""
$script:pennilogicLookup=@{File=$null;Clock=$null;Hash=$null;Write=$null;Bytes=0;Records=0;
    Failed=$false;Armed=$false;Seen=$false;Done=$false;Start=$null;Pre=$null;Post=$null;
    NegativeArmed=$false;NegativeSeen=$false;NegativeStart=$null;NotFound=$null;Boundary=$null};
try {
    $s=$script:pennilogicLookup;
    $attributes=[IO.File]::GetAttributes($pennilogicLookupPath);
    if(($attributes -band ([IO.FileAttributes]::ReparsePoint -bor [IO.FileAttributes]::Directory)) -ne 0){
        throw 'capture-file-kind'
    };
    $s.File=[IO.File]::Open($pennilogicLookupPath,[IO.FileMode]::Open,
        [IO.FileAccess]::Write,[IO.FileShare]::Read);
    if($s.File.Length -ne 0){throw 'capture-not-empty'};
    $s.Clock=[Diagnostics.Stopwatch]::StartNew();
    $s.Hash=[Security.Cryptography.SHA256]::Create();
    $s.Utf8=[Text.UTF8Encoding]::new($false,$true);
    $s.System=[IO.Path]::GetFullPath([IO.Path]::Combine($PSHOME,'Modules')).TrimEnd('\');
    $s.Invariant=[Globalization.CultureInfo]::InvariantCulture;
    $s.Write=[Action[string]]{
        param($fields)
        $w=$script:pennilogicLookup;
        if($w.Failed){return};
        try {
            if($fields -match '[^\x20-\x7e]'){throw 'capture-encoding'};
            $bytes=[Text.Encoding]::ASCII.GetBytes('{"v":2,'+$fields+"}`n");
            if($bytes.Length -gt 6144 -or $w.Records -ge 5 -or
                $w.Bytes+$bytes.Length -gt 8192 -or $w.File.Length -ne $w.Bytes){
                throw 'capture-limit'
            };
            $w.File.Write($bytes,0,$bytes.Length);$w.File.Flush();
            $w.Bytes+=$bytes.Length;$w.Records++;
        } catch {
            $w.Failed=$true;
            [Console]::Error.WriteLine('{"event":"api106_module_lookup_capture","status":"refused"}');
        }
    };
    $rootsReady=$false;$rootCode='snapshot';
    try {
        $raw=[Environment]::GetEnvironmentVariable('PSModulePath','Process');
        $entries=[Collections.Generic.List[string]]::new();
        $seen=[Collections.Generic.Dictionary[string,int]]::new([StringComparer]::Ordinal);
        $value='absent';
        if($null -ne $raw){
            $value='present';
            if($raw.Length -gt 32768){$rootCode='input-limit';throw 'root-limit'};
            $roots=$raw.Split([char[]]';',[StringSplitOptions]::None);
            if($roots.Length -gt 32){$rootCode='input-limit';throw 'root-limit'};
            for($n=0;$n -lt $roots.Length;$n++){
                $root=$roots[$n];
                if($root.Length -gt 2048){$rootCode='input-limit';throw 'root-limit'};
                $kind='unknown';
                if($root.Length -eq 0){$kind='empty'}
                elseif([StringComparer]::OrdinalIgnoreCase.Equals(
                    $root.Replace('/','\').TrimEnd('\'),$s.System)){$kind='pshome'};
                $rootCode='encoding';$encoded=$s.Utf8.GetBytes($root);$rootCode='snapshot';
                $hash=[BitConverter]::ToString($s.Hash.ComputeHash($encoded)).Replace('-','').ToLowerInvariant();
                $duplicate='null';
                if($seen.ContainsKey($root)){$duplicate=$seen[$root].ToString($s.Invariant)}
                else{$seen.Add($root,$n)};
                $entries.Add('{"i":'+$n.ToString($s.Invariant)+',"kind":"'+$kind+
                    '","sha256":"'+$hash+'","duplicate_of":'+$duplicate+'}');
            }
        };
        $s.Write.Invoke('"phase":"roots","status":"observed","value":"'+$value+
            '","roots":['+[String]::Join(',',$entries)+']');
        $rootsReady=-not $s.Failed;
    } catch {
        $s.Write.Invoke('"phase":"roots","status":"refused","code":"'+$rootCode+'"');
    };
    $observerStatus='unavailable';$observerCode='roots-refused';
    if($rootsReady){
        $observerCode='unsupported-api';
        $s.Intrinsics=$ExecutionContext.InvokeCommand;
        $intrinsicsType=[System.Management.Automation.CommandInvocationIntrinsics];
        $eventType=[System.Management.Automation.CommandLookupEventArgs];
        $s.PreProperty=$intrinsicsType.GetProperty('PreCommandLookupAction');
        $s.PostProperty=$intrinsicsType.GetProperty('PostCommandLookupAction');
        $s.NameProperty=$eventType.GetProperty('CommandName');
        $s.CommandProperty=$eventType.GetProperty('Command');
        $s.TypeProperty=[System.Management.Automation.CommandInfo].GetProperty('CommandType');
        $s.ModuleProperty=[System.Management.Automation.CommandInfo].GetProperty('Module');
        $s.PathProperty=[System.Management.Automation.PSModuleInfo].GetProperty('Path');
        $supported=$true;
        foreach($property in @($s.PreProperty,$s.PostProperty,$s.NameProperty,$s.CommandProperty,
            $s.TypeProperty,$s.ModuleProperty,$s.PathProperty)){
            if($null -eq $property -or $null -eq $property.GetGetMethod() -or
                $property.GetGetMethod().IsVirtual){$supported=$false;break}
        };
        if($supported -and $null -ne $s.PreProperty.GetSetMethod() -and
            $null -ne $s.PostProperty.GetSetMethod() -and
            $s.PreProperty.PropertyType -eq [EventHandler[System.Management.Automation.CommandLookupEventArgs]] -and
            $s.PostProperty.PropertyType -eq $s.PreProperty.PropertyType){
            $observerCode='existing-hooks';
            if($null -eq $s.PreProperty.GetValue($s.Intrinsics) -and
                $null -eq $s.PostProperty.GetValue($s.Intrinsics)){
                try {
                    $s.Pre=[EventHandler[System.Management.Automation.CommandLookupEventArgs]]{
                        param($sender,$lookupEvent)
                        $p=$script:pennilogicLookup;
                        if(-not $p.Armed -or $p.Done -or $p.Failed){return};
                        try {
                            if(-not [StringComparer]::OrdinalIgnoreCase.Equals(
                                $p.NameProperty.GetValue($lookupEvent),'Write-Output')){return};
                            if($p.Seen){
                                $p.Done=$true;
                                $p.Write.Invoke('"phase":"post","status":"unavailable","code":"nested-target"');
                                return
                            };
                            $p.Seen=$true;$preBoundary=$p.Boundary;$ms=$p.Clock.ElapsedMilliseconds;$time='null';
                            if($ms -ge 0 -and $ms -le 600000){
                                $p.Start=$ms;$time=$ms.ToString($p.Invariant)
                            };
                            $p.Write.Invoke('"phase":"pre","status":"observed","ms":'+$time);
                            if($null -ne $preBoundary){
                                [IO.File]::AppendAllText($preBoundary,'C',[Text.Encoding]::ASCII);
                            };
                        } catch {
                            $p.Done=$true;
                            $p.Write.Invoke('"phase":"post","status":"refused","code":"callback"');
                        }
                    };
                    $s.Post=[EventHandler[System.Management.Automation.CommandLookupEventArgs]]{
                        param($sender,$lookupEvent)
                        $p=$script:pennilogicLookup;
                        if(-not $p.Armed -or $p.Done -or $p.Failed){return};
                        $code='callback';
                        try {
                            if(-not [StringComparer]::OrdinalIgnoreCase.Equals(
                                $p.NameProperty.GetValue($lookupEvent),'Write-Output')){return};
                            $p.Done=$true;
                            if(-not $p.Seen){
                                $p.Write.Invoke('"phase":"post","status":"unavailable","code":"post-without-pre"');
                                return
                            };
                            $ms=$p.Clock.ElapsedMilliseconds;$time='null';$elapsed='null';
                            if($ms -ge 0 -and $ms -le 600000 -and
                                ($null -eq $p.Start -or $ms -ge $p.Start) -and
                                ($null -eq $p.NegativeStart -or $ms -ge $p.NegativeStart)){
                                $time=$ms.ToString($p.Invariant);
                                if($null -ne $p.Start -and $ms -ge $p.Start){
                                    $elapsed=($ms-$p.Start).ToString($p.Invariant)
                                }
                            };
                            $command=$p.CommandProperty.GetValue($lookupEvent);
                            $type='null';$kind='no-command';$hash='null';
                            if($null -ne $command){
                                $number=[int]$p.TypeProperty.GetValue($command);
                                if($number -notin @(1,2,4,8,16,32,64,128,256)){
                                    $code='command-type';throw 'command-type'
                                };
                                $type=$number.ToString($p.Invariant);$kind='none';
                                $module=$p.ModuleProperty.GetValue($command);
                                if($null -ne $module){
                                    $path=$p.PathProperty.GetValue($module);$kind='unknown';
                                    if(-not [string]::IsNullOrEmpty($path)){
                                        if($path.Length -gt 2048){$code='module-path-limit';throw 'module-path-limit'};
                                        $hash='"'+[BitConverter]::ToString($p.Hash.ComputeHash(
                                            $p.Utf8.GetBytes($path))).Replace('-','').ToLowerInvariant()+'"';
                                        $kind='other';
                                        if([IO.Path]::GetPathRoot($path).Length -ge 3 -and
                                            [IO.Path]::GetFullPath($path).StartsWith(
                                                $p.System+'\',[StringComparison]::OrdinalIgnoreCase)){$kind='pshome'}
                                    }
                                }
                            };
                            $p.Write.Invoke('"phase":"post","status":"observed","ms":'+$time+
                                ',"lookup_ms":'+$elapsed+',"command_type":'+$type+
                                ',"module_kind":"'+$kind+'","module_sha256":'+$hash);
                        } catch {
                            $p.Done=$true;
                            $p.Write.Invoke('"phase":"post","status":"refused","code":"'+$code+'"');
                        }
                    };
                    $s.PreProperty.SetValue($s.Intrinsics,$s.Pre);
                    $s.PostProperty.SetValue($s.Intrinsics,$s.Post);
                    if(-not [Object]::ReferenceEquals($s.PreProperty.GetValue($s.Intrinsics),$s.Pre) -or
                        -not [Object]::ReferenceEquals($s.PostProperty.GetValue($s.Intrinsics),$s.Post)){
                        throw 'observer-install'
                    };
                    $s.Armed=$true;$observerStatus='armed';
                } catch {$observerStatus='refused';$observerCode='install'}
            }
        }
    };
    $negativeStatus='unavailable';$negativeCode='observer-unavailable';
    if($s.Armed){
        $negativeCode='unsupported-api';
        try {
            $s.NotFoundProperty=$intrinsicsType.GetProperty('CommandNotFoundAction');
            if($null -ne $s.NotFoundProperty -and
                $null -ne $s.NotFoundProperty.GetGetMethod() -and
                -not $s.NotFoundProperty.GetGetMethod().IsVirtual -and
                $null -ne $s.NotFoundProperty.GetSetMethod() -and
                $s.NotFoundProperty.PropertyType -eq $s.PreProperty.PropertyType){
                $negativeCode='existing-hook';
                if($null -eq $s.NotFoundProperty.GetValue($s.Intrinsics)){
                    $s.NotFound=[EventHandler[System.Management.Automation.CommandLookupEventArgs]]{
                        param($sender,$lookupEvent)
                        $p=$script:pennilogicLookup;
                        if(-not $p.Armed -or -not $p.NegativeArmed -or $p.Done -or $p.Failed){return};
                        try {
                            if(-not [StringComparer]::OrdinalIgnoreCase.Equals(
                                $p.NameProperty.GetValue($lookupEvent),'Write-Output')){return};
                            if(-not $p.Seen){
                                $p.Done=$true;
                                $p.Write.Invoke('"phase":"post","status":"unavailable","code":"post-without-pre"');
                                return
                            };
                            if($p.NegativeSeen){
                                $p.Done=$true;
                                $p.Write.Invoke('"phase":"post","status":"unavailable","code":"nested-target"');
                                return
                            };
                            $p.NegativeSeen=$true;$ms=$p.Clock.ElapsedMilliseconds;$time='null';
                            if($ms -ge 0 -and $ms -le 600000 -and
                                ($null -eq $p.Start -or $ms -ge $p.Start)){
                                $p.NegativeStart=$ms;$time=$ms.ToString($p.Invariant)
                            };
                            $p.Write.Invoke('"phase":"not-found","status":"observed","ms":'+$time);
                        } catch {
                            $p.Done=$true;
                            $p.Write.Invoke('"phase":"post","status":"refused","code":"callback"');
                        }
                    };
                    $s.NotFoundProperty.SetValue($s.Intrinsics,$s.NotFound);
                    if(-not [Object]::ReferenceEquals($s.NotFoundProperty.GetValue($s.Intrinsics),$s.NotFound)){
                        throw 'negative-observer-install'
                    };
                    $s.NegativeArmed=$true;$negativeStatus='armed';
                }
            }
        } catch {$negativeStatus='refused';$negativeCode='install'}
    };
    $fields='"phase":"observer","status":"'+$observerStatus+'"';
    if($observerStatus -ne 'armed'){$fields+=',"code":"'+$observerCode+'"'};
    $fields+=',"not_found":{"status":"'+$negativeStatus+'"';
    if($negativeStatus -ne 'armed'){$fields+=',"code":"'+$negativeCode+'"'};
    $s.Write.Invoke($fields+'}');
} catch {
    if(-not $script:pennilogicLookup.Failed){
        $script:pennilogicLookup.Failed=$true;
        [Console]::Error.WriteLine('{"event":"api106_module_lookup_capture","status":"refused"}');
    }
};
"""

APPROVED_SHELL_LOOKUP_FINISH = r"""
;$s=$script:pennilogicLookup;
try {
    if($null -ne $s.Write -and -not $s.Done){
        $s.Done=$true;$code='observer-unavailable';
        if($s.Armed){$code=if($s.Seen){'no-post'}else{'no-pre'}};
        $s.Write.Invoke('"phase":"post","status":"unavailable","code":"'+$code+'"');
    };
    if($null -ne $s.Pre -and [Object]::ReferenceEquals(
        $s.PreProperty.GetValue($s.Intrinsics),$s.Pre)){$s.PreProperty.SetValue($s.Intrinsics,$null)};
    if($null -ne $s.Post -and [Object]::ReferenceEquals(
        $s.PostProperty.GetValue($s.Intrinsics),$s.Post)){$s.PostProperty.SetValue($s.Intrinsics,$null)};
    if($null -ne $s.NotFound -and [Object]::ReferenceEquals(
        $s.NotFoundProperty.GetValue($s.Intrinsics),$s.NotFound)){$s.NotFoundProperty.SetValue($s.Intrinsics,$null)};
} catch {
    if(-not $s.Failed){
        $s.Failed=$true;
        [Console]::Error.WriteLine('{"event":"api106_module_lookup_capture","status":"refused"}');
    }
} finally {
    try {
        try {if($null -ne $s.Hash){$s.Hash.Dispose()}}
        finally {
            try {if($null -ne $s.Clock){$s.Clock.Stop()}}
            finally {if($null -ne $s.File){$s.File.Dispose()}}
        }
    } catch {
        if(-not $s.Failed){
            $s.Failed=$true;
            [Console]::Error.WriteLine('{"event":"api106_module_lookup_capture","status":"refused"}');
        }
    }
};
"""


# A separately reviewed owning-suite supervisor must install the hard enclosure.
APPROVED_SHELL_PAIR_ENCLOSURE = None


def approved_shell_auxiliary_command(command, boundary, lookup):
    registration = """                    $s.PreProperty.SetValue($s.Intrinsics,$s.Pre);
                    $s.PostProperty.SetValue($s.Intrinsics,$s.Post);
                    if(-not [Object]::ReferenceEquals($s.PreProperty.GetValue($s.Intrinsics),$s.Pre) -or
                        -not [Object]::ReferenceEquals($s.PostProperty.GetValue($s.Intrinsics),$s.Post)){
                        throw 'observer-install'
                    };
                    $s.Armed=$true;$observerStatus='armed';
"""
    observer_record = """    $fields='"phase":"observer","status":"'+$observerStatus+'"';
    if($observerStatus -ne 'armed'){$fields+=',"code":"'+$observerCode+'"'};
    $fields+=',"not_found":{"status":"'+$negativeStatus+'"';
    if($negativeStatus -ne 'armed'){$fields+=',"code":"'+$negativeCode+'"'};
    $s.Write.Invoke($fields+'}');
"""
    if (command[:4] != ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
            or len(command) != 5 or command[-1].count(APPROVED_SHELL_LOOKUP_SETUP) != 1):
        raise ValueError("Observer comparison requires the original composed Windows command")
    setup = APPROVED_SHELL_LOOKUP_SETUP
    for old, new in ((registration, ""), (observer_record, "    $s.Done=$true;\n")):
        if setup.count(old) != 1:
            raise ValueError("Observer comparison setup no longer matches its reviewed seam")
        setup = setup.replace(old, new, 1)
    auxiliary = [*command[:4], command[-1].replace(APPROVED_SHELL_LOOKUP_SETUP, setup, 1)]
    for variable, path, name in (
        ("pennilogicBoundary", boundary, "api106-approved-shell-auxiliary-boundary.bin"),
        ("pennilogicLookupPath", lookup, "api106-approved-shell-auxiliary-module-lookup.jsonl"),
    ):
        old = "$" + variable + "='" + str(path).replace("'", "''") + "';"
        new = "$" + variable + "='" + str(path.with_name(name)).replace("'", "''") + "';"
        if auxiliary[-1].count(old) != 1:
            raise ValueError("Observer comparison diagnostic binding is not unique")
        auxiliary[-1] = auxiliary[-1].replace(old, new, 1)
    return auxiliary


def run_approved_shell_observer_pair(case, command, boundary, lookup, *, enclosure):
    if (boundary.name != "api106-approved-shell-boundary.bin"
            or lookup != boundary.with_name("api106-approved-shell-module-lookup.jsonl")):
        raise ValueError("Observer comparison requires the original diagnostic file bindings")
    required_error, required_traceback = None, None
    completed = False
    try:
        with enclosure(seconds=60, cleanup_reserve=12) as window:
            started = getattr(window, "started", None)
            if (type(started) not in (int, float) or not math.isfinite(started)
                    or not 0 <= started <= time.monotonic()):
                raise ValueError("Observer comparison requires the admitted absolute epoch")
            for parent in (boundary.parent.parent, boundary.parent):
                if parent.is_symlink() or parent.is_junction():
                    raise ValueError("Observer comparison diagnostic area is a reparse point")
            report = boundary.with_name("api106-approved-shell-observer-pair.jsonl")
            with report.open("xb") as stream:
                def record(fields):
                    data = (json.dumps({
                        "event": "api106_approved_shell_observer_pair", "v": 1,
                        "diagnostic_only": True, **fields,
                    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
                    if len(data) > 1024 or stream.tell() + len(data) > 4096:
                        raise ValueError("Observer comparison report exceeded its fixed bound")
                    stream.write(data)
                    stream.flush()

                record({"phase": "begin", "order": "required-then-auxiliary",
                        "enclosing_seconds": 60, "cleanup_reserve_seconds": 12,
                        "arm_work_seconds": 10, "arm_cleanup_seconds": 10})
                auxiliary = approved_shell_auxiliary_command(command, boundary, lookup)
                for path in (boundary.with_name("api106-approved-shell-auxiliary-boundary.bin"),
                             lookup.with_name("api106-approved-shell-auxiliary-module-lookup.jsonl")):
                    with path.open("xb"):
                        pass
                outcomes = {}
                for role, argv in (("required", command), ("auxiliary-no-lookup-delegates", auxiliary)):
                    if role != "required" and outcomes["required"] == "process-or-cleanup-error":
                        outcomes[role] = "not-executed"
                        record({"phase": "arm", "role": role, "outcome": "not-executed",
                                "reason": "required-process-or-cleanup-error"})
                        break
                    if time.monotonic() - started > 28:
                        record({"phase": "arm", "role": role, "outcome": "not-executed",
                                "reason": "insufficient-complete-arm-budget"})
                        raise TimeoutError("Observer comparison cannot admit another complete 10+10-second arm")
                    record({"phase": "started", "role": role})
                    arm_started = time.monotonic()
                    if arm_started - started > 28:
                        record({"phase": "arm", "role": role, "outcome": "not-executed",
                                "reason": "insufficient-complete-arm-budget"})
                        raise TimeoutError("Observer comparison cannot admit another complete 10+10-second arm")
                    outcome, error, traceback = "passed", None, None
                    returncode, stdout = None, None
                    try:
                        result = process_budget.run(argv, SCRIPTS, 10, capture=True)
                        candidate_returncode = result.returncode
                        if type(candidate_returncode) is not int or not -2147483648 <= candidate_returncode <= 4294967295:
                            raise ValueError("Observer comparison received an invalid process result")
                        returncode = candidate_returncode
                        if type(result.stdout) is not str:
                            raise ValueError("Observer comparison received an invalid capture result")
                        data = result.stdout.encode("utf-8", errors="backslashreplace")
                        stdout = {"text_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                        case.assertEqual(result.returncode, 0)
                        case.assertEqual(result.stdout.strip(), "approved-shell")
                    except (AssertionError, OSError, ValueError, subprocess.TimeoutExpired) as failure:
                        error, traceback = failure, failure.__traceback__
                        if isinstance(failure, process_budget.OutputExceeded):
                            outcome = "output-budget-exceeded"
                        elif isinstance(failure, process_budget.BudgetExceeded):
                            outcome = "work-budget-exceeded"
                        elif isinstance(failure, AssertionError):
                            outcome = "assertion-failed"
                        else:
                            outcome = "process-or-cleanup-error"
                    if role == "required":
                        required_error, required_traceback = error, traceback
                    outcomes[role] = outcome
                    record({"phase": "arm", "role": role, "outcome": outcome,
                            "elapsed_ms": int((time.monotonic() - arm_started) * 1000),
                            "returncode": returncode, "stdout": stdout})
                if time.monotonic() - started > 48:
                    raise TimeoutError("Observer comparison exceeded its 48-second work allowance")
                record({"phase": "complete", "required": outcomes["required"],
                        "auxiliary": outcomes["auxiliary-no-lookup-delegates"], "acceptance": False})
            if time.monotonic() - started > 48:
                raise TimeoutError("Observer comparison finalization exceeded its 48-second work allowance")
            completed = True
        if not completed:
            raise RuntimeError("Observer comparison enclosure suppressed an incomplete protocol")
    except (AssertionError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as failure:
        if required_error is not None and failure is not required_error:
            required_error.add_note("Observer comparison also failed in reporting or enclosure; no complete-pair credit")
            raise required_error.with_traceback(required_traceback) from failure
        raise
    if required_error is not None:
        raise required_error.with_traceback(required_traceback)


class ApprovedShellWriterReturnTest(unittest.TestCase):
    """Source controls only; never execute PowerShell or the protected native fixture."""

    def fixture(self):
        source = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        owner, = [item for item in source.body if isinstance(item, ast.ClassDef) and item.name == "ProcessBudgetTest"]
        method, = [item for item in owner.body if isinstance(item, ast.FunctionDef)
                   and item.name == "test_approved_system_shell_survives_without_inheriting_ambient_path"]
        return method

    def test_first_target_callback_marks_only_after_the_pre_writer_returns(self):
        callback = APPROVED_SHELL_LOOKUP_SETUP.split(
            "$s.Pre=[EventHandler[System.Management.Automation.CommandLookupEventArgs]]{", 1,
        )[1].split("$s.Post=[EventHandler[System.Management.Automation.CommandLookupEventArgs]]{", 1)[0]
        expected = r"""
            param($sender,$lookupEvent)
            $p=$script:pennilogicLookup;
            if(-not $p.Armed -or $p.Done -or $p.Failed){return};
            try {
                if(-not [StringComparer]::OrdinalIgnoreCase.Equals(
                    $p.NameProperty.GetValue($lookupEvent),'Write-Output')){return};
                if($p.Seen){
                    $p.Done=$true;
                    $p.Write.Invoke('"phase":"post","status":"unavailable","code":"nested-target"');
                    return
                };
                $p.Seen=$true;$preBoundary=$p.Boundary;$ms=$p.Clock.ElapsedMilliseconds;$time='null';
                if($ms -ge 0 -and $ms -le 600000){
                    $p.Start=$ms;$time=$ms.ToString($p.Invariant)
                };
                $p.Write.Invoke('"phase":"pre","status":"observed","ms":'+$time);
                if($null -ne $preBoundary){
                    [IO.File]::AppendAllText($preBoundary,'C',[Text.Encoding]::ASCII);
                };
            } catch {
                $p.Done=$true;
                $p.Write.Invoke('"phase":"post","status":"refused","code":"callback"');
            }
        };
        """
        self.assertEqual([line.strip() for line in callback.strip().splitlines()],
                         [line.strip() for line in expected.strip().splitlines()])
        marker = "[IO.File]::AppendAllText($preBoundary,'C',[Text.Encoding]::ASCII);"
        self.assertEqual(APPROVED_SHELL_LOOKUP_SETUP.count(marker), 1)
        self.assertNotIn(marker, APPROVED_SHELL_LOOKUP_FINISH)

    def test_marker_is_armed_only_between_s_flags_and_the_original_bare_statement(self):
        assignment, = [item for item in ast.walk(self.fixture()) if isinstance(item, ast.Assign)
                       and len(item.targets) == 1 and ast.unparse(item.targets[0]) == "command[-1]"]
        expected = ast.parse(r"""(
            "$pennilogicBoundary='" + str(boundary).replace("'", "''") + "';"
            "[IO.File]::AppendAllText($pennilogicBoundary,'E',[Text.Encoding]::ASCII);"
            "$pennilogicLookupPath='" + str(lookup).replace("'", "''") + "';"
            + APPROVED_SHELL_LOOKUP_SETUP +
            "$i=[Console]::IsInputRedirected;$o=[Console]::IsOutputRedirected;"
            "$e=[Console]::IsErrorRedirected;"
            "$r=if(($i -is [bool])-and($o -is [bool])-and($e -is [bool]))"
            "{'S{0}{1}{2}' -f [int]$i,[int]$o,[int]$e}else{'U'};"
            "[IO.File]::AppendAllText($pennilogicBoundary,$r,[Text.Encoding]::ASCII);"
            "if($r -cne 'U'){$script:pennilogicLookup.Boundary=$pennilogicBoundary};"
            + command[-1] + ";"
            "$script:pennilogicLookup.Boundary=$null;"
            "[IO.File]::AppendAllText($pennilogicBoundary,'X',[Text.Encoding]::ASCII)"
            + APPROVED_SHELL_LOOKUP_FINISH
        )""", mode="eval").body
        self.assertEqual(ast.dump(assignment.value), ast.dump(expected))
        self.assertIn("NotFound=$null;Boundary=$null};", APPROVED_SHELL_LOOKUP_SETUP)

    def test_original_fixture_keeps_shell_arguments_limits_and_result_assertions(self):
        method = self.fixture()
        expected_command = ast.parse(
            r"""command = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            "Write-Output 'approved-shell'"] if os.name == "nt"
                           else ["/bin/sh", "-c", "printf 'approved-shell\\n'"])"""
        ).body[0]
        self.assertEqual(ast.dump(method.body[0]), ast.dump(expected_command))
        calls = [item for item in ast.walk(method) if isinstance(item, ast.Call)]
        run, = [item for item in calls if ast.unparse(item.func) == "process_budget.run"]
        self.assertEqual(ast.dump(run), ast.dump(ast.parse(
            "process_budget.run(command, SCRIPTS, 10, capture=True)", mode="eval",
        ).body))
        expected_assertions = ast.parse(
            'self.assertEqual(result.returncode, 0)\nself.assertEqual(result.stdout.strip(), "approved-shell")'
        ).body
        self.assertEqual([ast.dump(item) for item in method.body[-2:]],
                         [ast.dump(item) for item in expected_assertions])
        for name in ("boundary", "lookup"):
            create, = [item for item in calls if ast.unparse(item.func) == name + ".open"]
            self.assertEqual(ast.dump(create), ast.dump(ast.parse(name + '.open("xb")', mode="eval").body))

    def test_observer_installation_and_cleanup_keep_null_and_identity_guards(self):
        setup = "".join(APPROVED_SHELL_LOOKUP_SETUP.split())
        finish = "".join(APPROVED_SHELL_LOOKUP_FINISH.split())
        self.assertIn(
            "if($null-eq$s.PreProperty.GetValue($s.Intrinsics)-and"
            "$null-eq$s.PostProperty.GetValue($s.Intrinsics)){try{", setup,
        )
        self.assertIn("if($null-eq$s.NotFoundProperty.GetValue($s.Intrinsics)){", setup)
        for name in ("Pre", "Post", "NotFound"):
            install = f"$s.{name}Property.SetValue($s.Intrinsics,$s.{name})"
            identity = f"[Object]::ReferenceEquals($s.{name}Property.GetValue($s.Intrinsics),$s.{name})"
            self.assertEqual(setup.count(install), 1)
            self.assertIn(identity, setup)
            self.assertIn(
                f"if($null-ne$s.{name}-and{identity})"
                f"{{$s.{name}Property.SetValue($s.Intrinsics,$null)}}", finish,
            )

    def test_v2_writer_limits_flush_and_fixed_failure_channel_are_unchanged(self):
        writer = APPROVED_SHELL_LOOKUP_SETUP.split("$s.Write=[Action[string]]{", 1)[1].split(
            "$rootsReady=$false;", 1,
        )[0]
        self.assertIn('''$bytes=[Text.Encoding]::ASCII.GetBytes('{"v":2,'+$fields+"}`n");''', writer)
        compact = "".join(writer.split())
        self.assertIn(
            "if($bytes.Length-gt6144-or$w.Records-ge5-or"
            "$w.Bytes+$bytes.Length-gt8192-or$w.File.Length-ne$w.Bytes){throw'capture-limit'};", compact,
        )
        self.assertIn(
            "$w.File.Write($bytes,0,$bytes.Length);$w.File.Flush();"
            "$w.Bytes+=$bytes.Length;$w.Records++;", compact,
        )
        self.assertIn(
            '''catch{$w.Failed=$true;[Console]::Error.WriteLine('{"event":"api106_module_lookup_capture",'''
            '''"status":"refused"}');}''', compact,
        )
        self.assertNotIn("Boundary", writer)
        self.assertNotIn("pre_record_writer_return", APPROVED_SHELL_LOOKUP_SETUP)


def run_docker_fixture(command, root):
    try:
        return quality.run(command, root=root, capture=True, budget=10)
    except subprocess.CalledProcessError as error:
        note = {"schema": "pennilogic.synthetic-docker-fixture/1", "status": "unavailable"}
        if type(error.output) is str:
            data = error.output.encode("utf-8", errors="backslashreplace")
            note.update(capture_text_bytes=len(data), capture_text_sha256=hashlib.sha256(data).hexdigest())
            if len(data) <= 4096:
                tail = error.output.splitlines()[-1:]
                for approved in (False, True):
                    for isolated in (False, True):
                        value = {"approved": approved, "isolated": isolated}
                        if tail == [json.dumps(value)]:
                            note.update(status="observed", **value)
        error.add_note("synthetic_docker_fixture=" + json.dumps(note, sort_keys=True, separators=(",", ":")))
        raise


class DockerFixtureDiagnosticTest(unittest.TestCase):
    def failure(self, output):
        error = subprocess.CalledProcessError(3758096423, ["synthetic-worker"], output=output)
        error.add_note("existing-note")
        with (
            patch.object(quality, "run", side_effect=error) as run,
            patch("sys.stdout", new_callable=io.StringIO),
            self.assertRaises(subprocess.CalledProcessError) as observed,
        ):
            run_docker_fixture(["synthetic-worker"], SCRIPTS)
        self.assertIs(observed.exception, error)
        self.assertEqual(error.returncode, 3758096423)
        self.assertEqual(error.__notes__[0], "existing-note")
        self.assertEqual(len(error.__notes__), 2)
        run.assert_called_once_with(["synthetic-worker"], root=SCRIPTS, capture=True, budget=10)
        return json.loads(error.__notes__[-1].split("=", 1)[1]), error.__notes__[-1]

    def test_failure_keeps_exact_error_and_bounded_synthetic_boolean_output(self):
        for approved in (False, True):
            for isolated in (False, True):
                with self.subTest(approved=approved, isolated=isolated):
                    value = {"approved": approved, "isolated": isolated}
                    output = json.dumps(value) + "\n"
                    note, text = self.failure(output)
                    self.assertEqual(note, {
                        "schema": "pennilogic.synthetic-docker-fixture/1", "status": "observed", **value,
                        "capture_text_bytes": len(output),
                        "capture_text_sha256": hashlib.sha256(output.encode()).hexdigest(),
                    })
                    self.assertLess(len(text), 512)

    def test_unrecognized_output_is_hash_only_and_never_prints_ambient_values(self):
        secret = "synthetic-private-value-not-for-diagnostics"
        for output in (secret, secret + "\n" + json.dumps({"approved": False, "isolated": True})):
            with self.subTest(trailing_record="\n" in output):
                note, text = self.failure(output)
                self.assertNotIn(secret, text)
                self.assertEqual(note["capture_text_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(note["status"], "observed" if "\n" in output else "unavailable")

    def test_invalid_oversized_or_missing_output_does_not_invent_a_synthetic_record(self):
        for output in (None, b'{"approved": false, "isolated": true}', "x" * 4097,
                       '{"approved": 1, "isolated": true}', '{"approved": false, "isolated": true, "extra": 1}',
                       '{"approved": false, "approved": true, "isolated": true}', "\ud800"):
            with self.subTest(type=type(output).__name__):
                note, text = self.failure(output)
                self.assertEqual(note["status"], "unavailable")
                self.assertNotIn("approved", note)
                self.assertNotIn("isolated", note)
                self.assertLess(len(text), 512)

    def test_success_returns_unchanged_output(self):
        output = '{"approved": true, "isolated": true}\n'
        with patch.object(quality, "run", return_value=output) as run:
            self.assertIs(run_docker_fixture(["synthetic-worker"], SCRIPTS), output)
        run.assert_called_once_with(["synthetic-worker"], root=SCRIPTS, capture=True, budget=10)

    def test_unrelated_failure_is_not_reclassified_or_hidden(self):
        error = OSError("synthetic-provider-failure")
        with patch.object(quality, "run", side_effect=error), self.assertRaises(OSError) as observed:
            run_docker_fixture(["synthetic-worker"], SCRIPTS)
        self.assertIs(observed.exception, error)
        self.assertFalse(hasattr(error, "__notes__"))


class ProcessBudgetEnvironmentTest(unittest.TestCase):
    @contextmanager
    def windows(self, *, system=r"C:\OS\System32", program_files=r"D:\Apps",
                system_length=None, folder_status=0, null_folder=False, lstat=None, budget=process_budget):
        class AbsoluteWindowsPath(PureWindowsPath):
            def absolute(path):
                self.assertTrue(path.is_absolute())
                return path

        kernel, shell, ole = Mock(), Mock(), Mock()
        folder_buffer = ctypes.create_unicode_buffer(program_files)
        folder_requests = []

        def system_directory(buffer, size):
            buffer.value = system
            return len(system) if system_length is None else system_length

        def known_folder(identifier, flags, token, output):
            folder_requests.append((ctypes.string_at(identifier, 16), flags, token))
            if not null_folder:
                ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(folder_buffer)
            return folder_status

        kernel.GetSystemDirectoryW.side_effect = system_directory
        shell.SHGetKnownFolderPath.side_effect = known_folder
        libraries = {"kernel32": kernel, "shell32.dll": shell, "ole32.dll": ole}
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0)
        operating_system = SimpleNamespace(
            name="nt", pathsep=";", environ={"JAVA_HOME": "ambient-relative"},
            lstat=Mock(side_effect=lstat) if lstat else Mock(return_value=directory),
        )
        with (
            patch.object(budget, "os", operating_system),
            patch.object(budget, "Path", AbsoluteWindowsPath),
            patch.object(budget, "sys",
                         SimpleNamespace(executable=r"C:\Python\python.exe", base_prefix=r"C:\Python")),
            patch.object(budget.ctypes, "WinDLL",
                         side_effect=lambda name, **kwargs: libraries[name], create=True) as loader,
        ):
            yield SimpleNamespace(os=operating_system, kernel=kernel, shell=shell, ole=ole,
                                  loader=loader, folder_requests=folder_requests)

    @contextmanager
    def posix(self, *, budget=process_budget):
        class AbsolutePosixPath(PurePosixPath):
            def absolute(path):
                self.assertTrue(path.is_absolute())
                return path

        operating_system = SimpleNamespace(name="posix", pathsep=":", environ={"CI": "ambient"})
        with (
            patch.object(budget, "os", operating_system),
            patch.object(budget, "Path", AbsolutePosixPath),
            patch.object(budget, "sys", SimpleNamespace(executable="/python/bin/python", base_prefix="/python")),
            patch.object(budget.ctypes, "WinDLL", create=True) as loader,
        ):
            yield SimpleNamespace(os=operating_system, loader=loader)

    def test_windows_module_order_is_os_derived_despite_poisoned_environment(self):
        supplied = {
            "PATH": "ambient-path", "Path": "other-ambient-path",
            "PSModulePath": "poison-one", "PSMODULEPATH": "poison-two", "pSmOdUlEpAtH": "poison-three",
            "ProgramFiles": "poison-four", "PROGRAMFILES": "poison-five", "pRoGrAmFiLeS": "poison-six",
            "SYSTEMROOT": "poison-seven", "WINDIR": "poison-eight", "COMSPEC": "poison-nine",
            "PENNILOGIC_PYTHON": "poison-ten", "JAVA_HOME": r"E:\Java", "CI": "1",
        }
        original = dict(supplied)
        with self.windows() as api:
            api.os.environ.update(supplied)
            result = process_budget.environment(supplied, runtime_dirs=[PureWindowsPath(r"E:\runtime\bin")])
        self.assertEqual(result.get("PSModulePath"),
                         r"C:\OS\System32\WindowsPowerShell\v1.0\Modules;D:\Apps\WindowsPowerShell\Modules")
        self.assertEqual([name for name in result if name.casefold() == "psmodulepath"], ["PSModulePath"])
        self.assertFalse(any(name.casefold() == "programfiles" for name in result))
        self.assertEqual(result["SYSTEMROOT"], r"C:\OS")
        self.assertEqual(result["WINDIR"], r"C:\OS")
        self.assertEqual(result["COMSPEC"], r"C:\OS\System32\cmd.exe")
        self.assertEqual(result["PENNILOGIC_PYTHON"], r"C:\Python\python.exe")
        self.assertEqual(result["PATH"].split(";"), [
            r"C:\Python", r"C:\Python\Scripts", r"E:\runtime\bin", r"E:\Java\bin",
            r"C:\OS\System32", r"C:\OS\System32\WindowsPowerShell\v1.0", r"C:\OS\System32\Wbem",
        ])
        self.assertEqual(result["CI"], "1")
        self.assertEqual(supplied, original)

    def test_repeated_sanitization_is_idempotent(self):
        with self.windows():
            directories = [PureWindowsPath(r"E:\runtime\bin")]
            first = process_budget.environment({"JAVA_HOME": r"E:\Java"}, runtime_dirs=directories)
            original = dict(first)
            second = process_budget.environment(first, runtime_dirs=directories)
        self.assertEqual(first, original)
        self.assertEqual(second, first)

    def test_known_folder_contract_frees_the_os_result(self):
        with self.windows() as api:
            process_budget.environment({})
            self.assertEqual(api.folder_requests, [
                (bytes.fromhex("b6635e90bfc14e49b29c65b732d3d21a"), 0, None),
            ])
            api.ole.CoTaskMemFree.assert_called_once()
            api.loader.assert_any_call("shell32.dll", use_last_error=True, winmode=0x800)
            api.loader.assert_any_call("ole32.dll", use_last_error=True, winmode=0x800)

    def test_optional_shared_directory_can_be_missing_without_creating_it(self):
        seen = []

        def available_ancestor(path):
            seen.append(str(path))
            if str(path) == r"D:\Apps\WindowsPowerShell":
                raise FileNotFoundError("optional shared directory absent")
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0)

        with self.windows(lstat=available_ancestor):
            result = process_budget.environment({})
        self.assertEqual(result["PSModulePath"].split(";"), [
            r"C:\OS\System32\WindowsPowerShell\v1.0\Modules", r"D:\Apps\WindowsPowerShell\Modules",
        ])
        self.assertIn(r"D:\Apps", seen)
        self.assertNotIn(r"D:\Apps\WindowsPowerShell\Modules", seen)

    def test_nonordinary_available_ancestors_are_refused(self):
        for mode, attributes in ((stat.S_IFREG | 0o644, 0), (stat.S_IFDIR | 0o755, 1024)):
            def metadata(path):
                return SimpleNamespace(
                    st_mode=mode if str(path) == r"D:\Apps" else stat.S_IFDIR | 0o755,
                    st_file_attributes=attributes if str(path) == r"D:\Apps" else 0,
                )

            with self.subTest(mode=mode, attributes=attributes), self.windows(lstat=metadata):
                with self.assertRaisesRegex(ValueError, "ordinary directory"):
                    process_budget.environment({})

    def test_missing_drive_or_unreadable_ancestor_never_reaches_native_start(self):
        for error in (FileNotFoundError("missing drive"), PermissionError("unreadable ancestor")):
            with (
                self.subTest(error=type(error).__name__),
                self.windows(lstat=Mock(side_effect=error)),
                patch.object(process_budget.subprocess, "Popen") as popen,
                patch.object(process_budget, "WindowsJob") as job,
            ):
                with self.assertRaises(type(error)):
                    process_budget.run([r"C:\Python\python.exe"], PureWindowsPath(r"C:\fixture"), 10, env={})
                popen.assert_not_called()
                job.assert_not_called()

    def test_unicode_os_roots_keep_their_spelling_and_order(self):
        program_files = "D:\\Apps\\\U0001f680"
        with self.windows(program_files=program_files):
            result = process_budget.environment({})
        self.assertEqual(result["PSModulePath"].split(";"), [
            r"C:\OS\System32\WindowsPowerShell\v1.0\Modules",
            program_files + r"\WindowsPowerShell\Modules",
        ])

    def test_os_failures_and_invalid_root_shapes_refuse_before_native_start(self):
        cases = [
            {"system_length": 0}, {"system_length": 32768},
            {"folder_status": -2147467259}, {"null_folder": True},
        ]
        invalid = ("relative", r"C:relative", r"\rooted", r"\\host\share", r"\\?\C:\OS",
                   "C:/OS", r"C:\OS;injected", r"C:\OS\..\redirect", r"C:\OS\trailing.",
                   r"C:\OS\space ", r"C:\PROGRA~1", r"C:\OS:stream", "C:\\OS\ninjected")
        cases.extend({key: value} for key in ("system", "program_files") for value in invalid)
        for options in cases:
            with (
                self.subTest(options=options), self.windows(**options) as api,
                patch.object(process_budget.subprocess, "Popen") as popen,
                patch.object(process_budget, "WindowsJob") as job,
            ):
                with self.assertRaises((OSError, ValueError)):
                    process_budget.run([r"C:\Python\python.exe"], PureWindowsPath(r"C:\fixture"),
                                       10, env={"ProgramFiles": r"D:\ambient-fallback"})
                popen.assert_not_called()
                job.assert_not_called()
                if options.get("folder_status") or "program_files" in options:
                    api.ole.CoTaskMemFree.assert_called_once()
                if options.get("null_folder"):
                    api.ole.CoTaskMemFree.assert_not_called()

    def test_explicit_empty_mapping_never_inherits_ambient_java_or_flags(self):
        with self.windows() as api:
            api.os.environ.update(CI="ambient", PSModulePath="ambient", ProgramFiles="ambient")
            result = process_budget.environment({})
        self.assertNotIn("JAVA_HOME", result)
        self.assertNotIn("CI", result)
        self.assertEqual(result["PSModulePath"].split(";")[1], r"D:\Apps\WindowsPowerShell\Modules")

    def test_run_preserves_explicit_runtime_path_without_launching_a_fixture(self):
        supplied = {"PATH": r"E:\runtime\bin", "PSModulePath": "ambient"}
        with (
            self.windows(),
            patch.object(process_budget, "resolve_command", side_effect=RuntimeError("source-only boundary")) as resolve,
            patch.object(process_budget.subprocess, "Popen") as popen,
            patch.object(process_budget, "WindowsJob") as job,
        ):
            with self.assertRaisesRegex(RuntimeError, "source-only boundary"):
                process_budget.run([r"C:\Python\python.exe"], PureWindowsPath(r"C:\fixture"), 10, env=supplied)
            child = resolve.call_args.args[1]
            self.assertIn(r"E:\runtime\bin", child["PATH"].split(";"))
            self.assertNotIn("ambient", child["PSModulePath"])
            popen.assert_not_called()
            job.assert_not_called()
        self.assertEqual(supplied, {"PATH": r"E:\runtime\bin", "PSModulePath": "ambient"})

    def test_existing_relative_java_and_runtime_refusals_are_preserved(self):
        for supplied, directories in (({"JAVA_HOME": "relative"}, ()), ({}, ("relative",))):
            with self.subTest(supplied=supplied), self.windows():
                with self.assertRaisesRegex(ValueError, "absolute"):
                    process_budget.environment(supplied, runtime_dirs=directories)

    def test_non_windows_environment_keeps_its_existing_explicit_mapping_policy(self):
        with self.posix() as api:
            result = process_budget.environment(
                {"HOME": "/home/fixture", "JAVA_HOME": "/jdk", "PATH": "/ambient", "PSModulePath": "ambient",
                 "PSMODULEPATH": "ambient-upper", "pSmOdUlEpAtH": "ambient-mixed",
                 "ProgramFiles": "ambient", "PROGRAMFILES": "ambient-upper", "pRoGrAmFiLeS": "ambient-mixed",
                 "PYTHONPATH": "/ambient"},
                runtime_dirs=[PurePosixPath("/runtime/bin")],
            )
            empty = process_budget.environment({})
            api.loader.assert_not_called()
        self.assertEqual(result, {
            "HOME": "/home/fixture", "JAVA_HOME": "/jdk", "PENNILOGIC_PYTHON": "/python/bin/python",
            "PATH": "/python/bin:/python/Scripts:/runtime/bin:/jdk/bin:/usr/local/bin:/usr/bin:/bin",
        })
        self.assertNotIn("CI", empty)
        self.assertNotIn("PSModulePath", empty)


class ProcessBudgetTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows Docker Desktop executable boundary")
    def test_budgeted_quality_admits_only_the_approved_docker_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved, ambient = root / "approved", root / "ambient"
            approved.mkdir()
            ambient.mkdir()
            system = process_budget.system_paths()[0]
            binary = (system / "cmd.exe").read_bytes()
            (approved / "docker.exe").write_bytes(binary)
            (ambient / "ambient-only.exe").write_bytes(binary)
            marker = root / "unexpected-startup"
            (ambient / "sitecustomize.py").write_text(
                f"__import__('pathlib').Path({str(marker)!r}).write_text('unexpected')", encoding="utf-8",
            )
            injected = {
                "PYTHONPATH": str(ambient), "JAVA_TOOL_OPTIONS": "synthetic-options",
                "NODE_OPTIONS": "synthetic-options", "AWS_SECRET_ACCESS_KEY": "synthetic-noncredential",
            }
            docker = Path(system.anchor) / "Program Files/Docker/Docker/resources/bin"
            environment = process_budget.environment
            admissions = []

            def controlled_environment(supplied=None, *, runtime_dirs=()):
                directories = list(runtime_dirs)
                if directories == [docker]:
                    admissions.append(docker)
                    directories = [approved]
                return environment(supplied, runtime_dirs=directories)

            code = (
                "import json,os,pathlib,shutil,subprocess,sys;"
                "tool=shutil.which('docker.exe');"
                "approved=bool(tool) and pathlib.Path(tool).resolve()==pathlib.Path("
                + repr(str(approved / "docker.exe")) + ").resolve();"
                "clean=not any(name in os.environ for name in " + repr(tuple(injected)) + ");"
                "clean=clean and shutil.which('ambient-only.exe') is None;"
                "status=subprocess.call([tool,'/d','/c','exit','0']) if approved else 23;"
                "print(json.dumps({'approved':status==0,'isolated':clean}));"
                "sys.exit(status if status else (0 if clean else 24))"
            )
            with (
                patch.dict(os.environ, {**injected, "PATH": str(ambient)}),
                patch.object(quality, "script_module", return_value=process_budget),
                patch.object(process_budget, "environment", side_effect=controlled_environment),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = run_docker_fixture([sys.executable, "-I", "-S", "-B", "-c", code], root)
            self.assertEqual(json.loads(result), {"approved": True, "isolated": True})
            self.assertEqual(admissions, [docker])
            self.assertFalse(marker.exists())
            self.assertEqual((approved / "docker.exe").read_bytes(), binary)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows failed Job assignment cleanup")
    def test_failed_native_attachment_reaps_bootstrap_and_refuses_interop_evidence(self):
        interop = quality.script_module("money_client_interop")
        for label, module in (("helper", process_budget), ("interop", interop.process_budget)):
            with self.subTest(caller=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "unexpected-command"
                worker = root / "owned-worker.py"
                worker.write_text(
                    f"__import__('pathlib').Path({str(marker)!r}).write_text('unexpected')", encoding="utf-8",
                )
                spawned = []
                popen, attach = subprocess.Popen, module.WindowsJob.attach

                def remember(*args, **kwargs):
                    process = popen(*args, **kwargs)
                    spawned.append(process)
                    return process

                def invalid_owned_job(job, process):
                    self.assertTrue(job.api.CloseHandle(job.handle))
                    try:
                        attach(job, process)
                    finally:
                        job.handle = None

                error = None
                started = time.monotonic()
                try:
                    with (
                        patch.object(module.subprocess, "Popen", side_effect=remember),
                        patch.object(module.WindowsJob, "attach", invalid_owned_job),
                    ):
                        try:
                            if label == "helper":
                                module.run([sys.executable, "-I", "-S", "-B", str(worker)],
                                           root, 2, capture=True, separate=True)
                            else:
                                with patch.object(interop, "__file__", str(worker)), patch.object(interop, "SECONDS", 2):
                                    interop.launch_run(root, Mock(java=None))
                        except (OSError, subprocess.TimeoutExpired) as failure:
                            error = failure
                    self.assertIsInstance(error, OSError)
                    self.assertEqual(str(error), "Cannot attach the launched process to its budget job")
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(len(spawned), 1)
                    self.assertIsNotNone(spawned[0].poll())
                    self.assertTrue(all(stream.closed for stream in (
                        spawned[0].stdin, spawned[0].stdout, spawned[0].stderr,
                    ) if stream is not None))
                    self.assertFalse(marker.exists())
                    if label == "interop":
                        self.assertEqual(interop.read_json(root / interop.AREA / "current.json")["status"], "refused")
                        self.assertFalse((root / interop.AREA / "run.lock").exists())
                finally:
                    for process in spawned:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=10)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
        self.assertEqual(process_budget.run(
            [sys.executable, "-I", "-S", "-B", "-c", "pass"], SCRIPTS, 10, capture=True,
        ).returncode, 0)

    def test_pipe_read_errors_cannot_become_successful_capture(self):
        for error in (OSError, ValueError):
            class BrokenStream:
                def read(self, _count):
                    raise error("synthetic-closed-stream")

            class Process:
                stdout, stderr = BrokenStream(), None

            with self.subTest(error=error.__name__), patch.object(process_budget.threading, "excepthook") as diagnostic:
                capture = process_budget.Capture(Process())
                for thread in capture.threads:
                    thread.join(timeout=1)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(capture.failed, "read-error-was-not-recorded")
                diagnostic.assert_not_called()

    def test_approved_system_shell_survives_without_inheriting_ambient_path(self):
        command = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output 'approved-shell'"]
                   if os.name == "nt" else ["/bin/sh", "-c", "printf 'approved-shell\\n'"])
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"PATH": temporary}):
            if os.name == "nt":
                boundary = SCRIPTS.parent / "build" / "diagnostics" / "api106-approved-shell-boundary.bin"
                for parent in (boundary.parent.parent, boundary.parent):
                    self.assertFalse(parent.is_symlink() or parent.is_junction(), "diagnostic-area-reparse-point")
                boundary.parent.mkdir(parents=True, exist_ok=True)
                with boundary.open("xb"):
                    pass
                lookup = boundary.with_name("api106-approved-shell-module-lookup.jsonl")
                try:
                    with lookup.open("xb"):
                        pass
                except OSError:
                    raise AssertionError("diagnostic-capsule-exclusive-create-refused") from None
                # One-shot diagnostic: keep checkpoints independent of the suspect output path.
                command[-1] = (
                    "$pennilogicBoundary='" + str(boundary).replace("'", "''") + "';"
                    "[IO.File]::AppendAllText($pennilogicBoundary,'E',[Text.Encoding]::ASCII);"
                    "$pennilogicLookupPath='" + str(lookup).replace("'", "''") + "';"
                    + APPROVED_SHELL_LOOKUP_SETUP +
                    "$i=[Console]::IsInputRedirected;$o=[Console]::IsOutputRedirected;"
                    "$e=[Console]::IsErrorRedirected;"
                    "$r=if(($i -is [bool])-and($o -is [bool])-and($e -is [bool]))"
                    "{'S{0}{1}{2}' -f [int]$i,[int]$o,[int]$e}else{'U'};"
                    "[IO.File]::AppendAllText($pennilogicBoundary,$r,[Text.Encoding]::ASCII);"
                    "if($r -cne 'U'){$script:pennilogicLookup.Boundary=$pennilogicBoundary};"
                    + command[-1] + ";"
                    "$script:pennilogicLookup.Boundary=$null;"
                    "[IO.File]::AppendAllText($pennilogicBoundary,'X',[Text.Encoding]::ASCII)"
                    + APPROVED_SHELL_LOOKUP_FINISH
                )
                if APPROVED_SHELL_PAIR_ENCLOSURE is not None:
                    return run_approved_shell_observer_pair(
                        self, command, boundary, lookup, enclosure=APPROVED_SHELL_PAIR_ENCLOSURE,
                    )
            result = process_budget.run(command, SCRIPTS, 10, capture=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "approved-shell")

    def test_bootstrap_and_worker_exclude_startup_hooks_and_ambient_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "unexpected-startup"
            (root / "sitecustomize.py").write_text(
                f"__import__('pathlib').Path({str(marker)!r}).write_text('synthetic-startup')",
                encoding="utf-8",
            )
            injected = {
                "PYTHONPATH": str(root), "JAVA_TOOL_OPTIONS": "synthetic-options",
                "NODE_OPTIONS": "synthetic-options", "AWS_SECRET_ACCESS_KEY": "synthetic-noncredential",
            }
            code = (
                "import os,json; print(json.dumps({'clean':not any(name in os.environ for name in "
                + repr(tuple(injected)) + ")}))"
            )
            with patch.dict(os.environ, injected):
                result = process_budget.run(
                    [sys.executable, "-I", "-S", "-B", "-c", code], root, 10, capture=True,
                )
            self.assertEqual(result.returncode, 0)
            self.assertFalse(marker.exists(), "unbound-startup-hook-ran")
            self.assertEqual(json.loads(result.stdout), {"clean": True})

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object startup ordering")
    def test_actual_command_waits_until_its_job_is_attached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "actual-command-started"
            attach = process_budget.WindowsJob.attach

            def delayed_attach(job, process):
                time.sleep(0.3)
                self.assertFalse(marker.exists())
                attach(job, process)

            with patch.object(process_budget.WindowsJob, "attach", delayed_attach):
                result = process_budget.run(
                    [sys.executable, "-c", f"__import__('pathlib').Path({str(marker)!r}).write_text('started')"],
                    root, 10, capture=True,
                )
            self.assertEqual(0, result.returncode)
            self.assertTrue(marker.is_file())

    def test_zero_negative_non_finite_and_boolean_budgets_refuse_to_launch(self):
        for seconds in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                process_budget.run([sys.executable, "-c", "pass"], SCRIPTS, seconds)

    def test_success_and_exit_failure_are_preserved(self):
        for code in (0, 3):
            result = process_budget.run([sys.executable, "-c", f"raise SystemExit({code})"], SCRIPTS, 10, capture=True)
            self.assertEqual(code, result.returncode)

    def test_timeout_terminates_only_the_owned_tree_and_restoration_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_marker = root / "child-finished"
            started_marker = root / "child-started"
            unrelated_marker = root / "unrelated-finished"
            child = f"import pathlib,time; time.sleep(3); pathlib.Path({str(child_marker)!r}).write_text('done')"
            parent = (
                "import subprocess,sys,time; "
                f"child=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                f"__import__('pathlib').Path({str(started_marker)!r}).write_text(str(child.pid)); "
                "print('owned-child-started', flush=True); time.sleep(60)"
            )
            unrelated = subprocess.Popen([
                sys.executable, "-c",
                f"import pathlib,time; time.sleep(2); pathlib.Path({str(unrelated_marker)!r}).write_text('done')",
            ])
            try:
                with self.assertRaisesRegex(TimeoutError, "elapsed budget") as raised:
                    process_budget.run([sys.executable, "-c", parent], root, 1, capture=True)
                self.assertIn("owned-child-started", raised.exception.output)
                self.assertTrue(started_marker.is_file())
                unrelated.wait(timeout=10)
                time.sleep(2.2)
                self.assertTrue(unrelated_marker.is_file())
                self.assertFalse(child_marker.exists())
                self.assertEqual(
                    0, process_budget.run([sys.executable, "-c", "pass"], root, 10, capture=True).returncode,
                )
            finally:
                if unrelated.poll() is None:
                    unrelated.kill()
                    unrelated.wait(timeout=10)

    def test_successful_parent_cannot_leave_an_unbounded_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "unexpected-child-completion"
            child = f"import pathlib,time; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('done')"
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            )
            result = process_budget.run([sys.executable, "-c", parent], root, 10, capture=True)
            self.assertEqual(0, result.returncode)
            time.sleep(2.5)
            self.assertFalse(marker.exists())

    def test_normal_quality_build_is_budgeted_and_contains_no_mutation_skip(self):
        provider = SyntheticStrategy()
        with (
            patch.object(sys, "argv", ["quality.py", "build"]),
            patch.object(quality, "money_provider_module", return_value=provider),
            patch.object(quality, "gradle") as gradle,
            patch.object(quality, "test_metrics"),
            patch.object(quality, "script_module") as loader,
        ):
            self.assertEqual(0, quality.main())
            self.assertEqual(("build", "installDist"), gradle.call_args.args)
            self.assertGreater(gradle.call_args.kwargs["budget"], 0)
            self.assertLessEqual(gradle.call_args.kwargs["budget"], 600)
            loader.assert_called_once_with("money_mutation")
            loader.return_value.check_latest.assert_called_once_with()

    def test_budget_overrun_after_the_build_also_fails(self):
        with (
            patch.object(sys, "argv", ["quality.py", "build"]),
            patch.object(quality, "money_provider_module", return_value=SyntheticStrategy()),
            patch.object(quality, "gradle"),
            patch.object(quality, "test_metrics"),
            patch.object(quality, "script_module"),
            patch.object(quality.time, "monotonic", side_effect=[0, 0, 601, 602]),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            self.assertEqual(1, quality.main())
            self.assertIn("exceeded", errors.getvalue())


class ProcessBudgetObservationTest(unittest.TestCase):
    PREFIX = "process_budget_state="
    SCHEMA = "pennilogic.process-budget-state/1"

    def state(self, note):
        self.assertIs(type(note), str)
        self.assertTrue(note.startswith(self.PREFIX))
        self.assertLessEqual(len(note.encode("ascii")), 384)
        value = json.loads(note[len(self.PREFIX):])
        self.assertEqual(value["schema"], self.SCHEMA)
        self.assertEqual(note, self.PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":")))
        if value["status"] == "unavailable":
            self.assertEqual(set(value), {"schema", "status"})
            return value
        self.assertEqual(value["status"], "observed")
        self.assertEqual(set(value), {
            "schema", "status", "process_running", "returncode", "capture_readers",
            "capture_readers_alive", "setup_ms", "wait_ms", "elapsed_ms",
        })
        self.assertIs(type(value["process_running"]), bool)
        self.assertEqual(value["process_running"], value["returncode"] is None)
        if value["returncode"] is not None:
            self.assertIs(type(value["returncode"]), int)
            self.assertGreaterEqual(value["returncode"], -2147483648)
            self.assertLessEqual(value["returncode"], 4294967295)
        for name in ("capture_readers", "capture_readers_alive"):
            self.assertIs(type(value[name]), int)
            self.assertIn(value[name], range(3))
        self.assertLessEqual(value["capture_readers_alive"], value["capture_readers"])
        for name in ("setup_ms", "wait_ms", "elapsed_ms"):
            self.assertIs(type(value[name]), int)
            self.assertGreaterEqual(value[name], 0)
            self.assertLessEqual(value[name], 86400000)
        self.assertEqual(value["setup_ms"] + value["wait_ms"], value["elapsed_ms"])
        return value

    def timeout_state(self, *, parent_exits=False, capture=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "synthetic-private-child-started"
            worker = "import time; time.sleep(60)"
            if parent_exits:
                child = f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('started'); time.sleep(60)"
                worker = "import subprocess,sys; " + f"subprocess.Popen([sys.executable,'-I','-S','-B','-c',{child!r}])"
            if capture:
                worker = "print('synthetic-private-output',flush=True); " + worker
            processes, captures = [], []
            popen, capture_class = subprocess.Popen, process_budget.Capture

            def remember(*args, **kwargs):
                process = popen(*args, **kwargs)
                processes.append(process)
                return process

            def remember_capture(process):
                value = capture_class(process)
                captures.append(value)
                return value

            with (
                patch.object(process_budget.subprocess, "Popen", side_effect=remember),
                patch.object(process_budget, "Capture", side_effect=remember_capture),
                self.assertRaisesRegex(process_budget.BudgetExceeded, "enforced elapsed budget") as raised,
            ):
                process_budget.run([sys.executable, "-I", "-S", "-B", "-c", worker],
                                   root, 1, capture=capture, separate=True)
            self.assertIs(type(raised.exception), process_budget.BudgetExceeded)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(all(stream.closed for stream in (
                processes[0].stdin, processes[0].stdout, processes[0].stderr,
            ) if stream is not None))
            self.assertEqual(len(captures), int(capture))
            self.assertTrue(all(not thread.is_alive() for value in captures for thread in value.threads))
            if capture:
                self.assertIn(b"synthetic-private-output", raised.exception.output)
                self.assertEqual(raised.exception.stderr, b"")
                self.assertEqual(raised.exception.streams["stdout"]["bytes"], len(raised.exception.output))
            if parent_exits:
                self.assertTrue(marker.is_file())
            notes = getattr(raised.exception, "__notes__", [])
            self.assertEqual(len(notes), 3 if os.name == "nt" else 2)
            capture_state = ProcessBudgetCaptureObservationTest().state(notes[0])
            if capture:
                self.assertEqual(capture_state, {
                    "schema": ProcessBudgetCaptureObservationTest.SCHEMA,
                    "status": "observed", "scope": "snapshot", "complete": False, "mode": "separate",
                    "stdout": raised.exception.streams["stdout"],
                    "stderr": raised.exception.streams["stderr"],
                })
                for name, output in (("stdout", raised.exception.output), ("stderr", raised.exception.stderr)):
                    self.assertIs(type(output), bytes)
                    self.assertEqual(capture_state[name], {
                        "bytes": len(output), "sha256": hashlib.sha256(output).hexdigest(),
                    })
            else:
                self.assertEqual(capture_state, {
                    "schema": ProcessBudgetCaptureObservationTest.SCHEMA, "status": "unavailable",
                })
                self.assertIsNone(raised.exception.output)
                self.assertIsNone(raised.exception.stderr)
                self.assertIsNone(raised.exception.streams)
            if os.name == "nt":
                ProcessBudgetJobObservationTest().decode(notes[-2])
            for note in notes:
                for private in ("synthetic-private", str(root), str(sys.executable), "time.sleep"):
                    self.assertNotIn(private, note)
            return self.state(notes[-1])

    def test_running_process_is_observed_before_termination_with_setup_time(self):
        environment = process_budget.environment

        def delayed_environment(*args, **kwargs):
            time.sleep(0.2)
            return environment(*args, **kwargs)

        with patch.object(process_budget, "environment", side_effect=delayed_environment):
            state = self.timeout_state()
        self.assertEqual(state["status"], "observed")
        self.assertTrue(state["process_running"])
        self.assertIsNone(state["returncode"])
        self.assertEqual((state["capture_readers"], state["capture_readers_alive"]), (2, 2))
        self.assertGreaterEqual(state["setup_ms"], 200)
        self.assertGreater(state["wait_ms"], 0)
        self.assertGreaterEqual(state["elapsed_ms"], 1000)

    def test_exited_parent_with_held_pipe_is_observed_before_termination(self):
        state = self.timeout_state(parent_exits=True)
        self.assertEqual(state["status"], "observed")
        self.assertFalse(state["process_running"])
        self.assertEqual(state["returncode"], 0)
        self.assertEqual((state["capture_readers"], state["capture_readers_alive"]), (2, 2))
        self.assertGreaterEqual(state["elapsed_ms"], 1000)

    def test_unavailable_sample_preserves_original_timeout_and_cleanup(self):
        observe = process_budget.budget_observation

        for malformed in (False, True):
            def unavailable(_process, captured, started, waiting):
                process = Mock()
                if malformed:
                    process.poll.return_value = True
                else:
                    process.poll.side_effect = OSError("synthetic-private-observation-error")
                return observe(process, captured, started, waiting)

            with self.subTest(malformed=malformed), patch.object(process_budget, "budget_observation", side_effect=unavailable):
                self.assertEqual(self.timeout_state(), {"schema": self.SCHEMA, "status": "unavailable"})

    def test_timeout_without_capture_has_no_readers(self):
        state = self.timeout_state(capture=False)
        self.assertTrue(state["process_running"])
        self.assertEqual((state["capture_readers"], state["capture_readers_alive"]), (0, 0))

    def test_valid_samples_are_exact_and_bounded(self):
        for code in (None, -2147483648, 0, 4294967295):
            for alive in ([], [False], [True], [False, True], [True, True]):
                process = Mock()
                process.poll.return_value = code
                captured = Mock(threads=[Mock(is_alive=Mock(return_value=value)) for value in alive])
                with self.subTest(code=code, alive=alive), patch.object(process_budget.time, "monotonic", return_value=86400):
                    state = self.state(process_budget.budget_observation(process, captured, 0, 1))
                self.assertEqual(state, {
                    "schema": self.SCHEMA, "status": "observed", "process_running": code is None,
                    "returncode": code, "capture_readers": len(alive), "capture_readers_alive": sum(alive),
                    "setup_ms": 1000, "wait_ms": 86399000, "elapsed_ms": 86400000,
                })

    def test_malformed_and_unavailable_samples_never_emit_private_values(self):
        cases = [
            {"code": value} for value in (False, True, "synthetic-private-code", -2147483649, 4294967296, 1.0)
        ] + [
            {"alive": [True, True, True]}, {"alive": [1]}, {"alive": ["synthetic-private-reader"]},
            {"started": True}, {"started": float("nan")}, {"waiting": float("inf")},
            {"waiting": -1}, {"waiting": 3}, {"now": 86400.001},
            {"poll_error": OSError}, {"poll_error": ValueError}, {"reader_error": RuntimeError},
        ]
        for case in cases:
            process = Mock()
            process.poll.return_value = case.get("code")
            if "poll_error" in case:
                process.poll.side_effect = case["poll_error"]("synthetic-private-observation-error")
            readers = [Mock(is_alive=Mock(return_value=value)) for value in case.get("alive", [True])]
            if "reader_error" in case:
                readers[0].is_alive.side_effect = case["reader_error"]("synthetic-private-reader-error")
            captured = Mock(threads=readers)
            with self.subTest(case=case), patch.object(process_budget.time, "monotonic", return_value=case.get("now", 2)):
                note = process_budget.budget_observation(process, captured, case.get("started", 0), case.get("waiting", 1))
            self.assertEqual(self.state(note), {"schema": self.SCHEMA, "status": "unavailable"})
            self.assertNotIn("synthetic-private", note)

    def test_success_and_nonzero_exit_neither_collect_nor_emit_state(self):
        for code in (0, 3):
            with (
                self.subTest(code=code),
                patch.object(process_budget, "budget_observation", side_effect=AssertionError("unexpected observation")) as observe,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                result = process_budget.run(
                    [sys.executable, "-I", "-S", "-B", "-c",
                     f"import sys; print('out'); print('err',file=sys.stderr); sys.exit({code})"],
                    SCRIPTS, 10, capture=True, separate=True,
                )
            observe.assert_not_called()
            self.assertEqual(result.returncode, code)
            self.assertEqual((result.stdout, result.stderr), (b"out\r\n", b"err\r\n") if os.name == "nt" else (b"out\n", b"err\n"))
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_output_limit_does_not_collect_elapsed_state(self):
        with (
            patch.object(process_budget, "MAX_CAPTURE_BYTES", 1024),
            patch.object(process_budget, "budget_observation", side_effect=AssertionError("unexpected observation")) as observe,
            self.assertRaises(process_budget.OutputExceeded) as raised,
        ):
            process_budget.run([sys.executable, "-I", "-S", "-B", "-c",
                                "import sys; sys.stdout.buffer.write(b'x'*4096)"], SCRIPTS, 10, capture=True)
        observe.assert_not_called()
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])

    def test_capture_failure_does_not_collect_elapsed_state(self):
        read = process_budget.Capture.read

        class BrokenStream:
            def read(self, _count):
                raise OSError("synthetic-private-capture-error")

        def fail_stderr(capture, name, stream):
            read(capture, name, BrokenStream() if name == "stderr" else stream)

        with (
            patch.object(process_budget.Capture, "read", fail_stderr),
            patch.object(process_budget, "budget_observation", side_effect=AssertionError("unexpected observation")) as observe,
            self.assertRaisesRegex(OSError, "^Owned process output capture failed$") as raised,
        ):
            process_budget.run([sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(60)"],
                               SCRIPTS, 10, capture=True, separate=True)
        observe.assert_not_called()
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])


class ProcessBudgetCaptureObservationTest(unittest.TestCase):
    PREFIX = "process_budget_capture_state="
    SCHEMA = "pennilogic.process-budget-capture-state/1"
    EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def metadata(self, stdout=b"", stderr=b""):
        return {name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for name, value in (("stdout", stdout), ("stderr", stderr))}

    def state(self, note):
        self.assertIs(type(note), str)
        self.assertLessEqual(len(note.encode("ascii")), 512)
        self.assertTrue(note.startswith(self.PREFIX))
        value = json.loads(note[len(self.PREFIX):])
        self.assertEqual(note, self.PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":")))
        self.assertEqual(value["schema"], self.SCHEMA)
        if value["status"] == "unavailable":
            self.assertEqual(set(value), {"schema", "status"})
            return value
        self.assertEqual(set(value), {"schema", "status", "scope", "complete", "mode", "stdout", "stderr"})
        self.assertEqual(value["status"], "observed")
        self.assertEqual(value["scope"], "snapshot")
        self.assertIs(value["complete"], False)
        self.assertIn(value["mode"], ("merged", "separate"))
        for name in ("stdout", "stderr"):
            stream = value[name]
            self.assertEqual(set(stream), {"bytes", "sha256"})
            self.assertIs(type(stream["bytes"]), int)
            self.assertTrue(0 <= stream["bytes"] <= 18446744073709551615)
            self.assertIs(type(stream["sha256"]), str)
            self.assertRegex(stream["sha256"], r"\A[0-9a-f]{64}\Z")
            if stream["bytes"] == 0:
                self.assertEqual(stream["sha256"], self.EMPTY_SHA256)
        if value["mode"] == "merged":
            self.assertEqual(value["stderr"], {"bytes": 0, "sha256": self.EMPTY_SHA256})
        return value

    def test_observed_zero_bytes_are_distinct_from_unavailable_capture(self):
        for separate in (False, True):
            with self.subTest(separate=separate):
                state = self.state(process_budget.capture_observation(self.metadata(), separate=separate))
                self.assertEqual(state["status"], "observed")
                self.assertEqual(state["mode"], "separate" if separate else "merged")
                self.assertEqual((state["stdout"]["bytes"], state["stderr"]["bytes"]), (0, 0))
                self.assertEqual(self.state(process_budget.capture_observation(None, separate=separate)),
                                 {"schema": self.SCHEMA, "status": "unavailable"})

    def test_merged_stderr_is_an_unused_slot_not_an_independent_stream(self):
        streams = self.metadata(b"out\nerr\n")
        state = self.state(process_budget.capture_observation(streams, separate=False))
        self.assertEqual(state["mode"], "merged")
        self.assertEqual(state["stdout"], streams["stdout"])
        self.assertEqual(state["stderr"], {"bytes": 0, "sha256": self.EMPTY_SHA256})
        note = process_budget.capture_observation(self.metadata(b"out", b"err"), separate=False)
        self.assertEqual(self.state(note), {"schema": self.SCHEMA, "status": "unavailable"})

    def test_separate_snapshot_projects_existing_hashes_without_rehash_or_queries(self):
        streams = self.metadata(b"\xffsynthetic-private-output", b"synthetic-private-error")
        before = json.dumps(streams, sort_keys=True)
        with (
            patch.object(process_budget.subprocess, "Popen", side_effect=AssertionError("unexpected launch")),
            patch.object(process_budget, "WindowsJob", side_effect=AssertionError("unexpected Job")),
            patch.object(process_budget.time, "monotonic", side_effect=AssertionError("unexpected clock")),
            patch.object(process_budget.hashlib, "sha256", side_effect=AssertionError("unexpected rehash")),
        ):
            note = process_budget.capture_observation(streams, separate=True)
        state = self.state(note)
        self.assertEqual((state["stdout"], state["stderr"]), (streams["stdout"], streams["stderr"]))
        self.assertEqual(json.dumps(streams, sort_keys=True), before)
        self.assertNotIn("synthetic-private", note)

    def test_uint64_boundaries_fit_the_exact_canonical_envelope(self):
        for stdout in (0, 1, 18446744073709551615):
            for stderr in (0, 1, 18446744073709551615):
                streams = {name: {"bytes": count, "sha256": "f" * 64 if count else self.EMPTY_SHA256}
                           for name, count in (("stdout", stdout), ("stderr", stderr))}
                with self.subTest(stdout=stdout, stderr=stderr):
                    note = process_budget.capture_observation(streams, separate=True)
                    state = self.state(note)
                    self.assertEqual((state["stdout"], state["stderr"]), (streams["stdout"], streams["stderr"]))
                    self.assertLessEqual(len(note), 389)
        self.assertEqual(len(note), 389)
        self.assertEqual(len(process_budget.CAPTURE_STATE_UNAVAILABLE.encode("ascii")), 106)

    def test_invalid_modes_counts_and_hashes_never_become_observed(self):
        for mode in (None, 0, 1, "synthetic-private-mode", [], {}):
            with self.subTest(mode=mode):
                self.assertEqual(process_budget.capture_observation(self.metadata(), separate=mode),
                                 process_budget.CAPTURE_STATE_UNAVAILABLE)
        counts = (False, True, None, -1, 18446744073709551616, 1.0, float("nan"),
                  float("inf"), "synthetic-private-count")
        hashes = (False, True, None, b"a" * 64, "a" * 63, "a" * 65, "A" * 64,
                  "g" * 64, "\u2603" * 64, "a" * 63 + "\n", "synthetic-private-path")
        for name in ("stdout", "stderr"):
            for key, values in (("bytes", counts), ("sha256", hashes)):
                for value in values:
                    streams = self.metadata(b"out", b"err")
                    streams[name][key] = value
                    with self.subTest(stream=name, field=key, value=value):
                        self.assertEqual(process_budget.capture_observation(streams, separate=True),
                                         process_budget.CAPTURE_STATE_UNAVAILABLE)
            streams = self.metadata()
            streams[name]["sha256"] = "a" * 64
            with self.subTest(zero_with_wrong_hash=name):
                self.assertEqual(process_budget.capture_observation(streams, separate=True),
                                 process_budget.CAPTURE_STATE_UNAVAILABLE)

    def test_malformed_shapes_and_extra_private_fields_are_unavailable(self):
        class NonPlainDict(dict):
            pass

        cases = [None, False, [], "synthetic-private-metadata", {}, {"stdout": self.metadata()["stdout"]},
                 {**self.metadata(), "command": "synthetic-private-command"}, NonPlainDict(self.metadata())]
        for name in ("stdout", "stderr"):
            for value in (None, [], False, "synthetic-private-stream", {},
                          {"bytes": 0}, {"sha256": self.EMPTY_SHA256},
                          {"bytes": 0, "sha256": self.EMPTY_SHA256, "path": "synthetic-private-path"},
                          NonPlainDict(self.metadata()[name])):
                cases.append({**self.metadata(), name: value})
        for index, streams in enumerate(cases):
            with self.subTest(case=index):
                note = process_budget.capture_observation(streams, separate=True)
                self.assertEqual(note, process_budget.CAPTURE_STATE_UNAVAILABLE)
                self.assertEqual(self.state(note), {"schema": self.SCHEMA, "status": "unavailable"})
                self.assertNotIn("synthetic-private", note)

    def test_encoding_refusals_keep_the_bounded_unavailable_note(self):
        for effect in (OSError("synthetic-private"), RuntimeError("synthetic-private"),
                       AttributeError("synthetic-private"), KeyError("synthetic-private"),
                       TypeError("synthetic-private"), ValueError("synthetic-private"),
                       OverflowError("synthetic-private"), "x" * 513, "\u2603", None):
            streams = self.metadata()
            options = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with self.subTest(effect=type(effect).__name__), patch.object(process_budget.json, "dumps", **options):
                note = process_budget.capture_observation(streams, separate=True)
            self.assertEqual(note, process_budget.CAPTURE_STATE_UNAVAILABLE)
            self.assertEqual(self.state(note), {"schema": self.SCHEMA, "status": "unavailable"})


class FakeJobObservationApi:
    JOB, BOOTSTRAP = 9000, 9001
    COMSPEC = r"C:\Windows\System32\cmd.exe"
    SHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    def __init__(self, pids=(11, 22, 33)):
        self.pids = list(pids)
        self.total = self.active = self.assigned = self.listed = len(pids)
        self.query_errors, self.query_lengths = {}, {}
        self.last_error, self.events, self.opened = 0, [], set()
        self.entries = {
            pid: {"bootstrap": pid == 11, "image": self.SHELL if pid == 22 else r"C:\synthetic-private\worker.exe"}
            for pid in pids
        }
        for name, handler in (
            ("QueryInformationJobObject", self.query), ("OpenProcess", self.open),
            ("IsProcessInJob", self.membership), ("CompareObjectHandles", self.compare),
            ("QueryFullProcessImageNameW", self.image), ("CloseHandle", self.close),
        ):
            setattr(self, name, Mock(side_effect=handler))
        self.kernel32 = SimpleNamespace(**{
            name: getattr(self, name) for name in (
                "QueryInformationJobObject", "OpenProcess", "IsProcessInJob", "QueryFullProcessImageNameW", "CloseHandle",
            )
        })
        self.kernelbase = SimpleNamespace(CompareObjectHandles=self.CompareObjectHandles)
        self.load_library = Mock(side_effect=self.library)

    def library(self, name, *, use_last_error, winmode=None):
        assert use_last_error is True
        if name == "kernel32":
            assert winmode is None
            return self.kernel32
        assert name == "KernelBase.dll" and winmode == 0x800
        return self.kernelbase

    def query(self, job, kind, output, size, returned):
        assert job == self.JOB and kind in (1, 3)
        value = output._obj
        self.events.append(("query", kind, size))
        assert size == ctypes.sizeof(value)
        if kind == 1:
            assert size == 48
            value.total, value.active = self.total, self.active
            length = size
        else:
            assert type(value).pids.offset == 8
            assert size == 8 + 16 * ctypes.sizeof(ctypes.c_size_t)
            value.assigned, value.listed = self.assigned, self.listed
            for index, pid in enumerate(self.pids[:16]):
                value.pids[index] = pid
            length = 8 + min(self.listed, 16) * ctypes.sizeof(ctypes.c_size_t)
        returned._obj.value = self.query_lengths.get(kind, length)
        self.last_error = self.query_errors.get(kind, 0)
        if self.last_error == "raise":
            raise OSError("synthetic-private-query")
        return int(not self.last_error)

    def open(self, access, inherit, pid):
        assert access == 0x1000 and inherit is False and pid in self.pids
        self.events.append(("open", pid))
        error = self.entries[pid].get("open_error")
        if error == "raise":
            raise OSError("synthetic-private-open")
        if error:
            return 0
        handle = pid + 100000
        self.opened.add(handle)
        return handle

    def membership(self, handle, job, belongs):
        assert handle in self.opened and job == self.JOB
        self.events.append(("membership", handle))
        entry = self.entries[handle - 100000]
        states = entry.get("membership")
        state = states.pop(0) if states else True
        if state == "raise":
            raise OSError("synthetic-private-membership")
        if state == "denied":
            return 0
        belongs._obj.value = int(state)
        return 1

    def compare(self, handle, bootstrap):
        assert handle in self.opened and bootstrap == self.BOOTSTRAP
        self.events.append(("compare", handle))
        entry = self.entries[handle - 100000]
        if entry.get("compare_error"):
            raise OSError("synthetic-private-object")
        return int(entry["bootstrap"])

    def image(self, handle, flags, buffer, size):
        assert handle in self.opened and flags == 0 and size._obj.value == len(buffer) == 32768
        self.events.append(("image", handle))
        entry = self.entries[handle - 100000]
        if entry.get("image_error") == "raise":
            raise OSError("synthetic-private-image")
        if entry.get("image_error"):
            return 0
        if not entry.get("no_image_write"):
            buffer.value = entry["image"]
        size._obj.value = entry.get("image_length", len(entry["image"]))
        return 1

    def close(self, handle):
        assert handle in self.opened and handle not in (self.JOB, self.BOOTSTRAP)
        self.events.append(("close", handle))
        self.opened.remove(handle)
        refusal = self.entries[handle - 100000].get("close_error")
        if refusal == "raise":
            raise OSError("synthetic-private-close")
        return int(not refusal)


class ProcessBudgetJobObservationTest(unittest.TestCase):
    def job(self, api):
        job = process_budget.WindowsJob.__new__(process_budget.WindowsJob)
        job.api, job.handle = api.kernel32, api.JOB
        return job

    def decode(self, note):
        self.assertIs(type(note), str)
        self.assertLessEqual(len(note.encode("ascii")), 512)
        self.assertTrue(note.startswith(process_budget.JOB_STATE_PREFIX))
        value = json.loads(note[len(process_budget.JOB_STATE_PREFIX):])
        self.assertEqual(note, process_budget.JOB_STATE_PREFIX + json.dumps(
            value, sort_keys=True, separators=(",", ":"),
        ))
        self.assertEqual(set(value), {"schema", "accounting", "members"})
        self.assertEqual(value["schema"], "pennilogic.process-budget-job-state/1")
        for private in ("synthetic-private", r"C:\Windows", "powershell.exe", "worker.exe", "9000", "9001"):
            self.assertNotIn(private, note)
        accounting, members = value["accounting"], value["members"]
        if accounting["status"] == "observed":
            self.assertEqual(set(accounting), {"status", "total_assigned", "active"})
            for key in ("total_assigned", "active"):
                self.assertIs(type(accounting[key]), int)
                self.assertTrue(0 <= accounting[key] <= 4294967295)
            self.assertLessEqual(accounting["active"], accounting["total_assigned"])
        else:
            self.assertIn(accounting["status"], ("unavailable", "unsupported"))
            self.assertEqual(set(accounting), {"status"})
        if members["status"] in ("observed", "partial"):
            self.assertEqual(set(members), {
                "status", "listed", "bootstrap", "system_approved_shell", "other", "unclassified",
            })
            for key in set(members) - {"status"}:
                self.assertIs(type(members[key]), int)
                self.assertIn(members[key], range(17))
            self.assertLessEqual(members["bootstrap"], 1)
            self.assertEqual(members["listed"], sum(
                members[key] for key in ("bootstrap", "system_approved_shell", "other", "unclassified")
            ))
            if members["status"] == "observed":
                self.assertEqual(members["unclassified"], 0)
                if accounting["status"] == "observed":
                    self.assertEqual(members["listed"], accounting["active"])
        else:
            self.assertIn(members["status"], ("unavailable", "unsupported", "oversized"))
            self.assertEqual(set(members), {"status"})
        return value

    def observe(self, api, *, command=None, environment=None, job=None):
        job = self.job(api) if job is None else job
        with (
            patch.object(process_budget.ctypes, "get_last_error", side_effect=lambda: api.last_error, create=True),
            patch.object(process_budget.ctypes, "WinDLL", side_effect=api.load_library, create=True),
        ):
            note = job.observation(
                SimpleNamespace(pid=11, _handle=api.BOOTSTRAP),
                [api.SHELL] if command is None else command,
                {"COMSPEC": api.COMSPEC} if environment is None else environment,
            )
        self.assertFalse(api.opened)
        return self.decode(note)

    def test_comparison_uses_documented_system_provider_with_separate_exports(self):
        api = FakeJobObservationApi()
        self.assertFalse(hasattr(api.kernel32, "CompareObjectHandles"))
        self.assertEqual(set(vars(api.kernelbase)), {"CompareObjectHandles"})
        job = self.job(api)
        value = self.observe(api, job=job)
        self.assertEqual(value["members"], {
            "status": "observed", "listed": 3, "bootstrap": 1,
            "system_approved_shell": 1, "other": 1, "unclassified": 0,
        })
        self.assertEqual(value["accounting"], {"status": "observed", "total_assigned": 3, "active": 3})
        api.load_library.assert_called_once_with("KernelBase.dll", use_last_error=True, winmode=0x800)
        self.assertIs(job.api, api.kernel32)
        self.assertEqual(api.QueryInformationJobObject.call_count, 2)
        self.assertEqual(api.CompareObjectHandles.call_count, 3)
        self.assertEqual(api.CloseHandle.call_count, 3)
        self.assertEqual(api.CompareObjectHandles.argtypes, [ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE])
        self.assertIs(api.CompareObjectHandles.restype, ctypes.c_int32)

    def test_job_construction_and_attachment_do_not_load_the_comparison_provider(self):
        api = FakeJobCleanupApi()
        with (
            patch.object(process_budget.ctypes, "WinDLL", return_value=api, create=True) as load,
            patch.object(process_budget.time, "monotonic", side_effect=lambda: api.clock),
        ):
            job = process_budget.WindowsJob()
            job.attach(SimpleNamespace(pid=11, _handle=api.BOOTSTRAP))
            job.close()
        load.assert_called_once_with("kernel32", use_last_error=True)
        api.OpenProcess.assert_not_called()
        api.AssignProcessToJobObject.assert_called_once_with(api.JOB, api.BOOTSTRAP)
        self.assertEqual([call.args[0] for call in api.CloseHandle.call_args_list], [api.JOB, api.PORT])
        self.assertIsNone(job.handle)
        api.CompareObjectHandles.assert_not_called()
        self.assertTrue(all(call.args[1] == 1 for call in api.QueryInformationJobObject.call_args_list))

    def test_comparison_provider_is_not_loaded_without_usable_members(self):
        for case in ("missing-job", "empty", "oversized", "refused"):
            with self.subTest(case=case):
                api = FakeJobObservationApi(() if case == "empty" else (11, 22))
                job = self.job(api)
                if case == "missing-job":
                    job.handle = None
                elif case == "oversized":
                    api.assigned = 17
                elif case == "refused":
                    api.query_errors[3] = 5
                self.observe(api, job=job)
                api.load_library.assert_not_called()
                api.OpenProcess.assert_not_called()

    def test_missing_comparison_provider_and_export_preserve_component_refusals(self):
        for missing, expected in (("library", "unavailable"), ("export", "unsupported")):
            with self.subTest(missing=missing):
                api = FakeJobObservationApi()
                if missing == "library":
                    api.load_library.side_effect = OSError("synthetic-private-missing-provider")
                else:
                    del api.kernelbase.CompareObjectHandles
                value = self.observe(api)
                self.assertEqual(value["accounting"], {"status": "observed", "total_assigned": 3, "active": 3})
                self.assertEqual(value["members"], {"status": expected})
                api.load_library.assert_called_once_with("KernelBase.dll", use_last_error=True, winmode=0x800)
                self.assertEqual(api.QueryInformationJobObject.call_count, 2)
                api.OpenProcess.assert_not_called()
                api.CompareObjectHandles.assert_not_called()
                api.CloseHandle.assert_not_called()

    def test_exact_wire_roles_and_fixed_queries_use_only_owned_handles(self):
        api = FakeJobObservationApi()
        value = self.observe(api)
        self.assertEqual(value["accounting"], {"status": "observed", "total_assigned": 3, "active": 3})
        self.assertEqual(value["members"], {
            "status": "observed", "listed": 3, "bootstrap": 1,
            "system_approved_shell": 1, "other": 1, "unclassified": 0,
        })
        self.assertEqual([call.args[1] for call in api.QueryInformationJobObject.call_args_list], [1, 3])
        self.assertEqual(api.OpenProcess.call_count, 3)
        self.assertEqual(api.IsProcessInJob.call_count, 6)
        self.assertEqual(api.CompareObjectHandles.call_count, 3)
        self.assertEqual(api.QueryFullProcessImageNameW.call_count, 2)
        self.assertEqual(api.CloseHandle.call_count, 3)
        for pid in api.pids:
            handle = pid + 100000
            events = [event[0] for event in api.events if event[1] == handle]
            self.assertEqual(events, ["membership", "compare", *([] if pid == 11 else ["image"]),
                                      "membership", "close"])
        self.assertEqual(ctypes.sizeof(api.IsProcessInJob.argtypes[2]._type_), 4)

    def test_bootstrap_requires_retained_object_identity_not_pid_or_image(self):
        api = FakeJobObservationApi((11, 22))
        api.entries[11].update(bootstrap=False, image=r"C:\synthetic-private\python.exe")
        api.entries[22]["bootstrap"] = True
        value = self.observe(api)
        self.assertEqual((value["members"]["bootstrap"], value["members"]["other"]), (1, 1))
        self.assertEqual([call.args[0] for call in api.QueryFullProcessImageNameW.call_args_list], [100011])
        api = FakeJobObservationApi((11, 22))
        api.entries[22]["bootstrap"] = True
        self.assertEqual(self.observe(api)["members"], {"status": "unavailable"})
        self.assertEqual(api.CloseHandle.call_count, 2)

    def test_only_the_positive_requested_system_shell_image_is_approved(self):
        for requested, image, expected in (
            (FakeJobObservationApi.SHELL, FakeJobObservationApi.SHELL.upper(), "system_approved_shell"),
            (FakeJobObservationApi.COMSPEC, FakeJobObservationApi.COMSPEC, "system_approved_shell"),
            (FakeJobObservationApi.SHELL, FakeJobObservationApi.COMSPEC, "other"),
            (r"C:\synthetic-private\powershell.exe", r"C:\synthetic-private\powershell.exe", "other"),
            (FakeJobObservationApi.SHELL, r"C:\synthetic-private\powershell.exe", "other"),
        ):
            with self.subTest(requested=requested, image=image):
                api = FakeJobObservationApi((22,))
                api.entries[22]["image"] = image
                members = self.observe(api, command=[requested])["members"]
                self.assertEqual(members[expected], 1)
                self.assertEqual(members["status"], "observed")

    def test_ambiguous_images_and_context_are_unclassified_without_discovery(self):
        for image in (
            "powershell.exe", r"\\?\C:\Windows\System32\cmd.exe", r"\\server\synthetic-private\worker.exe",
            r"C:\Windows\System32\..\cmd.exe", r"C:\WINDOW~1\System32\cmd.exe",
            "C:\\Windows\\System32\\cmd.exe ", "C:\\Windows\\System32\\cmd.exe.",
        ):
            with self.subTest(image=image):
                api = FakeJobObservationApi((22,))
                api.entries[22]["image"] = image
                self.assertEqual(self.observe(api)["members"]["unclassified"], 1)
        for command, environment in (([], {}), (["powershell.exe"], {}), ([FakeJobObservationApi.SHELL], {})):
            with self.subTest(command=command, environment=environment):
                api = FakeJobObservationApi((22,))
                self.assertEqual(self.observe(api, command=command, environment=environment)["members"]["unclassified"], 1)
                api.QueryFullProcessImageNameW.assert_not_called()

    def test_accounting_uint32_boundaries_are_not_membership_or_invented_zeroes(self):
        for total, active, expected in ((0, 0, "observed"), (4294967295, 4294967295, "observed"),
                                        (0, 1, "unavailable")):
            with self.subTest(total=total, active=active):
                api = FakeJobObservationApi(())
                api.total, api.active = total, active
                value = self.observe(api)
                self.assertEqual(value["accounting"]["status"], expected)
                self.assertEqual(value["members"]["status"], "partial" if active == 4294967295 else "observed")
        api = FakeJobObservationApi((11, 22))
        api.total = 4294967295
        self.assertEqual(self.observe(api)["members"]["status"], "observed")

    def test_active_mismatch_is_partial_but_missing_accounting_is_not_zero(self):
        for active, accounting_error, expected in ((1, 0, "partial"), (3, 0, "partial"), (0, 5, "observed")):
            with self.subTest(active=active, accounting_error=accounting_error):
                api = FakeJobObservationApi((11, 22))
                api.total, api.active = 3, active
                api.query_errors[1] = accounting_error
                value = self.observe(api)
                self.assertEqual(value["members"]["status"], expected)
                self.assertEqual(value["members"]["listed"], 2)
                self.assertEqual(value["members"]["unclassified"], 0)
                if accounting_error:
                    self.assertEqual(value["accounting"], {"status": "unavailable"})

    def test_empty_and_sixteen_member_lists_are_complete_without_extra_queries(self):
        for pids in ((), tuple(range(1, 17))):
            with self.subTest(count=len(pids)):
                api = FakeJobObservationApi(pids)
                members = self.observe(api)["members"]
                self.assertEqual(members["status"], "observed")
                self.assertEqual(members["listed"], len(pids))
                self.assertEqual(api.QueryInformationJobObject.call_count, 2)
                self.assertEqual(api.OpenProcess.call_count, len(pids))

    def test_oversized_membership_never_opens_truncated_pids_or_resizes(self):
        for field, number, error in (("assigned", 17, 0), ("listed", 17, 0), ("assigned", 4294967295, 0),
                                      ("assigned", 3, 234)):
            with self.subTest(field=field, number=number, error=error):
                api = FakeJobObservationApi()
                setattr(api, field, number)
                api.query_errors[3] = error
                value = self.observe(api)
                self.assertEqual(value["members"], {"status": "oversized"})
                self.assertEqual(value["accounting"]["status"], "observed")
                self.assertEqual(api.QueryInformationJobObject.call_count, 2)
                api.OpenProcess.assert_not_called()

    def test_malformed_lengths_counts_and_pids_do_not_publish_zeroes(self):
        cases = [{"query_lengths": {3: length}} for length in (0, 7, 8, 10000)]
        cases += [{"assigned": 1}, {"pids": [0, 22, 33]}, {"pids": [11, 11, 33]}]
        if ctypes.sizeof(ctypes.c_size_t) > 4:
            cases.append({"pids": [4294967296, 22, 33]})
        for changes in cases:
            with self.subTest(changes=changes):
                api = FakeJobObservationApi()
                for key, value in changes.items():
                    setattr(api, key, value)
                self.assertEqual(self.observe(api)["members"], {"status": "unavailable"})
                api.OpenProcess.assert_not_called()
        for length in (0, 47, 49):
            with self.subTest(accounting_length=length):
                api = FakeJobObservationApi(())
                api.query_lengths[1] = length
                self.assertEqual(self.observe(api)["accounting"], {"status": "unavailable"})

    def test_valid_incomplete_list_retains_only_genuinely_returned_roles(self):
        api = FakeJobObservationApi((11, 22))
        api.assigned, api.active, api.total = 3, 3, 3
        members = self.observe(api)["members"]
        self.assertEqual(members, {
            "status": "partial", "listed": 2, "bootstrap": 1,
            "system_approved_shell": 1, "other": 0, "unclassified": 0,
        })
        self.assertEqual(api.OpenProcess.call_count, 2)

    def test_unsupported_and_unavailable_components_are_independent(self):
        for kind in (1, 3):
            for code, expected in ((5, "unavailable"), (50, "unsupported"), (120, "unsupported"), ("raise", "unavailable")):
                with self.subTest(kind=kind, code=code):
                    api = FakeJobObservationApi()
                    api.query_errors[kind] = code
                    value = self.observe(api)
                    self.assertEqual(value["accounting" if kind == 1 else "members"], {"status": expected})
                    self.assertEqual(value["members" if kind == 1 else "accounting"]["status"], "observed")
                    self.assertEqual(api.QueryInformationJobObject.call_count, 2)
                    if kind == 3:
                        api.OpenProcess.assert_not_called()
        for missing in ("QueryInformationJobObject", "IsProcessInJob", "CompareObjectHandles",
                        "QueryFullProcessImageNameW", "CloseHandle", "OpenProcess"):
            with self.subTest(missing=missing):
                api = FakeJobObservationApi()
                delattr(api.kernelbase if missing == "CompareObjectHandles" else api.kernel32, missing)
                value = self.observe(api)
                self.assertEqual(value["members"], {"status": "unsupported"})
                self.assertEqual(value["accounting"]["status"],
                                 "unsupported" if missing == "QueryInformationJobObject" else "observed")
                self.assertFalse(any(event[0] == "open" for event in api.events))

    def test_disappeared_denied_and_reused_nonmembers_get_no_image_metadata(self):
        for changes in (
            {"open_error": True}, {"open_error": "raise"},
            {"membership": [False]}, {"membership": ["denied"]}, {"membership": ["raise"]},
        ):
            with self.subTest(changes=changes):
                api = FakeJobObservationApi((22,))
                api.entries[22].update(changes)
                members = self.observe(api)["members"]
                self.assertEqual((members["status"], members["listed"], members["unclassified"]), ("partial", 1, 1))
                api.CompareObjectHandles.assert_not_called()
                api.QueryFullProcessImageNameW.assert_not_called()
                self.assertEqual(api.CloseHandle.call_count, int("open_error" not in changes))

    def test_post_metadata_membership_refusal_discards_positive_roles(self):
        for pid in (11, 22, 33):
            for second in (False, "denied", "raise"):
                with self.subTest(pid=pid, second=second):
                    api = FakeJobObservationApi((pid,))
                    api.entries[pid]["membership"] = [True, second]
                    members = self.observe(api)["members"]
                    self.assertEqual(members["unclassified"], 1)
                    self.assertEqual(members["status"], "partial")
                    self.assertEqual(api.IsProcessInJob.call_count, 2)
                    self.assertEqual(api.CloseHandle.call_count, 1)

    def test_metadata_and_close_refusals_are_partial_without_leaking_or_retrying(self):
        for changes in (
            {"compare_error": True}, {"image_error": True}, {"image_error": "raise"},
            {"image_length": 0}, {"image_length": 32768}, {"image_length": 4294967295},
            {"image_length": len(FakeJobObservationApi.SHELL) - 4},
            {"image": "synthetic-private\0image"}, {"close_error": True}, {"close_error": "raise"},
        ):
            with self.subTest(changes=changes):
                api = FakeJobObservationApi((22,))
                api.entries[22].update(changes)
                members = self.observe(api)["members"]
                self.assertEqual((members["status"], members["unclassified"]), ("partial", 1))
                self.assertEqual(api.OpenProcess.call_count, 1)
                self.assertEqual(api.CloseHandle.call_count, 1)

    def test_reused_image_buffer_cannot_classify_unwritten_stale_payload(self):
        api = FakeJobObservationApi((22, 33))
        api.entries[33].update(no_image_write=True, image=api.SHELL)
        members = self.observe(api)["members"]
        self.assertEqual((members["system_approved_shell"], members["unclassified"]), (1, 1))
        self.assertEqual(members["status"], "partial")

    def test_missing_job_and_encoding_refusals_return_the_exact_unavailable_envelope(self):
        api = FakeJobObservationApi()
        job = self.job(api)
        job.handle = None
        self.assertEqual(self.observe(api, job=job), {
            "schema": "pennilogic.process-budget-job-state/1",
            "accounting": {"status": "unavailable"}, "members": {"status": "unavailable"},
        })
        api.QueryInformationJobObject.assert_not_called()
        for effect in (TypeError("synthetic-private-encoding"), ValueError("synthetic-private-encoding"),
                       "x" * 513, "\u2603", None):
            api = FakeJobObservationApi()
            options = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with self.subTest(effect=type(effect).__name__), \
                    patch.object(process_budget.ctypes, "get_last_error", return_value=0, create=True), \
                    patch.object(process_budget.ctypes, "WinDLL", side_effect=api.load_library, create=True), \
                    patch.object(process_budget.json, "dumps", **options):
                note = self.job(api).observation(
                    SimpleNamespace(_handle=api.BOOTSTRAP), [api.SHELL], {"COMSPEC": api.COMSPEC},
                )
            self.assertEqual(note, process_budget.JOB_STATE_UNAVAILABLE)
            self.decode(note)
            self.assertFalse(api.opened)


class FakeJobCleanupApi:
    JOB, PORT, BOOTSTRAP = 70001, 70002, 70003

    def __init__(self):
        self.clock, self.active, self.last_error = 100, 0, 0
        self.transitions, self.events = [1, 0], []
        self.CreateJobObjectW = Mock(return_value=self.JOB)
        self.SetInformationJobObject = Mock(return_value=1)
        self.CreateIoCompletionPort = Mock(return_value=self.PORT)
        self.AssignProcessToJobObject = Mock(side_effect=self.attach)
        self.TerminateJobObject = Mock(side_effect=lambda job, code: self.events.append(("terminate", job, code)) or 1)
        self.QueryInformationJobObject = Mock(side_effect=self.query)
        self.GetQueuedCompletionStatus = Mock(side_effect=self.wait)
        self.CloseHandle = Mock(side_effect=lambda handle: self.events.append(("close", handle, self.active)) or 1)
        self.OpenProcess, self.CompareObjectHandles = Mock(), Mock()

    def attach(self, job, process):
        self.active = 2
        self.events.append(("attach", job, process))
        return 1

    def query(self, job, kind, value, size, returned):
        if job != self.JOB or kind != 1 or size != 48:
            raise AssertionError("Unexpected owned-job query")
        account = ctypes.cast(value, ctypes.POINTER(process_budget.WindowsJob.Accounting)).contents
        account.total, account.active = 2, self.active
        returned._obj.value = size
        self.events.append(("active", self.active))
        return 1

    def wait(self, port, message, key, overlapped, milliseconds):
        if port != self.PORT or not 0 < milliseconds <= 10000:
            raise AssertionError("Unexpected owned completion wait")
        self.events.append(("wait", milliseconds))
        self.clock += 0.025
        self.active = self.transitions.pop(0)
        message._obj.value, key._obj.value, overlapped._obj.value = 4, self.JOB, None
        return 1


class WindowsJobCleanupTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeJobCleanupApi()
        self.load = self.enterContext(patch.object(process_budget.ctypes, "WinDLL", return_value=self.api, create=True))
        self.enterContext(patch.object(process_budget.ctypes, "get_last_error",
                                       side_effect=lambda: self.api.last_error, create=True))
        self.enterContext(patch.object(process_budget.time, "monotonic", side_effect=lambda: self.api.clock))
        self.enterContext(patch.object(process_budget.subprocess, "Popen", side_effect=AssertionError("No native control")))

    def attached_job(self):
        job = process_budget.WindowsJob()
        job.attach(SimpleNamespace(pid=11, _handle=self.api.BOOTSTRAP))
        return job

    def assert_released(self, job):
        self.assertIsNone(job.handle)
        self.assertIsNone(job.completion_port)
        self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list],
                         [self.api.JOB, self.api.PORT])
        self.api.OpenProcess.assert_not_called()

    def complete_after(self, seconds, *, timeout=False):
        def wait(port, message, key, overlapped, milliseconds):
            self.assertEqual(port, self.api.PORT)
            self.assertGreater(milliseconds, 0)
            self.assertLessEqual(milliseconds, 10000)
            self.api.clock += seconds
            self.api.active = 0
            if timeout:
                self.api.last_error = 258
                return 0
            message._obj.value, key._obj.value, overlapped._obj.value = 4, self.api.JOB, None
            return 1

        self.api.GetQueuedCompletionStatus.side_effect = wait

    def run_with_moving_cleanup_clock(self, *, completion_seconds=1, timeout=False, joins=(),
                                      reap_seconds=0, final_stream_seconds=0, reap_error=None,
                                      bootstrap_running=False):
        self.api = FakeJobCleanupApi()
        self.load.return_value = self.api
        self.complete_after(completion_seconds, timeout=timeout)
        jobs, readers = [], []
        original_init = process_budget.WindowsJob.__init__

        def initialize(job):
            original_init(job)
            jobs.append(job)

        process = Mock(pid=11, _handle=self.api.BOOTSTRAP, returncode=None if bootstrap_running else 0, stdin=Mock(),
                       stdout=Mock() if joins else None, stderr=Mock() if joins else None)
        process.poll.side_effect = lambda: process.returncode

        def reap(*, timeout):
            self.assertGreaterEqual(timeout, 0)
            self.api.clock += reap_seconds
            if reap_error is not None:
                raise reap_error
            return 0

        def close_stdin():
            if jobs[0].cleanup_deadline is not None:
                self.api.clock += final_stream_seconds

        process.wait.side_effect = reap
        process.stdin.close.side_effect = close_stdin
        for seconds in joins:
            reader = Mock()
            reader.is_alive.return_value = True

            def join(*, timeout, duration=seconds, target=reader):
                self.assertGreaterEqual(timeout, 0)
                self.api.clock += duration
                target.is_alive.return_value = False

            reader.join.side_effect = join
            readers.append(reader)
        captured = Mock(threads=readers, exceeded=bool(joins), failed=False)
        captured.snapshot.return_value = (b"", b"", {
            name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()} for name in ("stdout", "stderr")
        })
        result, error = None, None
        with (
            patch.object(process_budget, "os", SimpleNamespace(name="nt", pathsep=";")),
            patch.object(process_budget, "environment", return_value={}),
            patch.object(process_budget, "resolve_command", return_value=["synthetic-command"]),
            patch.object(process_budget.WindowsJob, "__init__", initialize),
            patch.object(process_budget.subprocess, "Popen", return_value=process),
            patch.object(process_budget, "Capture", return_value=captured),
        ):
            try:
                result = process_budget.run(["synthetic-command"], SCRIPTS, 1,
                                            capture=bool(joins), separate=bool(joins))
            except (TimeoutError, OSError) as observed:
                error = observed
        job, = jobs
        self.assert_released(job)
        self.assertEqual(job.cleanup_deadline, 110)
        self.assertEqual(process.stdin.close.call_count, 2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close.assert_called_once_with()
        self.assertEqual(process.kill.call_count, 2 if bootstrap_running else 0)
        return SimpleNamespace(job=job, process=process, readers=readers, result=result, error=error)

    def test_close_waits_for_all_members_before_releasing_owned_handles(self):
        job = self.attached_job()
        job.close()
        self.assertEqual(self.api.events, [
            ("attach", self.api.JOB, self.api.BOOTSTRAP), ("terminate", self.api.JOB, 1),
            ("active", 2), ("wait", 10000), ("active", 1), ("wait", int((110 - 100.025) * 1000)),
            ("active", 0), ("close", self.api.JOB, 0), ("close", self.api.PORT, 0),
        ])
        self.assertIsNone(job.handle)
        self.assertIsNone(job.completion_port)
        self.api.OpenProcess.assert_not_called()
        self.assertEqual(self.api.SetInformationJobObject.call_count, 2)
        associate = self.api.SetInformationJobObject.call_args_list[1]
        self.assertEqual(associate.args[:2], (self.api.JOB, 7))
        self.assertEqual((associate.args[2]._obj.key, associate.args[2]._obj.port),
                         (self.api.JOB, self.api.PORT))

    def test_close_is_idempotent_and_does_not_restart_cleanup_allowance(self):
        job = self.attached_job()
        job.close()
        before = list(self.api.events)
        self.api.clock += 3
        job.close()
        self.assertEqual(self.api.events, before)
        self.assertEqual(job.cleanup_deadline, 110)
        self.assertLess(job.remaining_cleanup(), 7)

    def test_already_empty_job_needs_no_completion_wait(self):
        job = self.attached_job()
        self.api.active = 0
        job.close()
        self.api.GetQueuedCompletionStatus.assert_not_called()
        self.api.TerminateJobObject.assert_called_once_with(self.api.JOB, 1)

    def test_construction_failures_release_only_created_handles(self):
        for step, expected, handles in (
            ("job", "Cannot create the owned process budget job", []),
            ("limits", "Cannot enforce the owned process budget job", [self.api.JOB]),
            ("port", "Cannot create the owned process completion port", [self.api.JOB]),
            ("association", "Cannot associate the owned process completion port", [self.api.JOB, self.api.PORT]),
        ):
            with self.subTest(step=step):
                api = FakeJobCleanupApi()
                if step == "job":
                    api.CreateJobObjectW.return_value = 0
                elif step == "limits":
                    api.SetInformationJobObject.return_value = 0
                elif step == "port":
                    api.CreateIoCompletionPort.return_value = 0
                else:
                    api.SetInformationJobObject.side_effect = [1, 0]
                self.load.return_value = api
                with self.assertRaisesRegex(OSError, expected):
                    process_budget.WindowsJob()
                self.assertEqual([call.args[0] for call in api.CloseHandle.call_args_list], handles)
                api.TerminateJobObject.assert_not_called()
                api.GetQueuedCompletionStatus.assert_not_called()

    def test_failed_attachment_closes_job_and_port_without_claiming_worker_ownership(self):
        self.api.AssignProcessToJobObject.side_effect = None
        self.api.AssignProcessToJobObject.return_value = 0
        job = process_budget.WindowsJob()
        with self.assertRaisesRegex(OSError, "Cannot attach the launched process"):
            job.attach(SimpleNamespace(pid=11, _handle=self.api.BOOTSTRAP))
        job.close()
        self.api.TerminateJobObject.assert_not_called()
        self.api.QueryInformationJobObject.assert_not_called()
        self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list],
                         [self.api.JOB, self.api.PORT])

    def test_unavailable_or_invalid_accounting_is_a_cleanup_failure(self):
        for case in ("query", "size", "count"):
            with self.subTest(case=case):
                self.api = FakeJobCleanupApi()
                self.load.return_value = self.api
                job = self.attached_job()

                def query(*args):
                    self.api.query(*args)
                    if case == "size":
                        args[-1]._obj.value = 1
                    elif case == "count":
                        args[2]._obj.active = 3
                    return 0 if case == "query" else 1

                self.api.QueryInformationJobObject.side_effect = query
                with self.assertRaisesRegex(OSError, "Cannot establish owned process job completion"):
                    job.close()
                self.assertIsNone(job.handle)
                self.assertIsNone(job.completion_port)
                self.api.GetQueuedCompletionStatus.assert_not_called()

    def test_completion_timeout_and_api_failure_refuse_instead_of_reporting_cleanup(self):
        for code, expected_type, expected in (
            (258, TimeoutError, "cleanup exceeded its existing allowance"),
            (5, OSError, "Cannot wait for owned process job completion"),
        ):
            with self.subTest(code=code):
                self.api = FakeJobCleanupApi()
                self.load.return_value = self.api
                job = self.attached_job()
                self.api.last_error = code
                self.api.GetQueuedCompletionStatus.side_effect = None
                self.api.GetQueuedCompletionStatus.return_value = 0
                with self.assertRaisesRegex(expected_type, expected):
                    job.close()
                self.assertEqual(self.api.GetQueuedCompletionStatus.call_count, 1)
                self.assertIsNone(job.handle)
                self.assertIsNone(job.completion_port)

    def test_expired_allowance_does_not_start_another_wait(self):
        job = self.attached_job()
        job.cleanup_deadline = self.api.clock
        with self.assertRaisesRegex(TimeoutError, "cleanup exceeded its existing allowance"):
            job.close()
        self.api.GetQueuedCompletionStatus.assert_not_called()
        self.assertIsNone(job.handle)
        self.assertIsNone(job.completion_port)

    def test_missing_completion_notification_still_requires_observed_empty_accounting(self):
        job = self.attached_job()

        def wait(*_args):
            self.api.clock += 10
            self.api.active, self.api.last_error = 0, 258
            return 0

        self.api.GetQueuedCompletionStatus.side_effect = wait
        job.close()
        self.assertEqual(job.remaining_cleanup(), 0)
        self.assertEqual([event for event in self.api.events if event[0] == "active"],
                         [("active", 2), ("active", 0)])
        self.assertIsNone(job.handle)
        self.assertIsNone(job.completion_port)

    def test_empty_accounting_after_either_completion_path_respects_deadline(self):
        for timeout in (False, True):
            for seconds in (9.999, 10, 10.001):
                with self.subTest(timeout=timeout, seconds=seconds):
                    self.api = FakeJobCleanupApi()
                    self.load.return_value = self.api
                    job = self.attached_job()
                    self.complete_after(seconds, timeout=timeout)
                    if seconds > 10:
                        with self.assertRaisesRegex(TimeoutError, "cleanup exceeded its existing allowance"):
                            job.close()
                    else:
                        job.close()
                        self.assertGreaterEqual(job.remaining_cleanup(), 0)
                    self.assert_released(job)
                    self.assertEqual(job.cleanup_deadline, 110)
                    self.assertEqual(self.api.GetQueuedCompletionStatus.call_count, 1)
                    self.assertEqual([event for event in self.api.events if event[0] == "active"],
                                     [("active", 2), ("active", 0)])

    def test_initial_empty_accounting_still_requires_timely_observation(self):
        for seconds in (10, 10.001):
            with self.subTest(seconds=seconds):
                self.api = FakeJobCleanupApi()
                self.load.return_value = self.api
                job = self.attached_job()
                self.api.active = 0

                def query(*args):
                    self.api.clock += seconds
                    return self.api.query(*args)

                self.api.QueryInformationJobObject.side_effect = query
                if seconds > 10:
                    with self.assertRaisesRegex(TimeoutError, "cleanup exceeded its existing allowance"):
                        job.close()
                else:
                    job.close()
                self.assert_released(job)
                self.api.GetQueuedCompletionStatus.assert_not_called()

    def test_handle_release_cannot_certify_late_cleanup(self):
        for delayed in (self.api.JOB, self.api.PORT):
            for seconds in (10, 10.001):
                with self.subTest(handle=delayed, seconds=seconds):
                    self.api = FakeJobCleanupApi()
                    self.load.return_value = self.api
                    job = self.attached_job()
                    self.api.active = 0

                    def close(handle):
                        if handle == delayed:
                            self.api.clock += seconds
                        return 1

                    self.api.CloseHandle.side_effect = close
                    if seconds > 10:
                        with self.assertRaisesRegex(TimeoutError, "cleanup exceeded its existing allowance"):
                            job.close()
                    else:
                        job.close()
                    self.assert_released(job)

    def test_repeated_close_does_not_recertify_an_expired_allowance(self):
        job = self.attached_job()
        job.close()
        self.api.clock = 110.001
        with self.assertRaisesRegex(TimeoutError, "cleanup exceeded its existing allowance"):
            job.close()
        self.assert_released(job)
        self.assertEqual(job.cleanup_deadline, 110)

    def test_run_rejects_late_empty_completion_despite_successful_bootstrap(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                observed = self.run_with_moving_cleanup_clock(completion_seconds=10.001, timeout=timeout)
                self.assertIs(type(observed.error), TimeoutError)
                self.assertIn("cleanup exceeded its existing allowance", str(observed.error))
                self.assertIsNone(observed.result)
                self.assertEqual(observed.process.returncode, 0)
                observed.process.wait.assert_called_once_with(timeout=0)

    def test_real_close_two_readers_and_reap_share_one_moving_allowance(self):
        observed = self.run_with_moving_cleanup_clock(joins=(4, 3), reap_seconds=2)
        self.assertIs(type(observed.error), process_budget.OutputExceeded)
        self.assertIsNone(observed.result)
        observed.readers[0].join.assert_called_once_with(timeout=9)
        observed.readers[1].join.assert_called_once_with(timeout=5)
        observed.process.wait.assert_called_once_with(timeout=2)
        self.assertEqual(self.api.clock, observed.job.cleanup_deadline)

    def test_moving_cleanup_clock_refuses_each_late_finalization_stage(self):
        for stage, joins, reap, stream in (
            ("first-reader", (9.001, 0), 0, 0),
            ("second-reader", (4, 5.001), 0, 0),
            ("reap-after-readers", (4, 3), 2.001, 0),
            ("stream-after-readers", (4, 3), 0, 2.001),
            ("successful-bootstrap-reap", (), 9.001, 0),
            ("successful-bootstrap-stream", (), 0, 9.001),
        ):
            with self.subTest(stage=stage):
                observed = self.run_with_moving_cleanup_clock(
                    joins=joins, reap_seconds=reap, final_stream_seconds=stream,
                )
                self.assertIs(type(observed.error), TimeoutError)
                self.assertIn("cleanup exceeded its existing allowance", str(observed.error))
                self.assertIsNone(observed.result)
                self.assertGreater(self.api.clock, observed.job.cleanup_deadline)
                observed.process.wait.assert_called_once()
                for reader in observed.readers:
                    reader.join.assert_called_once()

    def test_in_budget_and_exact_deadline_completion_remain_valid(self):
        for reap in (8.999, 9):
            with self.subTest(reap=reap):
                observed = self.run_with_moving_cleanup_clock(reap_seconds=reap)
                self.assertIsNone(observed.error)
                self.assertEqual(observed.result.returncode, 0)
                observed.process.wait.assert_called_once_with(timeout=9)
                self.assertLessEqual(self.api.clock, observed.job.cleanup_deadline)

    def test_bootstrap_reap_timeout_preserves_the_cleanup_timeout_contract(self):
        expired = subprocess.TimeoutExpired(["synthetic-command"], 9)
        observed = self.run_with_moving_cleanup_clock(
            joins=(0, 0), reap_seconds=9, reap_error=expired, bootstrap_running=True,
        )
        self.assertIs(type(observed.error), TimeoutError)
        self.assertIn("cleanup exceeded its existing allowance", str(observed.error))
        self.assertIs(observed.error.__cause__, expired)
        self.assertFalse(hasattr(observed.error, "output"))
        self.assertIsNone(observed.result)
        observed.process.wait.assert_called_once_with(timeout=9)
        self.assertEqual(self.api.clock, observed.job.cleanup_deadline)

    def test_foreign_completion_key_cannot_certify_owned_cleanup(self):
        job = self.attached_job()

        def wait(*args):
            self.api.wait(*args)
            args[2]._obj.value = self.api.JOB + 1
            return 1

        self.api.GetQueuedCompletionStatus.side_effect = wait
        with self.assertRaisesRegex(OSError, "Unexpected owned process job completion key"):
            job.close()
        self.assertIsNone(job.handle)
        self.assertIsNone(job.completion_port)

    def test_termination_and_handle_release_failures_are_not_silenced(self):
        for operation, message in (
            ("terminate", "Cannot terminate the owned process budget job"),
            ("job", "Cannot close the owned process budget job"),
            ("port", "Cannot close the owned process completion port"),
        ):
            with self.subTest(operation=operation):
                self.api = FakeJobCleanupApi()
                self.load.return_value = self.api
                job = self.attached_job()
                if operation == "terminate":
                    self.api.TerminateJobObject.side_effect = None
                    self.api.TerminateJobObject.return_value = 0
                else:
                    failing = self.api.JOB if operation == "job" else self.api.PORT
                    self.api.CloseHandle.side_effect = lambda handle: int(handle != failing)
                with self.assertRaisesRegex(OSError, message):
                    job.close()
                self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list],
                                 [self.api.JOB, self.api.PORT])

    def test_parent_exit_does_not_allow_return_with_an_active_descendant(self):
        process = Mock(pid=11, _handle=self.api.BOOTSTRAP, returncode=0, stdin=Mock(), stdout=None, stderr=None)
        process.poll.return_value = 0
        with (
            patch.object(process_budget, "os", SimpleNamespace(name="nt", pathsep=";")),
            patch.object(process_budget, "environment", return_value={}),
            patch.object(process_budget, "resolve_command", return_value=["synthetic-command"]),
            patch.object(process_budget.subprocess, "Popen", return_value=process),
        ):
            completed = process_budget.run(["synthetic-command"], SCRIPTS, 1)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.api.active, 0)
        self.assertIn(("active", 1), self.api.events)
        process.kill.assert_not_called()
        self.assertLess(process.wait.call_args.kwargs["timeout"], 10)
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 9.9)
        self.assertEqual(process.stdin.close.call_count, 2)

    def test_failed_assignment_reaps_exact_bootstrap_without_opening_the_stdin_gate(self):
        self.api.AssignProcessToJobObject.side_effect = None
        self.api.AssignProcessToJobObject.return_value = 0
        process = Mock(pid=11, _handle=self.api.BOOTSTRAP, stdin=Mock(), stdout=Mock(), stderr=Mock())
        process.poll.return_value = None
        with (
            patch.object(process_budget, "os", SimpleNamespace(name="nt", pathsep=";")),
            patch.object(process_budget, "environment", return_value={}),
            patch.object(process_budget, "resolve_command", return_value=["synthetic-command"]),
            patch.object(process_budget.subprocess, "Popen", return_value=process),
            patch.object(process_budget, "Capture", side_effect=AssertionError("Gate must remain closed")),
            self.assertRaisesRegex(OSError, "Cannot attach the launched process"),
        ):
            process_budget.run(["synthetic-command"], SCRIPTS, 1, capture=True, separate=True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)
        process.stdin.write.assert_not_called()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once_with()

    def test_failed_process_creation_releases_job_and_completion_port(self):
        error = OSError("synthetic-process-creation-failure")
        with (
            patch.object(process_budget, "os", SimpleNamespace(name="nt", pathsep=";")),
            patch.object(process_budget, "environment", return_value={}),
            patch.object(process_budget, "resolve_command", return_value=["synthetic-command"]),
            patch.object(process_budget.subprocess, "Popen", side_effect=error),
            self.assertRaises(OSError) as observed,
        ):
            process_budget.run(["synthetic-command"], SCRIPTS, 1)
        self.assertIs(observed.exception, error)
        self.api.AssignProcessToJobObject.assert_not_called()
        self.api.TerminateJobObject.assert_not_called()
        self.assertEqual([call.args[0] for call in self.api.CloseHandle.call_args_list],
                         [self.api.JOB, self.api.PORT])


class ProcessBudgetJobRunTest(unittest.TestCase):
    def run_mocked(self, *, platform="nt", failure="elapsed", exit_code=0, inherit_tree=False, query_error=0,
                   missing_provider=None, capture=True, separate=True, cleanup_seconds=10):
        events = []
        api = FakeJobObservationApi((11, 22))
        api.query_errors[3] = query_error
        if missing_provider == "library":
            api.load_library.side_effect = OSError("synthetic-private-missing-provider")
        elif missing_provider == "export":
            del api.kernelbase.CompareObjectHandles
        job = process_budget.WindowsJob.__new__(process_budget.WindowsJob)
        job.api, job.handle = api.kernel32, api.JOB
        process = Mock(pid=11, _handle=api.BOOTSTRAP)
        process.returncode = exit_code if failure is None else None
        process.poll.side_effect = lambda: process.returncode
        process.stdin = Mock() if platform == "nt" else None
        process.stdout = Mock() if capture else None
        process.stderr = Mock() if capture and separate else None
        process.kill.side_effect = lambda: setattr(process, "returncode", -9)
        job.attach = Mock(side_effect=lambda value: events.append(("attach", value)))

        def terminate():
            events.append(("terminate", None))
            job.handle = None
            process.returncode = -9 if process.returncode is None else process.returncode

        job.close = Mock(side_effect=terminate)
        job.remaining_cleanup = Mock(return_value=cleanup_seconds)
        job.check_cleanup_deadline = Mock()
        original = job.observation

        def observe(*args):
            events.append(("job_observation", None))
            self.assertEqual(job.handle, api.JOB)
            return original(*args)

        job.observation = Mock(side_effect=observe)
        reader = Mock()
        reader.is_alive.return_value = failure is not None
        reader.join.side_effect = lambda **_options: setattr(reader.is_alive, "return_value", False)
        captured = Mock(threads=[reader], exceeded=failure == "output", failed=failure == "capture")
        output, errors = (b"out", b"err") if separate else (b"out\nerr", b"")
        streams = {name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                   for name, value in (("stdout", output), ("stderr", errors))}

        def snapshot(mode):
            events.append(("snapshot", None))
            return (output if mode else output.decode("utf-8")), errors, streams

        captured.snapshot.side_effect = snapshot
        fake_os = SimpleNamespace(name=platform, pathsep=";" if platform == "nt" else ":",
                                  killpg=Mock(side_effect=lambda *_: terminate()))
        environment = {"COMSPEC": api.COMSPEC, "PATH": "synthetic-private-approved-path"}
        resolved = [api.SHELL, "-NoProfile", "-NonInteractive", "-Command", "synthetic-private-command"]
        times = [0, 0.025, 10.025, 10.025] if failure == "elapsed" else [0, 0.025, 0.5]
        raised, result = None, None
        original_observation = process_budget.budget_observation
        original_projection = process_budget.capture_observation
        original_error = process_budget.BudgetExceeded
        created_errors = []

        def v1(*args):
            events.append(("v1_observation", None))
            return original_observation(*args)

        def construct_error(*args):
            error = original_error(*args)
            created_errors.append(error)
            return error

        def project(metadata, **options):
            events.append(("capture_observation", None))
            self.assertIs(metadata, created_errors[-1].streams)
            return original_projection(metadata, **options)

        with (
            patch.object(process_budget, "os", fake_os),
            patch.object(process_budget, "signal", SimpleNamespace(SIGKILL=9)),
            patch.object(process_budget, "environment", return_value=environment) as environment_call,
            patch.object(process_budget, "resolve_command", return_value=resolved) as resolve_call,
            patch.object(process_budget, "WindowsJob", return_value=job) as constructor,
            patch.object(process_budget.subprocess, "Popen", return_value=process) as popen,
            patch.object(process_budget, "Capture", return_value=captured) as capture_call,
            patch.object(process_budget, "BudgetExceeded", side_effect=construct_error) as error_call,
            patch.object(process_budget, "budget_observation", side_effect=v1) as state_call,
            patch.object(process_budget, "capture_observation", side_effect=project) as capture_state_call,
            patch.object(process_budget.time, "monotonic", side_effect=times) as clock,
            patch.object(process_budget.ctypes, "get_last_error", side_effect=lambda: api.last_error, create=True),
            patch.object(process_budget.ctypes, "WinDLL", side_effect=api.load_library, create=True),
        ):
            try:
                result = process_budget.run(["powershell.exe"], SCRIPTS, 10, capture=capture,
                                            separate=separate, inherit_tree=inherit_tree)
            except (original_error, OSError) as error:
                raised = error
        environment_call.assert_called_once()
        self.assertEqual(list(environment_call.call_args.kwargs["runtime_dirs"]), [])
        resolve_call.assert_called_once_with(["powershell.exe"], environment)
        self.assertEqual(clock.call_count, len(times))
        if platform == "nt":
            constructor.assert_called_once_with()
        else:
            constructor.assert_not_called()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0], [
            sys.executable, "-I", "-S", "-B", "-c", process_budget.WINDOWS_BOOTSTRAP, *resolved,
        ] if platform == "nt" else resolved)
        self.assertEqual(popen.call_args.kwargs, {
            "cwd": SCRIPTS, "env": environment, "bufsize": 0,
            "stdin": subprocess.PIPE if platform == "nt" else None,
            "stdout": subprocess.PIPE if capture else None,
            "stderr": (subprocess.PIPE if separate else subprocess.STDOUT) if capture else None,
            "start_new_session": platform != "nt" and not inherit_tree,
        })
        if platform == "nt":
            job.attach.assert_called_once_with(process)
            process.stdin.write.assert_called_once_with(b"1")
            self.assertEqual(process.stdin.close.call_count, 2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=cleanup_seconds if platform == "nt" else 10)
        if platform == "nt":
            job.check_cleanup_deadline.assert_called_once_with()
        else:
            job.check_cleanup_deadline.assert_not_called()
        if capture:
            capture_call.assert_called_once_with(process)
            captured.stop.set.assert_called()
            captured.snapshot.assert_called_once_with(separate)
        else:
            capture_call.assert_not_called()
            captured.stop.set.assert_not_called()
            captured.snapshot.assert_not_called()
        if failure == "elapsed":
            error_call.assert_called_once()
            self.assertIs(raised, created_errors[0])
            capture_state_call.assert_called_once_with(raised.streams, separate=separate)
        else:
            error_call.assert_not_called()
            capture_state_call.assert_not_called()
        self.assertFalse(api.opened)
        if platform == "nt" and failure == "elapsed" and not query_error:
            api.load_library.assert_called_once_with("KernelBase.dll", use_last_error=True, winmode=0x800)
        else:
            api.load_library.assert_not_called()
        return SimpleNamespace(
            exception=raised, result=result, events=events, job=job, api=api,
            state_call=state_call, capture_state_call=capture_state_call, reader=reader,
            fake_os=fake_os, process=process, streams=streams if capture else None,
        )

    def test_missing_comparison_provider_preserves_timeout_notes_and_cleanup(self):
        for missing, expected in (("library", "unavailable"), ("export", "unsupported")):
            with self.subTest(missing=missing):
                observed = self.run_mocked(missing_provider=missing)
                self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
                self.assertEqual(observed.exception.args, ("Owned process tree exceeded its enforced elapsed budget",))
                self.assertEqual((observed.exception.output, observed.exception.stderr, observed.exception.streams),
                                 (b"out", b"err", observed.streams))
                notes = observed.exception.__notes__
                self.assertEqual(len(notes), 3)
                ProcessBudgetCaptureObservationTest().state(notes[0])
                self.assertEqual(json.loads(notes[-2][len(process_budget.JOB_STATE_PREFIX):])["members"],
                                 {"status": expected})
                self.assertEqual(notes[-1], (
                    'process_budget_state={"capture_readers":1,"capture_readers_alive":1,"elapsed_ms":10025,'
                    '"process_running":true,"returncode":null,"schema":"pennilogic.process-budget-state/1",'
                    '"setup_ms":25,"status":"observed","wait_ms":10000}'
                ))
                observed.job.observation.assert_called_once()
                self.assertEqual([name for name, _ in observed.events],
                                 ["attach", "v1_observation", "job_observation", "terminate",
                                  "snapshot", "capture_observation", "terminate"])
                observed.api.OpenProcess.assert_not_called()
                observed.api.CloseHandle.assert_not_called()
                observed.reader.join.assert_called_once_with(timeout=10)

    def test_elapsed_failure_observes_once_before_termination_and_keeps_v1_bytes(self):
        observed = self.run_mocked()
        self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
        self.assertEqual((observed.exception.output, observed.exception.stderr, observed.exception.streams),
                         (b"out", b"err", observed.streams))
        observed.job.observation.assert_called_once()
        observed.state_call.assert_called_once()
        events = [name for name, _ in observed.events]
        self.assertEqual(events, ["attach", "v1_observation", "job_observation", "terminate",
                                  "snapshot", "capture_observation", "terminate"])
        notes = observed.exception.__notes__
        self.assertEqual(len(notes), 3)
        ProcessBudgetCaptureObservationTest().state(notes[0])
        self.assertTrue(notes[-2].startswith("process_budget_job_state="))
        self.assertEqual(notes[-1], (
            'process_budget_state={"capture_readers":1,"capture_readers_alive":1,"elapsed_ms":10025,'
            '"process_running":true,"returncode":null,"schema":"pennilogic.process-budget-state/1",'
            '"setup_ms":25,"status":"observed","wait_ms":10000}'
        ))
        observed.reader.join.assert_called_once_with(timeout=10)

    def test_elapsed_failure_has_retained_capture_metadata_before_job_and_v1(self):
        observed = self.run_mocked()
        notes = observed.exception.__notes__
        self.assertTrue(notes[0].startswith("process_budget_capture_state="))
        self.assertEqual(json.loads(notes[0].split("=", 1)[1]), {
            "schema": "pennilogic.process-budget-capture-state/1", "status": "observed",
            "scope": "snapshot", "complete": False, "mode": "separate", **observed.streams,
        })
        self.assertTrue(notes[-2].startswith("process_budget_job_state="))
        self.assertTrue(notes[-1].startswith("process_budget_state="))

    def test_merged_capture_keeps_original_error_and_unused_stderr_metadata(self):
        observed = self.run_mocked(separate=False)
        self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
        self.assertEqual((observed.exception.output, observed.exception.stderr), ("out\nerr", b""))
        self.assertIs(observed.exception.streams, observed.streams)
        state = ProcessBudgetCaptureObservationTest().state(observed.exception.__notes__[0])
        self.assertEqual(state["mode"], "merged")
        self.assertEqual(state["stdout"], observed.streams["stdout"])
        self.assertEqual(state["stderr"], observed.streams["stderr"])

    def test_no_capture_is_unavailable_without_invented_bytes_or_hashes(self):
        for platform in ("nt", "posix"):
            for separate in (False, True):
                with self.subTest(platform=platform, separate=separate):
                    observed = self.run_mocked(platform=platform, capture=False, separate=separate)
                    self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
                    self.assertEqual((observed.exception.output, observed.exception.stderr,
                                      observed.exception.streams), (None, None, None))
                    self.assertEqual(observed.exception.__notes__[0], process_budget.CAPTURE_STATE_UNAVAILABLE)
                    self.assertTrue(observed.exception.__notes__[-1].startswith("process_budget_state="))
                    observed.reader.join.assert_not_called()

    def test_capture_projection_refusals_preserve_original_error_notes_and_cleanup(self):
        dumps = json.dumps
        for effect in (TypeError("synthetic-private-projection"), ValueError("synthetic-private-projection"),
                       RuntimeError("synthetic-private-projection"), "x" * 513, "\u2603", None):
            def encode(value, **options):
                if value.get("schema") == "pennilogic.process-budget-capture-state/1":
                    if isinstance(effect, Exception):
                        raise effect
                    return effect
                return dumps(value, **options)

            with self.subTest(effect=type(effect).__name__), patch.object(process_budget.json, "dumps", side_effect=encode):
                observed = self.run_mocked()
            self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
            self.assertEqual(observed.exception.args, ("Owned process tree exceeded its enforced elapsed budget",))
            self.assertEqual((observed.exception.output, observed.exception.stderr), (b"out", b"err"))
            self.assertIs(observed.exception.streams, observed.streams)
            self.assertEqual(observed.exception.__notes__[0], process_budget.CAPTURE_STATE_UNAVAILABLE)
            ProcessBudgetJobObservationTest().decode(observed.exception.__notes__[-2])
            ProcessBudgetObservationTest().state(observed.exception.__notes__[-1])
            self.assertEqual(observed.job.close.call_count, 2)
            self.assertEqual(observed.api.QueryInformationJobObject.call_count, 2)
            observed.reader.join.assert_called_once_with(timeout=10)

    def test_unavailable_job_query_preserves_original_timeout_and_existing_cleanup(self):
        observed = self.run_mocked(query_error=5)
        self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
        self.assertEqual(observed.exception.args, ("Owned process tree exceeded its enforced elapsed budget",))
        note = observed.exception.__notes__[-2]
        self.assertEqual(json.loads(note[len(process_budget.JOB_STATE_PREFIX):])["members"], {"status": "unavailable"})
        self.assertEqual(observed.job.close.call_count, 2)
        self.assertEqual(observed.api.QueryInformationJobObject.call_count, 2)
        observed.api.OpenProcess.assert_not_called()

    def test_non_windows_elapsed_adds_capture_before_v1_for_both_tree_modes(self):
        for inherit in (False, True):
            with self.subTest(inherit_tree=inherit):
                observed = self.run_mocked(platform="posix", inherit_tree=inherit)
                self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
                self.assertEqual(len(observed.exception.__notes__), 2)
                ProcessBudgetCaptureObservationTest().state(observed.exception.__notes__[0])
                self.assertTrue(observed.exception.__notes__[-1].startswith("process_budget_state="))
                observed.job.observation.assert_not_called()
                observed.api.QueryInformationJobObject.assert_not_called()
                observed.reader.join.assert_called_once_with(timeout=0.1 if inherit else 10)
                if inherit:
                    observed.fake_os.killpg.assert_not_called()
                    observed.process.kill.assert_called_once_with()
                else:
                    self.assertEqual(observed.fake_os.killpg.call_count, 2)

    def test_success_and_nonzero_exit_do_not_query_or_attach_either_note(self):
        for platform in ("nt", "posix"):
            for code in (0, 3):
                with self.subTest(platform=platform, code=code):
                    observed = self.run_mocked(platform=platform, failure=None, exit_code=code)
                    self.assertIsNone(observed.exception)
                    self.assertEqual(observed.result.returncode, code)
                    self.assertEqual((observed.result.stdout, observed.result.stderr), (b"out", b"err"))
                    observed.state_call.assert_not_called()
                    observed.job.observation.assert_not_called()
                    observed.api.QueryInformationJobObject.assert_not_called()

    def test_output_and_capture_refusals_keep_their_exception_without_observing(self):
        for failure in ("output", "capture"):
            with self.subTest(failure=failure):
                observed = self.run_mocked(failure=failure)
                self.assertIs(type(observed.exception), process_budget.OutputExceeded if failure == "output" else OSError)
                self.assertEqual(getattr(observed.exception, "__notes__", []), [])
                observed.state_call.assert_not_called()
                observed.job.observation.assert_not_called()
                observed.api.QueryInformationJobObject.assert_not_called()

    def test_windows_readers_and_bootstrap_share_remaining_cleanup_allowance(self):
        observed = self.run_mocked(cleanup_seconds=0.125)
        self.assertIs(type(observed.exception), process_budget.BudgetExceeded)
        observed.reader.join.assert_called_once_with(timeout=0.125)
        observed.process.wait.assert_called_once_with(timeout=0.125)
        self.assertEqual(observed.job.remaining_cleanup.call_count, 2)


class WindowsShellObserverPairProtocolTest(unittest.TestCase):
    """Synthetic protocol controls; no shell, provider or enclosure executes."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root, self.sequence, self.now = Path(temporary.name), 0, 0
        self.arm_runner = self.enterContext(patch.object(
            process_budget, "run", side_effect=AssertionError("Synthetic arm outcome not supplied"),
        ))
        self.popen = self.enterContext(patch.object(
            subprocess, "Popen", side_effect=AssertionError("Native execution is forbidden in protocol controls"),
        ))
        self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.now))
        self.enclosure_calls = []

        @contextmanager
        def enclosure(**kwargs):
            self.enclosure_calls.append(kwargs)
            yield SimpleNamespace(started=self.now)

        self.enclosure = enclosure

    def tearDown(self):
        self.popen.assert_not_called()

    def inputs(self):
        self.sequence += 1
        directory = self.root / str(self.sequence) / "build" / "diagnostics"
        directory.mkdir(parents=True)
        boundary = directory / "api106-approved-shell-boundary.bin"
        lookup = directory / "api106-approved-shell-module-lookup.jsonl"
        for path in (boundary, lookup):
            with path.open("xb"):
                pass
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output 'approved-shell'"]
        assignment, = [item for item in ast.walk(ApprovedShellWriterReturnTest().fixture())
                       if isinstance(item, ast.Assign) and len(item.targets) == 1
                       and ast.unparse(item.targets[0]) == "command[-1]"]
        command[-1] = eval(compile(ast.Expression(assignment.value), "<synthetic-command-shape>", "eval"), {
            "__builtins__": {}, "str": str, "command": command, "boundary": boundary, "lookup": lookup,
            "APPROVED_SHELL_LOOKUP_SETUP": APPROVED_SHELL_LOOKUP_SETUP,
            "APPROVED_SHELL_LOOKUP_FINISH": APPROVED_SHELL_LOOKUP_FINISH,
        })
        return command, boundary, lookup

    def result(self, code=0, stdout="approved-shell\n"):
        return subprocess.CompletedProcess(["synthetic-only"], code, stdout)

    def invoke(self, inputs):
        return run_approved_shell_observer_pair(self, *inputs, enclosure=self.enclosure)

    def records(self, inputs):
        def reject_constant(_):
            self.fail("Observer comparison report contains a non-JSON numeric constant")

        path = inputs[1].with_name("api106-approved-shell-observer-pair.jsonl")
        data = path.read_bytes()
        self.assertLessEqual(len(data), 4096)
        data.decode("ascii")
        records = [json.loads(line, parse_constant=reject_constant) for line in data.splitlines()]
        self.assertLessEqual(len(records), 6)
        return records

    def malformed_returncodes(self):
        return (
            ("string", "synthetic-rejected-result"),
            ("true", True), ("false", False),
            ("nested-mapping", {"synthetic-rejected-result": {"nested": ["private"]}}),
            ("nested-sequence", ["synthetic-rejected-result", {"nested": "private"}]),
            ("nonserializable", object()),
            ("nan", float("nan")), ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")), ("float-zero", 0.0), ("none", None),
            ("below-range", -2147483649), ("above-range", 4294967296),
        )

    def malformed_captures(self):
        return tuple((name, value) for name, value in self.malformed_returncodes() if type(value) is not str) + (
            ("bytes", b"synthetic-rejected-result"),
        )

    def assert_invalid_arm(self, inputs, role, returncode):
        records = self.records(inputs)
        arm, = [item for item in records if item.get("role") == role and item["phase"] == "arm"]
        self.assertEqual(arm, {
            "event": "api106_approved_shell_observer_pair", "v": 1, "diagnostic_only": True,
            "phase": "arm", "role": role, "outcome": "process-or-cleanup-error", "elapsed_ms": 0,
            "returncode": returncode, "stdout": None,
        })
        raw = inputs[1].with_name("api106-approved-shell-observer-pair.jsonl").read_text(encoding="ascii")
        self.assertNotIn("synthetic-rejected-result", raw)
        self.assertFalse(records[-1]["acceptance"])
        return records

    def test_required_malformed_returncode_is_null_and_prevents_auxiliary(self):
        for name, value in self.malformed_returncodes():
            with self.subTest(value=name):
                inputs = self.inputs()
                self.arm_runner.reset_mock()
                self.arm_runner.side_effect = [self.result(value), self.result()]
                with self.assertRaisesRegex(ValueError, "^Observer comparison received an invalid process result$") as observed:
                    self.invoke(inputs)
                self.assertIs(type(observed.exception), ValueError)
                self.assertIsNone(observed.exception.__cause__)
                self.arm_runner.assert_called_once()
                records = self.assert_invalid_arm(inputs, "required", None)
                self.assertEqual(records[-1]["required"], "process-or-cleanup-error")
                self.assertEqual(records[-1]["auxiliary"], "not-executed")
                self.assertEqual(records[-2]["reason"], "required-process-or-cleanup-error")

    def test_required_malformed_capture_is_null_and_prevents_auxiliary(self):
        for name, value in self.malformed_captures():
            with self.subTest(value=name):
                inputs = self.inputs()
                self.arm_runner.reset_mock()
                self.arm_runner.side_effect = [self.result(stdout=value), self.result()]
                with self.assertRaisesRegex(ValueError, "^Observer comparison received an invalid capture result$") as observed:
                    self.invoke(inputs)
                self.assertIs(type(observed.exception), ValueError)
                self.assertIsNone(observed.exception.__cause__)
                self.arm_runner.assert_called_once()
                records = self.assert_invalid_arm(inputs, "required", 0)
                self.assertEqual(records[-1]["required"], "process-or-cleanup-error")
                self.assertEqual(records[-1]["auxiliary"], "not-executed")

    def test_auxiliary_malformed_result_cannot_change_required_outcome(self):
        for field, cases in (("code", self.malformed_returncodes()), ("stdout", self.malformed_captures())):
            for name, value in cases:
                for required in ("passed", "work-budget-exceeded", "assertion-failed"):
                    with self.subTest(field=field, value=name, required=required):
                        inputs = self.inputs()
                        error = (process_budget.BudgetExceeded("") if required == "work-budget-exceeded"
                                 else AssertionError("synthetic required failure") if required == "assertion-failed"
                                 else None)
                        if error is not None:
                            error.add_note("synthetic original required note")
                        self.arm_runner.reset_mock()
                        self.arm_runner.side_effect = [
                            self.result() if error is None else error, self.result(**{field: value}),
                        ]
                        if error is None:
                            self.invoke(inputs)
                        else:
                            with self.assertRaises(type(error)) as observed:
                                self.invoke(inputs)
                            self.assertIs(observed.exception, error)
                            self.assertIsNone(error.__cause__)
                            self.assertEqual(error.__notes__, ["synthetic original required note"])
                        self.assertEqual(self.arm_runner.call_count, 2)
                        records = self.assert_invalid_arm(
                            inputs, "auxiliary-no-lookup-delegates", None if field == "code" else 0,
                        )
                        self.assertEqual(records[-1]["required"], required)
                        self.assertEqual(records[-1]["auxiliary"], "process-or-cleanup-error")

    def test_valid_exit_and_signal_codes_keep_existing_assertion_semantics(self):
        for role in ("required", "auxiliary-no-lookup-delegates"):
            for code in (-2147483648, -15, -1, 0, 1, 255, 2147483647, 4294967295):
                with self.subTest(role=role, code=code):
                    inputs = self.inputs()
                    self.arm_runner.reset_mock()
                    self.arm_runner.side_effect = ([self.result(code), self.result()] if role == "required"
                                                   else [self.result(), self.result(code)])
                    if role == "required" and code:
                        with self.assertRaises(AssertionError):
                            self.invoke(inputs)
                    else:
                        self.invoke(inputs)
                    self.assertEqual(self.arm_runner.call_count, 2)
                    records = self.records(inputs)
                    arm, = [item for item in records if item.get("role") == role and item["phase"] == "arm"]
                    self.assertIs(type(arm["returncode"]), int)
                    self.assertEqual(arm["returncode"], code)
                    self.assertEqual(arm["outcome"], "assertion-failed" if code else "passed")
                    self.assertEqual(records[-1]["required"],
                                     "assertion-failed" if role == "required" and code else "passed")
                    self.assertFalse(records[-1]["acceptance"])

    def test_default_seam_is_dormant_and_original_method_keeps_its_position(self):
        module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        assignment, = [item for item in module.body if isinstance(item, ast.Assign)
                       and ast.unparse(item.targets[0]) == "APPROVED_SHELL_PAIR_ENCLOSURE"]
        self.assertIsNone(ast.literal_eval(assignment.value))
        method = ApprovedShellWriterReturnTest().fixture()
        gate, = [item for item in ast.walk(method) if isinstance(item, ast.If)
                 and ast.unparse(item.test) == "APPROVED_SHELL_PAIR_ENCLOSURE is not None"]
        self.assertEqual(len(gate.body), 1)
        self.assertIsInstance(gate.body[0], ast.Return)
        self.assertEqual(ast.unparse(gate.body[0].value), (
            "run_approved_shell_observer_pair(self, command, boundary, lookup, "
            "enclosure=APPROVED_SHELL_PAIR_ENCLOSURE)"
        ))
        self.assertNotIn("environ", ast.unparse(gate))
        self.assertNotIn("load_tests", [item.name for item in module.body if isinstance(item, ast.FunctionDef)])

    def test_auxiliary_only_omits_registration_and_corresponding_observer_records(self):
        command, boundary, lookup = self.inputs()
        original = list(command)
        auxiliary = approved_shell_auxiliary_command(command, boundary, lookup)
        self.assertEqual(command, original)
        self.assertEqual(auxiliary[:4], command[:4])
        setup = auxiliary[-1].split("$i=[Console]::IsInputRedirected;", 1)[0]
        self.assertNotIn("$s.PreProperty.SetValue($s.Intrinsics,$s.Pre);", setup)
        self.assertNotIn("$s.PostProperty.SetValue($s.Intrinsics,$s.Post);", setup)
        self.assertNotIn("$s.Armed=$true", setup)
        self.assertIn("if($s.Armed){", setup)
        self.assertIn("$s.NotFoundProperty.SetValue($s.Intrinsics,$s.NotFound);", setup)
        self.assertIn("$s.Done=$true;\n} catch {", setup)
        self.assertNotIn("""$fields='"phase":"observer" """.strip(), setup)
        root_prefix = APPROVED_SHELL_LOOKUP_SETUP.split("$observerStatus='unavailable';", 1)[0]
        self.assertIn(root_prefix, auxiliary[-1])
        self.assertTrue(auxiliary[-1].endswith(APPROVED_SHELL_LOOKUP_FINISH))
        self.assertEqual(auxiliary[-1].count("Write-Output 'approved-shell';"), 1)
        required_tail = command[-1].split("$i=[Console]::IsInputRedirected;", 1)[1]
        self.assertEqual(auxiliary[-1].split("$i=[Console]::IsInputRedirected;", 1)[1], required_tail)

    def test_required_command_cwd_caps_environment_and_assertions_are_unchanged(self):
        inputs = self.inputs()
        self.arm_runner.side_effect = [self.result(), self.result()]
        self.invoke(inputs)
        self.assertEqual(self.enclosure_calls, [{"seconds": 60, "cleanup_reserve": 12}])
        self.assertEqual(self.arm_runner.call_count, 2)
        self.assertIs(self.arm_runner.call_args_list[0].args[0], inputs[0])
        for call in self.arm_runner.call_args_list:
            self.assertEqual(call.args[1:], (SCRIPTS, 10))
            self.assertEqual(call.kwargs, {"capture": True})
        records = self.records(inputs)
        self.assertEqual([item["role"] for item in records if item["phase"] == "started"],
                         ["required", "auxiliary-no-lookup-delegates"])
        self.assertEqual((records[-1]["required"], records[-1]["auxiliary"]), ("passed", "passed"))
        self.assertFalse(records[-1]["acceptance"])
        self.assertEqual(inputs[1].read_bytes(), b"")
        self.assertEqual(inputs[2].read_bytes(), b"")

    def test_absolute_window_includes_prior_admission_instead_of_restarting_after_enter(self):
        @contextmanager
        def enclosure(**kwargs):
            self.now = 29
            yield SimpleNamespace(started=0)

        self.enclosure = enclosure
        inputs = self.inputs()
        with self.assertRaisesRegex(TimeoutError, "complete 10\\+10-second arm"):
            self.invoke(inputs)
        self.arm_runner.assert_not_called()
        self.assertEqual(self.records(inputs)[-1]["reason"], "insufficient-complete-arm-budget")

    def test_invalid_or_missing_absolute_window_refuses_before_report_or_provider(self):
        for started in (None, True, "synthetic", float("nan"), float("inf"), -1, 1):
            @contextmanager
            def enclosure(**kwargs):
                yield SimpleNamespace(started=started)

            with self.subTest(started=repr(started)):
                self.enclosure = enclosure
                inputs = self.inputs()
                with self.assertRaisesRegex(ValueError, "admitted absolute epoch"):
                    self.invoke(inputs)
                self.arm_runner.assert_not_called()
                self.assertFalse(inputs[1].with_name("api106-approved-shell-observer-pair.jsonl").exists())

    def test_required_timeout_is_not_replaced_by_auxiliary_success(self):
        inputs = self.inputs()
        error = process_budget.BudgetExceeded("")
        error.add_note("synthetic original note")
        self.arm_runner.side_effect = [error, self.result()]
        with self.assertRaises(process_budget.BudgetExceeded) as observed:
            self.invoke(inputs)
        self.assertIs(observed.exception, error)
        self.assertEqual(error.__notes__, ["synthetic original note"])
        self.assertEqual(self.records(inputs)[-1]["required"], "work-budget-exceeded")
        self.assertEqual(self.records(inputs)[-1]["auxiliary"], "passed")
        self.assertEqual(self.arm_runner.call_count, 2)

    def test_required_exit_and_output_assertions_still_fail_independently(self):
        for result in (self.result(7), self.result(stdout="not-approved-shell")):
            with self.subTest(code=result.returncode, stdout=result.stdout):
                inputs = self.inputs()
                self.arm_runner.side_effect = [result, self.result()]
                with self.assertRaises(AssertionError):
                    self.invoke(inputs)
                self.assertEqual(self.records(inputs)[-1]["required"], "assertion-failed")
                self.assertEqual(self.records(inputs)[-1]["auxiliary"], "passed")

    def test_auxiliary_failure_is_explicit_but_not_the_required_arm_result(self):
        for result in (self.result(9), self.result(stdout="different"), process_budget.BudgetExceeded("")):
            with self.subTest(result=type(result).__name__):
                inputs = self.inputs()
                self.arm_runner.side_effect = [self.result(), result]
                self.invoke(inputs)
                records = self.records(inputs)
                self.assertEqual(records[-1]["required"], "passed")
                self.assertIn(records[-1]["auxiliary"], ("assertion-failed", "work-budget-exceeded"))
                self.assertFalse(records[-1]["acceptance"])

    def test_unknown_required_ownership_or_cleanup_error_prevents_auxiliary(self):
        for error in (OSError("synthetic handle failure"), TimeoutError("synthetic cleanup limit"),
                      ValueError("synthetic setup refusal"), subprocess.TimeoutExpired("synthetic", 10)):
            with self.subTest(error=type(error).__name__):
                inputs = self.inputs()
                self.arm_runner.reset_mock()
                self.arm_runner.side_effect = error
                with self.assertRaises(type(error)) as observed:
                    self.invoke(inputs)
                self.assertIs(observed.exception, error)
                self.arm_runner.assert_called_once()
                self.assertEqual(self.records(inputs)[-1]["auxiliary"], "not-executed")

    def test_auxiliary_cleanup_failure_cannot_erase_required_timeout(self):
        inputs = self.inputs()
        error = process_budget.BudgetExceeded("")
        self.arm_runner.side_effect = [error, TimeoutError("synthetic auxiliary cleanup limit")]
        with self.assertRaises(process_budget.BudgetExceeded) as observed:
            self.invoke(inputs)
        self.assertIs(observed.exception, error)
        self.assertEqual(self.records(inputs)[-1]["auxiliary"], "process-or-cleanup-error")

    def test_enclosure_failure_preserves_required_exception_and_secondary_cause(self):
        @contextmanager
        def enclosure(**kwargs):
            yield SimpleNamespace(started=self.now)
            raise TimeoutError("synthetic enclosing cleanup limit")

        inputs = self.inputs()
        error = process_budget.BudgetExceeded("")
        self.arm_runner.side_effect = [error, self.result()]
        self.enclosure = enclosure
        with self.assertRaises(process_budget.BudgetExceeded) as observed:
            self.invoke(inputs)
        self.assertIs(observed.exception, error)
        self.assertIsInstance(error.__cause__, TimeoutError)
        self.assertIn("no complete-pair credit", error.__notes__[-1])

    def test_enclosure_failure_after_required_success_is_not_silenced(self):
        @contextmanager
        def enclosure(**kwargs):
            yield SimpleNamespace(started=self.now)
            raise TimeoutError("synthetic enclosing cleanup limit")

        self.enclosure = enclosure
        self.arm_runner.side_effect = [self.result(), self.result()]
        with self.assertRaisesRegex(TimeoutError, "enclosing cleanup"):
            self.invoke(self.inputs())

    def test_enclosure_cannot_suppress_prearm_failure_as_success(self):
        @contextmanager
        def enclosure(**kwargs):
            try:
                yield SimpleNamespace(started=self.now)
            except ValueError:
                pass

        self.enclosure = enclosure
        inputs = self.inputs()
        inputs[0][1] = "-File"
        with self.assertRaisesRegex(RuntimeError, "incomplete protocol"):
            self.invoke(inputs)
        self.arm_runner.assert_not_called()
        self.assertNotIn("complete", [record["phase"] for record in self.records(inputs)])

    def test_budget_reserve_refuses_auxiliary_without_changing_its_inner_limit(self):
        inputs = self.inputs()

        def slow_required(*args, **kwargs):
            self.now = 29
            return self.result()

        self.arm_runner.side_effect = slow_required
        with self.assertRaisesRegex(TimeoutError, "complete 10\\+10-second arm"):
            self.invoke(inputs)
        self.arm_runner.assert_called_once()
        self.assertEqual(self.records(inputs)[-1]["reason"], "insufficient-complete-arm-budget")

    def test_budget_is_checked_again_after_the_start_record(self):
        inputs = self.inputs()
        with patch.object(time, "monotonic", side_effect=[0, 0, 29]):
            with self.assertRaisesRegex(TimeoutError, "complete 10\\+10-second arm"):
                self.invoke(inputs)
        self.arm_runner.assert_not_called()
        self.assertEqual(self.records(inputs)[-1]["outcome"], "not-executed")

    def test_late_pair_cannot_report_completion(self):
        inputs = self.inputs()

        def late_auxiliary(*args, **kwargs):
            if self.arm_runner.call_count == 2:
                self.now = 49
            return self.result()

        self.arm_runner.side_effect = late_auxiliary
        with self.assertRaisesRegex(TimeoutError, "48-second work"):
            self.invoke(inputs)
        self.assertEqual(self.arm_runner.call_count, 2)
        self.assertNotIn("complete", [record["phase"] for record in self.records(inputs)])

    def test_late_report_finalization_cannot_become_success(self):
        inputs = self.inputs()
        self.arm_runner.side_effect = [self.result(), self.result()]
        with patch.object(time, "monotonic", side_effect=[0] * 8 + [49]):
            with self.assertRaisesRegex(TimeoutError, "finalization"):
                self.invoke(inputs)
        self.assertEqual(self.arm_runner.call_count, 2)
        self.assertFalse(self.records(inputs)[-1]["acceptance"])

    def test_claim_and_each_auxiliary_file_are_exclusive_and_never_removed(self):
        for name in ("api106-approved-shell-observer-pair.jsonl",
                     "api106-approved-shell-auxiliary-boundary.bin",
                     "api106-approved-shell-auxiliary-module-lookup.jsonl"):
            with self.subTest(name=name):
                inputs = self.inputs()
                path = inputs[1].with_name(name)
                path.write_bytes(b"synthetic prior observation")
                with self.assertRaises(FileExistsError):
                    self.invoke(inputs)
                self.assertEqual(path.read_bytes(), b"synthetic prior observation")
                self.arm_runner.assert_not_called()

    def test_second_invocation_cannot_repeat_either_arm(self):
        inputs = self.inputs()
        self.arm_runner.side_effect = [self.result(), self.result()]
        self.invoke(inputs)
        before = self.records(inputs)
        with self.assertRaises(FileExistsError):
            self.invoke(inputs)
        self.assertEqual(self.arm_runner.call_count, 2)
        self.assertEqual(self.records(inputs), before)

    def test_unknown_flags_setup_or_bindings_refuse_before_either_arm(self):
        for change in ("flags", "setup", "duplicate-binding"):
            with self.subTest(change=change):
                command, boundary, lookup = self.inputs()
                if change == "flags":
                    command[1] = "-File"
                elif change == "setup":
                    command[-1] = command[-1].replace(APPROVED_SHELL_LOOKUP_SETUP, "")
                else:
                    command[-1] += "$pennilogicBoundary='" + str(boundary).replace("'", "''") + "';"
                with self.assertRaises(ValueError):
                    self.invoke((command, boundary, lookup))
                self.arm_runner.assert_not_called()

    def test_report_has_no_raw_output_exception_text_or_paths(self):
        inputs = self.inputs()
        secret = "synthetic-private-diagnostic-text"
        self.arm_runner.side_effect = [self.result(stdout=secret), OSError(secret)]
        with self.assertRaises(AssertionError):
            self.invoke(inputs)
        records = self.records(inputs)
        text = json.dumps(records)
        self.assertNotIn(secret, text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("powershell.exe", text)
        required, = [item for item in records if item.get("role") == "required" and item["phase"] == "arm"]
        self.assertEqual(required["stdout"], {
            "text_bytes": len(secret), "sha256": hashlib.sha256(secret.encode()).hexdigest(),
        })


if __name__ == "__main__":
    unittest.main()

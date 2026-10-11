"""Synthetic report probes test the consumer, never stand in for real PIT results."""

from collections import Counter
import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
        self.directory = self.root / "build/test-results/moneyTest"
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
            directory = root / "build/test-results/moneyTest"
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
            directory = root / "build/test-results/moneyTest"
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
                result = quality.run(
                    [sys.executable, "-I", "-S", "-B", "-c", code], root=root, capture=True, budget=10,
                )
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
            ProcessBudgetCaptureObservationTest().state(notes[0])
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
        api = FakeJobObservationApi()
        api.kernel32.CreateJobObjectW = Mock(return_value=api.JOB)
        api.kernel32.SetInformationJobObject = Mock(return_value=1)
        api.kernel32.AssignProcessToJobObject = Mock(return_value=1)
        api.kernel32.OpenProcess = Mock(return_value=100011)
        api.kernel32.CloseHandle = Mock(return_value=1)
        with patch.object(process_budget.ctypes, "WinDLL", side_effect=api.load_library, create=True):
            job = process_budget.WindowsJob()
            job.attach(SimpleNamespace(pid=11))
            job.close()
        api.load_library.assert_called_once_with("kernel32", use_last_error=True)
        api.kernel32.OpenProcess.assert_called_once_with(0x0101, False, 11)
        api.kernel32.AssignProcessToJobObject.assert_called_once_with(api.JOB, 100011)
        self.assertEqual([call.args[0] for call in api.kernel32.CloseHandle.call_args_list], [100011, api.JOB])
        self.assertIsNone(job.handle)
        api.CompareObjectHandles.assert_not_called()
        api.QueryInformationJobObject.assert_not_called()

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


class ProcessBudgetJobRunTest(unittest.TestCase):
    def run_mocked(self, *, platform="nt", failure="elapsed", exit_code=0, inherit_tree=False, query_error=0,
                   missing_provider=None, capture=True, separate=True):
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
        process.wait.assert_called_once_with(timeout=10)
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


if __name__ == "__main__":
    unittest.main()

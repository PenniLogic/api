"""Synthetic report probes test the consumer, never stand in for real PIT results."""

from collections import Counter
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("money_mutation", SCRIPTS / "money_mutation.py")
mutation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mutation)
quality = mutation.quality
process_budget = quality.script_module("process_budget")


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


class MutationEvidenceTest(unittest.TestCase):
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
                }
                store(record)
                return record

            with (
                patch.object(mutation, "ROOT", root),
                patch.object(quality, "money_provider_module", return_value=provider),
                patch.object(quality, "money_class_inventory", return_value={fixture.target.replace(".", "/"): "synthetic"}),
                patch.object(mutation, "input_snapshot", return_value={"synthetic": "bound"}) as snapshot,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                save(90)
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
            self.assertEqual(3, evidence["target_tests"])
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
            self.assertEqual(3, quality.money_test_metrics(root)["target_tests"])
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


if __name__ == "__main__":
    unittest.main()

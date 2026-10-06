"""Portable quality commands. CI supplies the reviewed base explicitly for coverage."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path("build/reports/jacoco/test/jacocoTestReport.xml")
BASELINE = "quality/coverage-baseline.json"
MONEY_REPORT = Path("build/reports/jacoco/moneyCoverageReport/moneyCoverageReport.xml")
MONEY_CLASSES = Path("build/classes/kotlin/contractsMoney")
MONEY_PACKAGE = "com/pennilogic/contracts/money"
MONEY_BASELINE = "quality/money-coverage-baseline.json"


def run(command, root=ROOT, capture=False, budget=None):
    print("+ " + subprocess.list2cmdline([str(part) for part in command]), flush=True)
    if budget is not None:
        result = script_module("process_budget").run(command, root, budget, capture)
    else:
        result = subprocess.run(
            command, cwd=root, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None, check=False,
        )
    if result.returncode:
        if capture:
            print(result.stdout)
        raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
    return result.stdout if capture else ""


def gradle(*tasks, root=ROOT, capture=False, budget=None):
    wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    command = [str(wrapper)] if os.name == "nt" else ["sh", str(wrapper)]
    return run(
        [*command, "--no-daemon", "--console=plain", "-Pkotlin.compiler.execution.strategy=in-process", *tasks],
        root, capture, budget,
    )


def read_report(path):
    report = ET.parse(path).getroot()
    counters = {}
    for kind in ("LINE", "BRANCH"):
        counter = report.find(f"./counter[@type='{kind}']")
        if counter is None:
            raise ValueError(f"Coverage report lacks {kind} counter")
        covered, missed = int(counter.get("covered")), int(counter.get("missed"))
        if covered < 0 or missed < 0 or covered + missed == 0:
            raise ValueError(f"Coverage report has empty/invalid {kind} counter")
        counters[kind] = {"covered": covered, "total": covered + missed}
    executable = {}
    for package in report.findall("package"):
        for source in package.findall("sourcefile"):
            path = f"src/main/kotlin/{package.get('name')}/{source.get('name')}"
            executable[path] = {
                int(line.get("nr")): int(line.get("ci")) > 0
                for line in source.findall("line")
                if int(line.get("ci")) + int(line.get("mi")) > 0
            }
    if not executable:
        raise ValueError("Coverage report contains no executable source files")
    return counters, executable


def ratio(counter):
    covered, total = counter["covered"], counter["total"]
    if type(covered) is not int or type(total) is not int or not 0 <= covered <= total or total <= 0:
        raise ValueError("Invalid baseline coverage counter")
    return Fraction(covered, total)


def enforce(counters, changed, baseline=None):
    for kind in ("LINE", "BRANCH"):
        actual = ratio(counters[kind])
        if actual < Fraction(80, 100):
            raise ValueError(f"{kind} coverage is below 80 percent")
        if baseline is not None and actual < ratio(baseline[kind]):
            raise ValueError(f"{kind} coverage decreased from reviewed base")
    if changed and sum(changed) * 10 < len(changed) * 9:
        raise ValueError("Changed executable line coverage is below 90 percent")


def changed_line_numbers(diff):
    numbers = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        numbers.update(range(start, start + count))
    return numbers


def check_coverage(base, write_baseline=False):
    # No origin/main or environment fallback: the caller must name its reviewed base.
    if not base or not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        raise ValueError("coverage requires --base with the full trusted base commit SHA")
    run(["git", "cat-file", "-e", f"{base}^{{commit}}"])
    counters, executable = read_report(ROOT / REPORT)
    sources = {path.relative_to(ROOT).as_posix() for path in (ROOT / "src/main/kotlin").rglob("*.kt")}
    if sources != set(executable):
        raise ValueError("Coverage report source inventory does not match shipped Kotlin source")
    base_paths = set(run(["git", "ls-tree", "-r", "--name-only", base], capture=True).splitlines())
    baseline = None
    if BASELINE in base_paths:
        baseline = json.loads(run(["git", "show", f"{base}:{BASELINE}"], capture=True))
    elif any(path.startswith("src/main/kotlin/") for path in base_paths):
        raise ValueError("Scaffolded base is missing its coverage baseline")
    changed = []
    for path, lines in executable.items():
        if path not in base_paths:
            numbers = set(lines)
        else:
            diff = run(
                ["git", "diff", "--no-ext-diff", "--no-textconv", "--unified=0", base, "--", path],
                capture=True,
            )
            numbers = changed_line_numbers(diff)
        changed.extend(covered for number, covered in lines.items() if number in numbers)
    try:
        enforce(counters, changed, baseline)
    except ValueError as error:
        print(json.dumps({
            "event": "coverage_refused",
            "reason": str(error),
            "counters": counters,
            "reviewed_counters": baseline,
            "changed_covered": sum(changed),
            "changed_total": len(changed),
            "base": base,
        }))
        raise
    record = {
        "event": "coverage",
        "counters": counters,
        "changed_covered": sum(changed),
        "changed_total": len(changed),
        "base": base,
    }
    print(json.dumps(record))
    if write_baseline:
        target = ROOT / BASELINE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(counters, indent=2) + "\n", encoding="utf-8")
    elif json.loads((ROOT / BASELINE).read_text(encoding="utf-8")) != counters:
        raise ValueError("Coverage baseline does not match report; run coverage --write-baseline and review it")


def script_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def money_provider_module():
    return script_module("money_provider")


def money_policy(provider):
    content = provider.read_strategy(ROOT / provider.BUNDLE / provider.STRATEGY_FILE)
    strategy = json.loads(content)
    packages = [package for package in strategy.get("packages", []) if package.get("id") == "api.money"]
    if len(packages) != 1 or packages[0].get("repository") != "PenniLogic/api" or packages[0].get("money_path") is not True:
        raise ValueError("Accepted test strategy does not uniquely declare the API money package")
    floors = {
        "LINE": packages[0].get("line_coverage_floor_percent"),
        "BRANCH": packages[0].get("branch_coverage_floor_percent"),
        "MUTATION": packages[0].get("mutation_score_floor_percent"),
    }
    if any(type(value) is not int or not 0 < value <= 100 for value in floors.values()):
        raise ValueError("Accepted money-package strategy lacks valid numeric qualification floors")
    if strategy.get("floor_policy", {}).get("money_path_ratchet") is not True:
        raise ValueError("Accepted strategy does not declare the required money coverage ratchet")
    return floors, hashlib.sha256(content).hexdigest()


def money_budget(provider):
    strategy = json.loads(provider.read_strategy(ROOT / provider.BUNDLE / provider.STRATEGY_FILE))
    budgets = strategy.get("pipeline_budgets", {})
    names = ("money_path_harness_minutes", "pull_request_gate_minutes")
    if any(type(budgets.get(name)) is not int or budgets[name] <= 0 for name in names):
        raise ValueError("Accepted strategy lacks numeric Money harness and pipeline budgets")
    return {
        **{name: budgets[name] for name in names},
        "native_process_seconds": 600,
        "enforced_seconds": min(600, *(budgets[name] * 60 for name in names)),
    }


def read_money_coverage(path, class_names, source_names):
    report = ET.parse(path).getroot()
    packages = report.findall("package")
    if len(packages) != 1 or packages[0].get("name") != MONEY_PACKAGE:
        raise ValueError("Money coverage must report the complete authoritative package, not unrelated API classes")
    package = packages[0]
    classes = [entry.get("name") for entry in package.findall("class")]
    sources = [entry.get("name") for entry in package.findall("sourcefile")]
    if not class_names or len(classes) != len(set(classes)) or set(classes) != set(class_names):
        raise ValueError("Money coverage class inventory differs from the actual compiled dependency")
    if not source_names or len(sources) != len(set(sources)) or set(sources) != set(source_names):
        raise ValueError("Money coverage source inventory differs from the immutable provider inputs")
    counters = {}
    for kind in ("LINE", "BRANCH"):
        matching = report.findall(f"./counter[@type='{kind}']")
        package_matching = package.findall(f"./counter[@type='{kind}']")
        if len(matching) != 1 or len(package_matching) != 1:
            raise ValueError(f"Money coverage report lacks a unique {kind} counter")
        counter = matching[0]
        if counter.attrib != package_matching[0].attrib:
            raise ValueError(f"Money coverage {kind} package and report counters differ")
        missed, covered = int(counter.get("missed")), int(counter.get("covered"))
        counters[kind] = {"covered": covered, "total": covered + missed}
        ratio(counters[kind])
    return counters


def enforce_money_coverage(counters, floors, baseline=None):
    for kind in ("LINE", "BRANCH"):
        actual = ratio(counters[kind])
        if actual < Fraction(floors[kind], 100):
            raise ValueError(f"Authoritative Money {kind} coverage below the accepted floor")
        if baseline is not None and actual < ratio(baseline[kind]):
            raise ValueError(f"Authoritative Money {kind} coverage decreased from the recorded baseline")


def money_class_inventory(provider):
    compiled = ROOT / MONEY_CLASSES
    if not compiled.is_dir() or compiled.is_symlink() or compiled.is_junction():
        raise ValueError("Authoritative Money dependency classes are missing or linked")
    classes = {}
    for directory, directories, files in os.walk(compiled, followlinks=False, onerror=provider.refused_walk):
        parent = Path(directory)
        if any((parent / name).is_symlink() or (parent / name).is_junction() for name in directories):
            raise ValueError("Linked Money class inventory cannot qualify coverage")
        for name in files:
            path = parent / name
            if path.suffix == ".class":
                if path.is_symlink():
                    raise ValueError("Linked Money class cannot qualify coverage")
                classes[path.relative_to(compiled).with_suffix("").as_posix()] = path
    if not classes or any(not name.startswith(MONEY_PACKAGE + "/") for name in classes):
        raise ValueError("Authoritative Money class inventory is empty or contains another package")
    return classes


def money_test_metrics(root=ROOT):
    suites = [ET.parse(path).getroot() for path in (root / "build/test-results/moneyTest").glob("TEST-*.xml")]
    identities = set()
    for suite in suites:
        if any(
            not re.fullmatch(r"0|[1-9][0-9]*", suite.get(name, ""))
            for name in ("tests", "skipped", "failures", "errors")
        ):
            raise ValueError("Money test evidence lacks valid numeric execution counters")
        cases = suite.findall("testcase")
        if suite.tag != "testsuite" or len(cases) != int(suite.get("tests")) or any(
            sum(case.find(element) is not None for case in cases) != int(suite.get(counter))
            for counter, element in (("skipped", "skipped"), ("failures", "failure"), ("errors", "error"))
        ):
            raise ValueError("Money test counters disagree with their executed cases")
        for case in cases:
            identity = (case.get("classname", ""), case.get("name", ""))
            if not identity[0].startswith("com.pennilogic.money.") or not identity[1] or identity in identities:
                raise ValueError("Money test evidence has an unrelated, unnamed or duplicate case")
            identities.add(identity)
    tests = sum(int(suite.get("tests")) for suite in suites)
    skipped = sum(int(suite.get("skipped")) for suite in suites)
    failed = sum(int(suite.get("failures")) + int(suite.get("errors")) for suite in suites)
    if not tests or skipped or failed:
        raise ValueError("Money qualification requires executed target tests without failures or skips")
    properties = {}
    for suite in suites:
        for node in suite.findall("system-out"):
            for line in (node.text or "").splitlines():
                if line.startswith('{"event":"money_property_cases"'):
                    event = json.loads(line)
                    name, count = event.get("property"), event.get("count")
                    if name in properties or type(count) is not int or count <= 0:
                        raise ValueError("Money property case evidence is duplicated or non-numeric")
                    properties[name] = count
    if set(properties) != {"round_trip", "associativity", "collision_keys"}:
        raise ValueError("Money property case evidence is missing or has an unknown category")
    return {
        "target_tests": tests, "skipped": skipped, "failed": failed, "property_cases": properties,
        "independent_oracle": {"status": "not_implemented", "disagreements": None},
    }


def check_money_coverage(base=None, write_baseline=False):
    provider = money_provider_module()
    source_paths = provider.verify_outputs()
    floors, strategy_sha256 = money_policy(provider)
    class_names = set(money_class_inventory(provider))
    counters = read_money_coverage(
        ROOT / MONEY_REPORT, class_names, {path.name for path in source_paths},
    )
    test_evidence = money_test_metrics()
    record = {
        "event": "money_package_coverage",
        "strategy_package": "api.money",
        "jvm_package": MONEY_PACKAGE,
        "contracts_source_ref": provider.SOURCE_REF,
        "docs_source_ref": provider.DOCS_REF,
        "strategy_sha256": strategy_sha256,
        "source_file_count": len(source_paths),
        "compiled_class_count": len(class_names),
        "source_files": sorted(path.name for path in source_paths),
        "compiled_classes": sorted(class_names),
        **test_evidence,
        "counters": counters,
        "floor_percent": floors,
        "mutation": {
            "status": "separate_required_gate",
            "command": "python scripts/quality.py money-mutation",
            "reason": "Coverage is not mutation evidence; moneyMutation runs unconditionally in check",
        },
    }
    print(json.dumps(record))
    enforce_money_coverage(counters, floors)
    target = ROOT / MONEY_BASELINE
    baseline_record = {
        "strategy_package": "api.money",
        "contracts_source_ref": provider.SOURCE_REF,
        "source_file_count": len(source_paths),
        "compiled_class_count": len(class_names),
        **counters,
    }
    baseline = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
    if baseline is not None:
        if baseline["strategy_package"] != "api.money":
            raise ValueError("Recorded money baseline names a different package")
        enforce_money_coverage(counters, floors, baseline)
    if base is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
            raise ValueError("Money coverage ratchet requires the full trusted base commit SHA")
        paths = set(run(["git", "ls-tree", "-r", "--name-only", base], capture=True).splitlines())
        if MONEY_BASELINE in paths:
            reviewed = json.loads(run(["git", "show", f"{base}:{MONEY_BASELINE}"], capture=True))
            enforce_money_coverage(counters, floors, reviewed)
        else:
            print(json.dumps({
                "event": "money_coverage_initial_baseline",
                "base": base,
                "reason": "No authoritative-package coverage baseline existed at this base",
            }))
    if write_baseline:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(baseline_record, indent=2) + "\n", encoding="utf-8", newline="\n")
    elif baseline != baseline_record:
        raise ValueError("Money coverage baseline differs from the measured report; generate and review the actual baseline")


def gate_self_test(artifact_dir):
    inputs = [
        "settings.gradle.kts", "build.gradle.kts", "gradle.properties", ".editorconfig",
        "gradlew", "gradlew.bat", "gradle.lockfile", "gradle", "src", "scripts/check_money.py",
        "scripts/money_provider.py", "build/contracts-money/source",
        "scripts/prepare_database_admission.py", "scripts/materialize_money_sources.py", "build/database-admission",
        "database/admission-inventory.json",
        "scripts/quality.py", "build/contracts-money/strategy",
        "scripts/money_mutation.py", "scripts/process_budget.py", "scripts/PitCatalogue.java",
        MONEY_BASELINE,
    ]
    with tempfile.TemporaryDirectory(prefix="api-gate-self-test-", dir=artifact_dir) as temporary:
        root = Path(temporary)
        for name in inputs:
            source, destination = ROOT / name, root / name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        gradle("test", "spotlessCheck", root=root)
        tests = root / "src/test/kotlin/com/pennilogic/bootstrap"
        failing_test = tests / "GateFailureTest.kt"
        failing_test.write_text(
            'package com.pennilogic.bootstrap\n'
            'import org.junit.jupiter.api.Test\n'
            'import org.junit.jupiter.api.Assertions.assertEquals\n'
            'class GateFailureTest {\n'
            '    @Test fun gateRejectsWrongResult() { assertEquals(2, 1 + 2) }\n'
            '}\n', encoding="utf-8",
        )
        try:
            gradle("test", root=root)
        except subprocess.CalledProcessError:
            xml = root / "build/test-results/test/TEST-com.pennilogic.bootstrap.GateFailureTest.xml"
            if not xml.is_file():
                raise ValueError("Test command failed without executing the planted assertion")
            suite = ET.parse(xml).getroot()
            failure = suite.find(".//failure")
            if (
                int(suite.get("tests", "0")) != 1
                or int(suite.get("failures", "0")) != 1
                or failure is None
                or failure.get("type") != "org.opentest4j.AssertionFailedError"
            ):
                raise ValueError("Test command failed without the expected assertion failure")
            print('{"event":"gate_self_test","gate":"test","rejected":true}')
        else:
            raise ValueError("Test gate accepted a failing assertion")
        finally:
            failing_test.unlink()
        bad_format = tests / "GateLint.kt"
        bad_format.write_text(
            'package com.pennilogic.bootstrap\n\nclass GateLint{val value=1}\n', encoding="utf-8",
        )
        try:
            gradle("build", root=root, capture=True)
        except subprocess.CalledProcessError as error:
            if "GateLint.kt" not in error.output or "spotlessKotlinCheck" not in error.output:
                raise ValueError("Lint failure was not the planted formatting violation")
            print('{"event":"gate_self_test","gate":"lint","rejected":true}')
        else:
            raise ValueError("Lint gate accepted a formatting violation")
        finally:
            bad_format.unlink()
        bad_money = tests / "GateMoney.kt"
        defects = [
            ("double", "MG001", "data class GateMoney(\n    val amount: Double,\n)"),
            ("float", "MG001", "data class GateMoney(\n    val amount: Float,\n)"),
            ("bigdecimal", "MG001", "data class GateMoney(\n    val amount: java.math.BigDecimal,\n)"),
            ("conversion", "MG003", "fun forbiddenConversion(amount: Long): Double = amount.toDouble()"),
            ("raw_arithmetic", "MG002", "fun forbiddenArithmetic(\n    amount: Long,\n    fee: Long,\n): Long = amount + fee"),
        ]
        for case, rule, declaration in defects:
            bad_money.write_text(
                f"package com.pennilogic.bootstrap\n\n{declaration}\n", encoding="utf-8", newline="\n",
            )
            try:
                gradle("build", root=root, capture=True)
            except subprocess.CalledProcessError as error:
                if "GateMoney.kt" not in error.output or rule not in error.output or ":moneyGuard FAILED" not in error.output:
                    raise ValueError(f"Money guard failure was not the planted {case} defect")
                print(json.dumps({"event": "gate_self_test", "gate": "money", "case": case, "rejected": True}))
            else:
                raise ValueError(f"Money guard accepted the planted {case} defect")
            finally:
                bad_money.unlink()
        gradle("build", root=root)


def test_metrics(root=ROOT):
    suites = [ET.parse(path).getroot() for path in (root / "build/test-results/test").glob("TEST-*.xml")]
    count = sum(int(suite.get("tests")) for suite in suites)
    skipped = sum(int(suite.get("skipped")) for suite in suites)
    failed = sum(int(suite.get("failures")) + int(suite.get("errors")) for suite in suites)
    print(json.dumps({"event": "tests", "count": count, "skipped": skipped, "failed": failed}))
    if count == 0 or skipped or failed:
        raise ValueError("The scaffold suite must execute real tests without skips/failures")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "version", "install", "build", "test", "lint", "format", "coverage", "gate-self-test", "money-guard",
            "money-coverage", "money-coverage-report",
            "money-mutation", "money-mutation-report",
        ],
    )
    parser.add_argument("--base", help="Full trusted PR base commit SHA; required for coverage")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, help="Parent directory for isolated gate self-test copies")
    args = parser.parse_args()
    started = time.monotonic()
    budget = None
    try:
        if args.command == "version":
            gradle("--version")
        elif args.command == "install":
            gradle("resolveDependencies")
        elif args.command == "build":
            budget = money_budget(money_provider_module())["enforced_seconds"]
            gradle("build", "installDist", budget=budget - (time.monotonic() - started))
            test_metrics()
        elif args.command == "test":
            run([sys.executable, "-m", "unittest", "discover", "-s", "scripts/tests", "-v"])
            gradle("test")
            test_metrics()
        elif args.command == "lint":
            gradle("spotlessCheck", "compileKotlin", "compileTestKotlin")
        elif args.command == "format":
            gradle("spotlessApply")
        elif args.command == "coverage":
            if not args.base:
                parser.error("coverage requires --base; no implicit or stale CI base is accepted")
            budget = money_budget(money_provider_module())["enforced_seconds"]
            gradle(
                "jacocoTestReport", "jacocoTestCoverageVerification", "moneyCoverageReport", "moneyMutation",
                budget=budget - (time.monotonic() - started),
            )
            check_coverage(args.base, args.write_baseline)
            check_money_coverage(args.base, args.write_baseline)
        elif args.command == "money-guard":
            run([sys.executable, str(ROOT / "scripts/check_money.py")])
        elif args.command == "money-coverage":
            gradle("moneyCoverageReport")
            check_money_coverage(args.base, args.write_baseline)
        elif args.command == "money-coverage-report":
            check_money_coverage(args.base, args.write_baseline)
        elif args.command == "money-mutation":
            budget = money_budget(money_provider_module())["enforced_seconds"]
            gradle("moneyMutation", budget=budget - (time.monotonic() - started))
        elif args.command == "money-mutation-report":
            script_module("money_mutation").check_latest()
        else:
            gate_self_test(args.artifact_dir)
        if args.command in ("build", "coverage", "money-mutation"):
            script_module("money_mutation").check_latest()
        if budget is not None and time.monotonic() - started > budget:
            raise TimeoutError("Quality command exceeded its enforced elapsed budget")
    except (subprocess.CalledProcessError, ValueError, OSError, ET.ParseError) as error:
        print(f"Quality command failed: {error}", file=sys.stderr)
        return 1
    finally:
        print(json.dumps({"event": "quality_duration", "command": args.command, "seconds": round(time.monotonic() - started, 3)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

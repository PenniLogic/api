"""Portable quality commands. CI supplies the reviewed base explicitly for coverage."""

import argparse
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


def run(command, root=ROOT, capture=False):
    print("+ " + subprocess.list2cmdline([str(part) for part in command]), flush=True)
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


def gradle(*tasks, root=ROOT, capture=False):
    wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    command = [str(wrapper)] if os.name == "nt" else ["sh", str(wrapper)]
    return run([*command, "--no-daemon", "--console=plain", *tasks], root, capture)


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
    enforce(counters, changed, baseline)
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


def gate_self_test(artifact_dir):
    inputs = [
        "settings.gradle.kts", "build.gradle.kts", "gradle.properties", ".editorconfig",
        "gradlew", "gradlew.bat", "gradle.lockfile", "gradle", "src",
    ]
    with tempfile.TemporaryDirectory(prefix="api-gate-self-test-", dir=artifact_dir) as temporary:
        root = Path(temporary)
        for name in inputs:
            source, destination = ROOT / name, root / name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
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
    parser.add_argument("command", choices=["version", "install", "build", "test", "lint", "format", "coverage", "gate-self-test"])
    parser.add_argument("--base", help="Full trusted PR base commit SHA; required for coverage")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, help="Parent directory for isolated gate self-test copies")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        if args.command == "version":
            gradle("--version")
        elif args.command == "install":
            gradle("resolveDependencies")
        elif args.command == "build":
            gradle("build", "installDist")
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
            gradle("jacocoTestReport", "jacocoTestCoverageVerification")
            check_coverage(args.base, args.write_baseline)
        else:
            gate_self_test(args.artifact_dir)
    except (subprocess.CalledProcessError, ValueError, OSError, ET.ParseError) as error:
        print(f"Quality command failed: {error}", file=sys.stderr)
        return 1
    finally:
        print(json.dumps({"event": "quality_duration", "command": args.command, "seconds": round(time.monotonic() - started, 3)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

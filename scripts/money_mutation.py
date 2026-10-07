"""Real PIT mutation execution and fail-closed qualification of the pinned Money package."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location("mutation_quality", Path(__file__).with_name("quality.py"))
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)
ROOT = quality.ROOT
REPORTS = Path("build/reports/money-mutation")
INPUT_FILE = Path("build/money-mutation-input.json")
STATUSES = (
    "KILLED", "SURVIVED", "NO_COVERAGE", "TIMED_OUT", "NON_VIABLE",
    "MEMORY_ERROR", "RUN_ERROR", "STARTED", "NOT_STARTED",
)
ERROR_STATUSES = ("NON_VIABLE", "MEMORY_ERROR", "RUN_ERROR", "STARTED", "NOT_STARTED")
TEST_PACKAGE = "com.pennilogic.money."
SCHEMA_VERSION = 2
FEATURE_SELECTION = ("-flogcall", "+fkotlin")


def file_record(path):
    if not path.is_file() or path.is_symlink() or path.is_junction():
        raise ValueError("Mutation input or output is missing or linked")
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def runtime_executable(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Mutation runtime executable must have an absolute path")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Mutation runtime must resolve to an executable regular file")
    return resolved


def write_record(path, value):
    quality.money_provider_module().owned_target(ROOT, path.relative_to(ROOT))
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def input_snapshot(config):
    provider = quality.money_provider_module()
    provider.verify_outputs()
    files = [
        ROOT / name for name in (
            "build.gradle.kts", "settings.gradle.kts", "gradle.lockfile",
            "gradle/verification-metadata.xml", "scripts/quality.py", "scripts/process_budget.py",
            "scripts/money_mutation.py", "scripts/PitCatalogue.java", "scripts/money_provider.py",
            "scripts/check_money.py",
        )
    ]
    files.extend(provider.verify_outputs())
    files.extend(ROOT / provider.BUNDLE / "source" / name for name in provider.SOURCE_FILES)
    files.append(ROOT / provider.BUNDLE / provider.STRATEGY_FILE)
    files.append(runtime_executable(config["java"]))
    files.append(runtime_executable(sys.executable))
    files.extend(path for path in (ROOT / "src").rglob("*") if path.suffix in (".kt", ".java"))
    files.extend((ROOT / "build/test-results/moneyTest").glob("TEST-*.xml"))
    files.extend(quality.money_class_inventory(provider).values())
    for name in ("tool_classpath", "test_classpath"):
        paths = config.get(name)
        if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
            raise ValueError("Mutation classpath inventory is empty or duplicated")
        for entry in paths:
            path = Path(entry)
            if not path.is_absolute() or path.is_symlink() or path.is_junction() or "," in entry:
                raise ValueError("Mutation classpath has an unsafe or unsupported path")
            if path.is_dir():
                for directory, directories, names in os.walk(path, followlinks=False, onerror=provider.refused_walk):
                    parent = Path(directory)
                    if any((parent / item).is_symlink() or (parent / item).is_junction() for item in directories):
                        raise ValueError("Mutation classpath directory contains a link")
                    files.extend(parent / item for item in names)
            else:
                files.append(path)
    return {str(path): file_record(path) for path in sorted(set(files))}


def engine_versions(config):
    names = [Path(path).name for path in config["tool_classpath"]]
    result = {}
    for artifact in ("pitest", "pitest-entry", "pitest-command-line", "pitest-junit5-plugin", "junit-platform-launcher"):
        matches = [
            match.group(1) for name in names
            if (match := re.fullmatch(re.escape(artifact) + r"-(\d+\.\d+\.\d+)\.jar", name))
        ]
        if len(matches) != 1:
            raise ValueError("Mutation engine or JUnit Platform dependency is missing or ambiguous")
        result[artifact] = matches[0]
    if len({result[name] for name in ("pitest", "pitest-entry", "pitest-command-line")}) != 1:
        raise ValueError("PIT engine component versions disagree")
    return result


def read_catalogue(output):
    catalogue = {}
    features = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError("PIT returned an invalid mutator catalogue")
        kind, name, identifier = parts
        if kind == "FEATURE":
            if not re.fullmatch(r"[a-z0-9]+", name) or name in features or identifier not in ("true", "false"):
                raise ValueError("PIT returned an invalid feature catalogue")
            features[name] = {"default_enabled": identifier == "true", "enabled": identifier == "true"}
            continue
        if (
            kind != "MUTATOR" or not re.fullmatch(r"[A-Za-z0-9_\[\]-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9_.$]+", identifier)
        ):
            raise ValueError("PIT returned an invalid mutator catalogue")
        if identifier in catalogue:
            raise ValueError("PIT returned a duplicate mutator identifier")
        catalogue[identifier] = name
    if not catalogue or not features:
        raise ValueError("PIT returned an empty mutator catalogue")
    for selection in FEATURE_SELECTION:
        name = selection[1:]
        if name not in features:
            raise ValueError("Required PIT filter control is unavailable")
        features[name]["enabled"] = selection.startswith("+")
    return dict(sorted(catalogue.items())), dict(sorted(features.items()))


def natural_number(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError(f"Mutation report lacks a valid {label} number")
    return int(value)


def read_report(path, classes, catalogue, sources):
    document = ET.parse(path).getroot()
    if document.tag != "mutations" or document.get("partial") not in ("true", "false"):
        raise ValueError("Mutation report is not a complete PIT run")
    if not classes or not catalogue or not sources or not list(document):
        raise ValueError("Mutation report or authoritative target inventory is empty")
    counts = Counter({status: 0 for status in STATUSES})
    class_counts = {name: 0 for name in sorted(classes)}
    mutator_counts = {name: 0 for name in sorted(catalogue)}
    outcomes = []
    identities = set()
    for entry in document:
        if entry.tag != "mutation":
            raise ValueError("Mutation report contains an unexpected record")
        status = entry.get("status")
        if status not in counts:
            raise ValueError("Mutation report contains an unknown outcome")
        tests = natural_number(entry.get("numberOfTestsRun"), "test-count")

        def text(name):
            nodes = entry.findall(name)
            if len(nodes) != 1 or not nodes[0].text:
                raise ValueError("Mutation report lacks a unique required identity field")
            return nodes[0].text

        target, source, mutator = text("mutatedClass"), text("sourceFile"), text("mutator")
        if target not in classes or source not in sources or mutator not in catalogue:
            raise ValueError("Mutation report contains a class, source or operator outside the complete target")
        line = natural_number(text("lineNumber"), "line")
        indexes = [natural_number(node.text, "instruction-index") for node in entry.findall("indexes/index")]
        if not indexes or len(indexes) != len(set(indexes)):
            raise ValueError("Mutation report lacks unique instruction indexes")
        identity = (target, text("mutatedMethod"), text("methodDescription"), mutator, tuple(indexes))
        if identity in identities:
            raise ValueError("Mutation report repeats a mutant identity")
        identities.add(identity)
        detected = entry.get("detected")
        killing_test = entry.findtext("killingTest") or ""
        if detected not in ("true", "false"):
            raise ValueError("Mutation report lacks its detection flag")
        money_test = re.match(
            r"(com\.pennilogic\.money\.[A-Za-z0-9_$]+)\."
            r"\[engine:junit-jupiter\]/\[class:\1\]/\[method:",
            killing_test,
        )
        if status == "KILLED" and (not tests or detected != "true" or not money_test):
            raise ValueError("Killed mutation lacks an executed Money test")
        if status in ("SURVIVED", "NO_COVERAGE") and (detected != "false" or killing_test):
            raise ValueError("Undetected mutation has inconsistent killing evidence")
        if (status == "SURVIVED" and not tests) or (status == "NO_COVERAGE" and tests):
            raise ValueError("Mutation report test count disagrees with its outcome")
        counts[status] += 1
        class_counts[target] += 1
        mutator_counts[mutator] += 1
        outcomes.append({
            "id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            "class": target, "method": identity[1], "descriptor": identity[2],
            "line": line, "mutator": mutator, "indexes": indexes, "status": status,
            "tests_run": tests,
        })
    total = sum(counts.values())
    return {
        "total": total, "counts": dict(counts),
        "partial_line_coverage": document.get("partial") == "true",
        "score": {"numerator": counts["KILLED"], "denominator": total},
        "score_percent": round(counts["KILLED"] * 100 / total, 6),
        "classes": class_counts, "mutators": mutator_counts,
        "outcomes": sorted(outcomes, key=lambda row: row["id"]),
    }


def check_native_summary(path, expected, result):
    if expected is None or file_record(path) != expected:
        raise ValueError("Mutation native engine log lacks its declared byte/hash binding")
    summaries = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"^>> Generated\b.*\bmutations\b", line)
    ]
    if len(summaries) != 1:
        raise ValueError("PIT console lacks a unique complete mutation summary")
    summary = re.fullmatch(r">> Generated (\S+) mutations Killed (\S+) \([0-9]+%\)", summaries[0])
    if summary is None:
        raise ValueError("PIT console lacks complete generated/killed numbers")
    generated = natural_number(summary[1], "native generated")
    killed = natural_number(summary[2], "native killed")
    if (generated, killed) != (result["total"], result["counts"]["KILLED"]):
        raise ValueError("PIT console counts disagree with its complete XML catalogue")


def enforce(result, floors):
    floor = floors.get("MUTATION")
    if type(floor) is not int or not 0 < floor <= 100:
        raise ValueError("Accepted money-package strategy lacks a numeric mutation floor")
    total, counts = result["total"], result["counts"]
    if (
        type(total) is not int or total <= 0 or set(counts) != set(STATUSES)
        or any(type(value) is not int or value < 0 for value in counts.values())
        or sum(counts.values()) != total
    ):
        raise ValueError("Mutation result lacks a complete numeric denominator")
    if any(result["counts"][name] for name in ERROR_STATUSES):
        raise ValueError("PIT has unresolved errors or incomplete mutations")
    if Fraction(result["counts"]["KILLED"], result["total"]) < Fraction(floor, 100):
        raise ValueError("Authoritative Money mutation score below the accepted floor")


def java_argument_text(arguments):
    if any("\n" in value or "\r" in value for value in arguments):
        raise ValueError("Invalid newline in mutation engine argument")
    return "\n".join('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in arguments) + "\n"


def java_arguments(path, arguments):
    path.write_text(java_argument_text(arguments), encoding="utf-8", newline="\n")


def execute(input_file=ROOT / INPUT_FILE):
    started = time.monotonic()
    provider = quality.money_provider_module()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    directory = provider.owned_target(ROOT, REPORTS / run_id)
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "schema_version": SCHEMA_VERSION, "event": "money_package_mutation", "status": "started",
        "feature_selection": list(FEATURE_SELECTION),
        "run_id": run_id, "strategy_package": "api.money",
        "commands": [], "result": None,
    }
    write_record(directory / "run.json", record)
    write_record(ROOT / REPORTS / "latest.json", {"run_id": run_id})

    try:
        provider.verify_outputs()
        floors, strategy_hash = quality.money_policy(provider)
        budget = quality.money_budget(provider)
        config = json.loads(input_file.read_text(encoding="utf-8"))
        snapshot = input_snapshot(config)
        classes = {name.replace("/", ".") for name in quality.money_class_inventory(provider)}
        sources = {Path(name).name for name in provider.OUTPUT_HASHES}
        record.update({
            "contracts_source_ref": provider.SOURCE_REF, "docs_source_ref": provider.DOCS_REF,
            "strategy_sha256": strategy_hash, "floor_percent": floors, "budget": budget,
            "engine": engine_versions(config), "selection": "ALL",
            "target_classes": sorted(classes), "target_sources": sorted(sources),
            "exclusions": {"classes": [], "methods": [], "tests": [], "mutators": []},
            "config": config, "inputs": snapshot, "test_evidence": quality.money_test_metrics(),
            "tool_limits": [
                "PIT's native bytecode eligibility and listed default compiler/equivalence filters still apply.",
                "FKOTLIN filters .kt mutations with native line zero, including meaningful generated bodies.",
                "The Kotlin heuristic is incomplete and can have false positives; filtering is not kill or equivalence credit.",
                "Static initializers and non-lambda synthetic methods are not mutated by PIT.",
                "Registry fixture assertions are not static-initializer mutation kills.",
                "Zero-mutant classes and operators remain in the catalogue; no equivalence is waived locally.",
                "Only KILLED earns credit; timeouts, no coverage and errors never earn credit.",
            ],
        })
        write_record(directory / "run.json", record)

        def invoke(arguments, name):
            argfile = directory / f"{name}.args"
            java_arguments(argfile, arguments)
            command = [config["java"], "@" + str(argfile)]
            process_budget = quality.script_module("process_budget")
            entry = {
                "argv": command, "process_argv": process_budget.launch_command(command),
                "java_arguments": arguments, "exit_code": None,
            }
            record["commands"].append(entry)
            write_record(directory / "run.json", record)
            step_start = time.monotonic()
            try:
                result = process_budget.run(
                    command, ROOT, budget["enforced_seconds"] - (time.monotonic() - started), capture=True,
                )
            except TimeoutError as error:
                entry["budget_exceeded"] = True
                (directory / f"{name}.log").write_text(error.output or "", encoding="utf-8", newline="\n")
                raise
            finally:
                entry["elapsed_seconds"] = round(time.monotonic() - step_start, 6)
            entry["exit_code"] = result.returncode
            log = directory / f"{name}.log"
            log.write_text(result.stdout, encoding="utf-8", newline="\n")
            record.setdefault("outputs", {})[log.name] = file_record(log)
            return result

        tool_path = os.pathsep.join(config["tool_classpath"])
        catalogue_run = invoke(["-cp", tool_path, str(ROOT / "scripts/PitCatalogue.java")], "catalogue")
        if catalogue_run.returncode:
            raise ValueError("PIT catalogue process failed; see the preserved attempt")
        catalogue, features = read_catalogue(catalogue_run.stdout)
        record["catalogue"] = catalogue
        record["features"] = features
        engine = invoke([
            "-cp", tool_path, "org.pitest.mutationtest.commandline.MutationCoverageReport",
            "--reportDir", str(directory),
            "--targetClasses", quality.MONEY_PACKAGE.replace("/", ".") + ".*",
            "--targetTests", TEST_PACKAGE + "*",
            "--sourceDirs", str(ROOT / provider.BUNDLE / "kotlin/src/main/kotlin"),
            "--mutableCodePaths", str(ROOT / quality.MONEY_CLASSES),
            "--classPath", ",".join(config["test_classpath"]),
            "--includeLaunchClasspath=false", "--mutators=ALL",
            "--features=" + ",".join(FEATURE_SELECTION),
            "--threads=2", "--timeoutFactor=1.25", "--timeoutConst=4000",
            "--jvmArgs", ",".join([
                "-Xmx512m", "-Duser.timezone=UTC",
                "-Dpennilogic.money.source=" + str(ROOT / provider.BUNDLE / "source"),
            ]),
            "--outputFormats=XML", "--timestampedReports=false", "--failWhenNoMutations=true",
            "--mutationThreshold=" + str(floors["MUTATION"]),
            "--coverageThreshold=" + str(floors["LINE"]),
        ], "engine")
        report = directory / "mutations.xml"
        record["report"] = file_record(report)
        record["result"] = read_report(report, classes, catalogue, sources)
        check_native_summary(directory / "engine.log", record["outputs"].get("engine.log"), record["result"])
        if input_snapshot(config) != snapshot:
            raise ValueError("Mutation inputs changed during execution")
        enforce(record["result"], floors)
        if engine.returncode:
            raise ValueError("PIT process failed despite its report; see the preserved attempt")
        if time.monotonic() - started > budget["enforced_seconds"]:
            raise TimeoutError("Money mutation harness exceeded its enforced elapsed budget")
        record["status"] = "passed"
    except (ValueError, OSError, TimeoutError, ET.ParseError, KeyError, TypeError) as error:
        record["status"] = "failed"
        record["failure"] = str(error) if isinstance(error, (ValueError, TimeoutError)) else type(error).__name__
        raise
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 6)
        record["outputs"] = {
            path.name: file_record(path) for path in sorted(directory.iterdir()) if path.name != "run.json"
        }
        write_record(directory / "run.json", record)
        result = record["result"]
        print(json.dumps({
            "event": record["event"], "run_id": run_id, "status": record["status"],
            "feature_selection": record["feature_selection"],
            "engine": record.get("engine"), "strategy_package": "api.money",
            "floor_percent": record.get("floor_percent", {}).get("MUTATION"),
            "elapsed_seconds": record["elapsed_seconds"], "failure": record.get("failure"),
            "budget": record.get("budget"), "counts": result["counts"] if result else None,
            "total": result["total"] if result else None,
            "score": result["score"] if result else None,
            "score_percent": result["score_percent"] if result else None,
            "report": str((directory / "run.json").relative_to(ROOT)),
        }))
    return record


def check_latest():
    provider = quality.money_provider_module()
    provider.verify_outputs()
    floors, strategy_hash = quality.money_policy(provider)
    budget = quality.money_budget(provider)
    latest = json.loads(provider.safe_path(ROOT / REPORTS, "latest.json").read_text(encoding="utf-8"))
    run_id = latest.get("run_id", "")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", run_id):
        raise ValueError("Mutation run reference is invalid")
    directory = provider.safe_path(ROOT / REPORTS, run_id)
    record = quality.money_execution_event(provider.safe_path(directory, "run.json").read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA_VERSION or record.get("feature_selection") != list(FEATURE_SELECTION):
        raise ValueError("Mutation attempt uses a different native feature measurement scope")
    elapsed = record["elapsed_seconds"]
    if (
        record["status"] != "passed" or record["strategy_sha256"] != strategy_hash
        or record["floor_percent"] != floors or record["budget"] != budget
        or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed) or not 0 < elapsed <= budget["enforced_seconds"]
        or record["inputs"] != input_snapshot(record["config"])
    ):
        raise ValueError("Latest mutation attempt failed, exceeded budget or has stale inputs")
    test_evidence = quality.money_test_metrics()
    if json.dumps(record.get("test_evidence"), sort_keys=True) != json.dumps(test_evidence, sort_keys=True):
        raise ValueError("Mutation test evidence differs from executed Money comparisons")
    for name, expected in record["outputs"].items():
        if Path(name).name != name or file_record(provider.safe_path(directory, name)) != expected:
            raise ValueError("Mutation attempt output changed after execution")
    catalogue, features = read_catalogue(provider.safe_path(directory, "catalogue.log").read_text(encoding="utf-8"))
    classes = sorted(name.replace("/", ".") for name in quality.money_class_inventory(provider))
    if (
        record["target_classes"] != classes or record["catalogue"] != catalogue or record["features"] != features
        or len(record["commands"]) != 2 or any(command["exit_code"] != 0 for command in record["commands"])
    ):
        raise ValueError("Mutation attempt is not the complete successful engine invocation")
    arguments = record["commands"][1]["java_arguments"]
    if [value for value in arguments if value == "--features" or value.startswith("--features=")] != [
        "--features=" + ",".join(FEATURE_SELECTION),
    ]:
        raise ValueError("Mutation engine feature selection differs from the required measurement scope")
    for name, command in zip(("catalogue", "engine"), record["commands"]):
        argfile = provider.safe_path(directory, f"{name}.args")
        if (
            argfile.name not in record["outputs"]
            or command["argv"] != [record["config"]["java"], "@" + str(argfile)]
            or argfile.read_bytes() != java_argument_text(command["java_arguments"]).encode("utf-8")
        ):
            raise ValueError("Mutation native argument file differs from its recorded invocation")
    report = provider.safe_path(directory, "mutations.xml")
    if file_record(report) != record["report"]:
        raise ValueError("Mutation output changed after execution")
    result = read_report(report, set(record["target_classes"]), record["catalogue"], set(record["target_sources"]))
    if result != record["result"]:
        raise ValueError("Mutation summary does not match its engine report")
    check_native_summary(provider.safe_path(directory, "engine.log"), record["outputs"].get("engine.log"), result)
    enforce(result, floors)
    print(json.dumps({
        "event": "money_mutation_verified", "run_id": run_id,
        "counts": result["counts"], "score": result["score"],
        "primitive_model": test_evidence["primitive_model"],
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--input-file", type=Path, default=ROOT / INPUT_FILE)
    args = parser.parse_args()
    try:
        if args.command == "run":
            execute(args.input_file)
        else:
            check_latest()
    except (ValueError, OSError, TimeoutError, ET.ParseError, KeyError, TypeError):
        print("Money mutation gate failed; no failed, incomplete or unmeasured attempt qualifies.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

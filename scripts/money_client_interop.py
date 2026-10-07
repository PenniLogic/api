"""API-owned, test-only backend -> emitted client model -> backend Money gate."""

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).absolute().parents[1]
AREA = Path("build") / "money-client-interop"
FIXTURES = Path("scripts") / "tests" / "fixtures" / "money_client_interop"
BACKEND = Path("src/test/kotlin/com/pennilogic/money/MoneyClientInteropBridge.kt")
SOURCE = "aa8d90cb98cec9b6dd08c91b3a4d869e47362662"
SOURCE_TREE = "da0d17d9deaee9c049776d16c1511c5840fa16fe"
CATALOG_SHA256 = "da45087cec04f9cdfee7cf880533b2e0527159a535c872f55f8d27c8b9e33016"
CORPUS_SHA256 = "22ed8a8ff3204a8962c10c8fcfada7d20dd6ed147fc2a07f98edef827ea28efe"
INVALID_SHA256 = "3c95a95c290fd1aad5d58300c301ab3c8c6b3b2883a95fbb586b58336ab1cf1d"
CANONICAL_SHA256 = "6d3172f611bc69f7d529912f68539a1e66101a1c188710ff2400ceab4fbb1ef6"
WIRE_SCHEMA = "pennilogic.api-money-client-wire/1"
REJECTIONS_SCHEMA = "pennilogic.api-money-client-rejections/1"
REPORT_SCHEMA = "pennilogic.api-money-client-interop/1"
COUNT = 10030
INVALID_COUNT = 41
LANGUAGES = ("kotlin", "typescript", "python")
MODELS = {
    "kotlin": "src/main/kotlin/com/pennilogic/contracts/models/SyntheticEnvelope.kt",
    "typescript": "src/models/SyntheticEnvelope.ts",
    "python": "pennilogic_contracts/models/synthetic_envelope.py",
}
MAX_DOCUMENT = 4 * 1024 * 1024
SECONDS = 600


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Source bindings must govern execution, not an unbound import cache beside them.
    exec(compile(read_file(path), str(path), "exec", dont_inherit=True), module.__dict__)
    return module


class InteropError(ValueError):
    """Static causes only; never include wire data, tool output or exception messages."""


def require(condition, code):
    if not condition:
        raise InteropError(code)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def owned(root, relative):
    return sources.owned_path(root, relative)


def read_file(path, limit=MAX_DOCUMENT):
    for component in (path, *path.parents):
        require(not component.is_symlink() and not component.is_junction(), "linked-input")
    metadata = path.stat(follow_symlinks=False)
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1, "input-kind")
    require(metadata.st_size <= limit, "document-size")
    with path.open("rb") as stream:
        content = stream.read(metadata.st_size + 1)
    require(len(content) == metadata.st_size, "input-changed")
    return content


sources = module_at("interop_source_primitives", ROOT / "scripts/materialize_money_sources.py")
process_budget = module_at("interop_process_budget", ROOT / "scripts/process_budget.py")


def read_json(path):
    return sources.json_document(read_file(path))


def write_file(root, relative, content, replace=False):
    path = owned(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(replace, "output-exists")
        read_file(path)
    with path.open("wb" if replace else "xb") as stream:
        stream.write(content)
    return path


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def write_json(root, relative, value, replace=False):
    return write_file(root, relative, json_bytes(value), replace)


def binding(path):
    content = read_file(path, 256 * 1024 * 1024)
    return {"bytes": len(content), "sha256": digest(content)}


def inventory(root, *, ignored=(), limit=4096):
    require(root.is_dir(), "inventory-missing")
    files, pending, count = {}, [root], 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                require(count <= limit, "inventory-limit")
                path = owned(root, Path(entry.path).relative_to(root))
                relative = path.relative_to(root).as_posix()
                if entry.is_dir(follow_symlinks=False):
                    if relative not in ignored:
                        pending.append(path)
                else:
                    files[relative] = binding(path)
    return dict(sorted(files.items()))


def catalog(root=ROOT):
    content = read_file(owned(root, FIXTURES / "source-inputs.json")).replace(b"\r\n", b"\n")
    require(digest(content) == CATALOG_SHA256, "catalog-binding")
    data = sources.json_document(content)
    require(set(data) == {"schema", "consumer", "sources"}, "catalog-shape")
    require(data["schema"] == "pennilogic.api-money-client-inputs/1", "catalog-version")
    require(data["consumer"] == {
        "repository": "PenniLogic/api", "repository_id": 1394134582, "organization_id": 335295566,
    }, "catalog-consumer")
    require(type(data["sources"]) is list and len(data["sources"]) == 1, "catalog-source")
    source = data["sources"][0]
    require(source["repository"] == "PenniLogic/contracts" and source["repository_id"] == 1394134505
            and source["commit"] == SOURCE and source["tree"] == SOURCE_TREE
            and source["snapshot"] == "contracts-" + SOURCE, "catalog-source")
    require(type(source["files"]) is list and len(source["files"]) == 39, "catalog-count")
    names = []
    for entry in source["files"]:
        require(set(entry) == {"path", "bytes", "sha256", "git_blob", "mode"}, "catalog-entry")
        # The accepted npm configuration and executable wrapper are the only extensions to the
        # older provider catalog's path/mode subset. Its bytes/blob checks remain unchanged.
        if entry["path"] != ".npmrc":
            sources.relative_path(entry["path"])
        require(entry["mode"] == ("100755" if entry["path"] == "smoke/kotlin/gradlew" else "100644"), "catalog-mode")
        sources.sha(entry["git_blob"])
        require(type(entry["bytes"]) is int and 0 < entry["bytes"] <= sources.MAX_FILE_BYTES, "catalog-size")
        require(type(entry["sha256"]) is str and sources.DIGEST.fullmatch(entry["sha256"]), "catalog-digest")
        names.append(entry["path"])
    require(names == sorted(set(names)) and len({name.lower() for name in names}) == 39, "catalog-order")
    return data


def source_bindings(data):
    return {entry["path"]: entry for entry in data["sources"][0]["files"]}


def verify_sources(root=ROOT):
    data = catalog(root)
    sources.verify_directory(root, AREA / "inputs", source_bindings(data))
    return data


def verify_acquisition(root):
    value = read_json(owned(root, AREA / "acquisition.json"))
    common = {"schema", "source_ref", "source_tree", "catalog_sha256", "inputs", "mode", "requests", "reused_provider_inputs"}
    require(type(value) is dict and value.get("mode") in {
        "native-public-git-object-get", "offline-hash-and-git-blob-verified",
    }, "acquisition-mode")
    native = value["mode"] == "native-public-git-object-get"
    require(set(value) == common | ({"response_bytes", "elapsed_seconds"} if native else set()), "acquisition-shape")
    require(value["schema"] == "pennilogic.api-money-client-acquisition/1"
            and value["source_ref"] == SOURCE and value["source_tree"] == SOURCE_TREE
            and value["catalog_sha256"] == CATALOG_SHA256, "acquisition-source")
    require(all(type(value[key]) is int and value[key] == expected for key, expected in {
        "inputs": 39, "requests": 35 if native else 0, "reused_provider_inputs": 10 if native else 0,
    }.items()), "acquisition-count")
    if native:
        require(type(value["response_bytes"]) is int and 0 < value["response_bytes"] <= 2 * sources.MAX_TOTAL_BYTES,
                "acquisition-size")
        require(type(value["elapsed_seconds"]) in (int, float) and math.isfinite(value["elapsed_seconds"])
                and 0 < value["elapsed_seconds"] < sources.DEADLINE_SECONDS, "acquisition-budget")


def acquire(data, root=ROOT):
    """Reuse the native provider's verified files and bounded, non-redirecting Git-object reader."""
    sources.verify_inputs(root)
    sources.verify_provider(root)
    accepted = sources.input_bindings(sources.validate_catalog(sources.CATALOG))
    contents, missing = {}, []
    for name, entry in source_bindings(data).items():
        shared = "contracts-" + SOURCE + "/" + name
        if shared in accepted:
            content = read_file(owned(root, sources.INPUTS / shared))
            sources.check_bytes(content, entry)
            contents[name] = content
        else:
            missing.append(entry)
    require(len(contents) == 10 and len(missing) == 29, "shared-input-inventory")
    overall, requests, response_bytes = sources.Budget(), 0, 0
    # Each independent native budget admits at most 32 GETs/4 MiB/180 seconds. Two
    # fixed batches suffice; an outer native deadline bounds the complete acquisition.
    for offset in range(0, len(missing), 28):
        overall.remaining()
        part = {**data["sources"][0], "files": missing[offset:offset + 28]}
        client = sources.ReadOnlyClient({"sources": [part]})
        snapshot = sources.GitSnapshot(client, part, 335295566)
        require(snapshot.root == SOURCE_TREE, "source-tree-binding")
        for entry in part["files"]:
            overall.remaining()
            contents[entry["path"]] = snapshot.blob(entry)
        requests += client.budget.requests
        response_bytes += client.budget.bytes
    overall.remaining()
    return contents, {
        "mode": "native-public-git-object-get", "requests": requests, "response_bytes": response_bytes,
        "reused_provider_inputs": 10, "elapsed_seconds": round(time.monotonic() - overall.started, 3),
    }


def prepare(root=ROOT, offline_source=None):
    data = catalog(root)
    target = owned(root, AREA / "inputs")
    if target.exists():
        verify_sources(root)
        verify_acquisition(root)
        return {"event": "money_client_inputs", "status": "verified_existing", "inputs": 39, "requests": 0}
    if offline_source is None:
        contents, provenance = acquire(data, root)
    else:
        offline_source = offline_source.absolute()
        sources.verify_directory(offline_source.parent, Path(offline_source.name), source_bindings(data))
        contents = {name: read_file(offline_source / name) for name in source_bindings(data)}
        provenance = {"mode": "offline-hash-and-git-blob-verified", "requests": 0, "reused_provider_inputs": 0}
    require(set(contents) == set(source_bindings(data)), "source-inventory")
    stage = AREA / ("input-stage-" + uuid.uuid4().hex)
    for name, content in contents.items():
        sources.check_bytes(content, source_bindings(data)[name])
        write_file(root, stage / name, content)
    sources.verify_directory(root, stage, source_bindings(data))
    require(not target.exists(), "source-ownership-conflict")
    owned(root, stage).rename(target)
    verify_sources(root)
    receipt = {
        "schema": "pennilogic.api-money-client-acquisition/1", "source_ref": SOURCE,
        "source_tree": SOURCE_TREE, "catalog_sha256": CATALOG_SHA256, "inputs": 39, **provenance,
    }
    write_json(root, AREA / "acquisition.json", receipt)
    return {"event": "money_client_inputs", "status": "prepared", **receipt}


def producer(root=ROOT):
    data = verify_sources(root)
    directory = owned(root, AREA / "producer")
    expected = {name: {"bytes": entry["bytes"], "sha256": entry["sha256"]}
                for name, entry in source_bindings(data).items()}
    if not directory.exists():
        for name in expected:
            write_file(root, AREA / "producer" / name, read_file(owned(root, AREA / "inputs" / name)))
    actual = inventory(directory, ignored=("build", ".toolchain", "node_modules"))
    require(actual == expected, "producer-source-inventory")
    return directory


def corpus(root=ROOT):
    content = read_file(owned(root, AREA / "inputs/spec/fixtures/money-roundtrip-generated.v1.json"))
    require(digest(content) == CORPUS_SHA256, "corpus-source")
    data = sources.json_document(content)
    require(data["schema_version"] == 1 and type(data["schema_version"]) is int, "corpus-version")
    require((data["generated_count"], data["boundary_count"], data["count"]) == (10000, 30, COUNT), "corpus-count")
    require(type(data["values"]) is list and len(data["values"]) == COUNT, "corpus-count")
    require(data["round_trip_sha256"] == CANONICAL_SHA256, "corpus-digest")
    require({row["wire"]["currency"] for row in data["values"]} == {"INR", "JPY", "KWD"}, "corpus-currencies")
    return data


def envelope(wire):
    return {"total": wire, "recorded_at": "2026-09-30T04:52:08.439Z", "booked_on": "2026-09-30"}


def case_id(index):
    return f"case-{index:05d}"


def transport(run_id, values, fixture_sha256=CORPUS_SHA256):
    return {
        "schema": WIRE_SCHEMA, "run_id": run_id, "source_ref": SOURCE, "fixture_sha256": fixture_sha256,
        "case_count": len(values), "cases": [{"id": case_id(index), "envelope": envelope(row["wire"])}
                                           for index, row in enumerate(values)],
    }


def validate_transport(data, expected, *, compare_values=True, rejections=False):
    require(type(data) is dict and set(data) == set(expected), "transport-shape")
    for key in ("schema", "run_id", "source_ref", "fixture_sha256", "case_count"):
        wanted = REJECTIONS_SCHEMA if rejections and key == "schema" else expected[key]
        require(type(data[key]) is type(wanted) and data[key] == wanted, "transport-" + key.replace("_", "-"))
    rows = data["cases"]
    require(type(rows) is list and len(rows) == expected["case_count"] > 0, "transport-count")
    for index, row in enumerate(rows):
        keys = {"id", "status"} if rejections else {"id", "envelope"}
        require(type(row) is dict and set(row) == keys, "case-shape")
        require(row["id"] == case_id(index), "case-order")
        if rejections:
            require(row["status"] == "rejected", "case-unexecuted")
        else:
            require(type(row["envelope"]) is dict and set(row["envelope"]) == {"total", "recorded_at", "booked_on"}, "envelope-shape")
            if compare_values:
                require(row["envelope"] == expected["cases"][index]["envelope"], "wire-disagreement")
    return data


def scratch_spec(root, directory):
    original = read_file(owned(root, AREA / "inputs/spec/openapi.yaml")).replace(b"\r\n", b"\n")
    fragment = read_file(owned(root, FIXTURES / "envelope.yaml")).replace(b"\r\n", b"\n")
    anchor = b"  parameters:\n    IdempotencyKey:"
    require(original.count(anchor) == 1 and b"SyntheticEnvelope:" not in original, "scratch-anchor")
    return write_file(root, directory / "spec/openapi.yaml", original.replace(anchor, fragment + anchor))


def generated(root, directory, specification, language, pl, *, scratch=True):
    base = owned(root, directory / "generated" / language)
    manifest = read_json(base / "contracts-manifest.json")
    require(set(manifest) == {
        "schema_version", "target", "spec_version", "spec_sha256", "currency_registry_sha256",
        "generator", "runtime_sha256", "file_count", "tree_sha256", "files",
    }, "generated-manifest-shape")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and manifest["target"] == language and manifest["spec_version"] == "0.1.0", "generated-version")
    require(manifest["spec_sha256"] == digest(read_file(specification)), "generated-spec")
    inputs = owned(root, AREA / "inputs")
    pins = read_json(inputs / "toolchain/versions.json")
    config = read_json(inputs / "generator" / (language + ".json"))
    templates = inputs / "generator/templates" / language
    expected_generator = {
        "name": "openapi-generator-cli", "version": pins["openapi_generator"]["version"],
        "jar_sha256": pins["openapi_generator"]["sha256"], "generator_name": config["generatorName"],
        "config": f"generator/{language}.json",
        "config_sha256": digest(read_file(inputs / "generator" / (language + ".json"))),
        "ignore_override_sha256": digest(read_file(inputs / "generator/openapi-generator-ignore")),
        "template_override_sha256": pl.tree_hash(templates)[0] if language != "typescript" else None,
    }
    require(manifest["generator"] == expected_generator, "generated-toolchain")
    require(manifest["currency_registry_sha256"] == digest(read_file(inputs / "spec/currency-registry.v1.json"))
            and manifest["runtime_sha256"] == pl.tree_hash(inputs / "runtime" / language)[0], "generated-seams")
    actual = inventory(base)
    del actual["contracts-manifest.json"]
    entries = [{"path": name, "sha256": entry["sha256"]} for name, entry in actual.items()]
    require(manifest["files"] == entries and type(manifest["file_count"]) is int
            and manifest["file_count"] == len(entries) > 0, "generated-inventory")
    require(manifest["tree_sha256"] == pl.combined_digest({name: entry["sha256"] for name, entry in actual.items()}),
            "generated-tree")
    if scratch:
        require(MODELS[language] in actual, "generated-model-missing")
    else:
        golden = read_json(inputs / "generator/golden.json")[language]
        require(MODELS[language] not in actual and manifest["tree_sha256"] == golden["tree_sha256"]
                and {name: entry["sha256"] for name, entry in actual.items()} == golden["files"], "default-generation-tampered")
    return {"target": language, "model": MODELS[language] if scratch else None,
            "model_sha256": actual[MODELS[language]]["sha256"] if scratch else None,
            "manifest_sha256": binding(base / "contracts-manifest.json")["sha256"], "tree_sha256": manifest["tree_sha256"]}


def verify_default_outputs(root, source, pl):
    directory = AREA / "producer/build"
    base = owned(root, directory)
    if not base.exists():
        return
    require({path.name for path in base.iterdir()} <= {"generated", "generated-verify"}, "default-output-inventory")
    second = owned(root, directory / "generated-verify")
    require(not second.exists() or (second.is_dir() and not list(second.iterdir())), "default-output-partial")
    first = owned(root, directory / "generated")
    if first.exists():
        require(first.is_dir() and {path.name for path in first.iterdir()} == set(LANGUAGES), "default-output-partial")
        for language in LANGUAGES:
            generated(root, directory, source / "spec/openapi.yaml", language, pl, scratch=False)


def backend_launch(root=ROOT):
    launch = read_json(owned(root, AREA / "backend-launch.json"))
    require(type(launch) is dict and set(launch) == {"java", "classpath"}, "backend-launch-shape")
    require(type(launch["java"]) is str and Path(launch["java"]).is_absolute()
            and Path(launch["java"]).name in {"java", "java.exe"} and Path(launch["java"]).is_file(), "backend-java")
    classpath(launch["classpath"])
    return launch


def classpath(paths):
    require(type(paths) is list and paths and all(type(path) is str and Path(path).is_absolute() for path in paths), "classpath")
    require(len(paths) == len(set(paths)) and len(paths) <= 256, "classpath")
    require(all(Path(path).exists() for path in paths), "classpath-missing")
    return os.pathsep.join(paths)


def classpath_bindings(paths):
    classpath(paths)
    return {path: inventory(Path(path)) if Path(path).is_dir() else binding(Path(path)) for path in paths}


def api_inputs(root=ROOT):
    verify_acquisition(root)
    names = {
        "build.gradle.kts", "gradle.lockfile", "gradle/verification-metadata.xml", BACKEND.as_posix(),
        "scripts/money_client_interop.py", "scripts/materialize_money_sources.py", "scripts/process_budget.py",
        (AREA / "backend-launch.json").as_posix(), (AREA / "acquisition.json").as_posix(),
    }
    names.update((FIXTURES / name).as_posix() for name in inventory(owned(root, FIXTURES)))
    names.update((AREA / "inputs" / name).as_posix() for name in source_bindings(catalog(root)))
    names.update((sources.PROVIDER / name).as_posix() for name in sources.provider_bindings(sources.CATALOG))
    return {name: binding(owned(root, Path(name))) for name in sorted(names)}


def receipt(target, operation, count, run_id):
    value = {
        "event": "money_client_backend" if target == "backend" else "money_client_conversion",
        "status": "passed", "operation": operation, "case_count": count, "run_id": run_id,
    }
    if target == "backend":
        value["canonical_sha256"] = None if operation == "reject" else CANONICAL_SHA256
    else:
        value["target"] = target
    return value


DISAGREEMENT = {"event": "money_client_backend", "status": "refused", "code": "wire-disagreement", "case_id": "case-00000"}


class Runner:
    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        self.started, self.records = time.monotonic(), []

    def call(self, label, argv, *, cwd=None, expected=0, protocol=None, readiness=False):
        require(label not in {record["label"] for record in self.records}, "command-identity")
        argv = [str(arg) for arg in argv]
        start = time.monotonic()
        environment = dict(os.environ)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            environment.pop(name, None)
        try:
            result = subprocess.run(
                argv, cwd=cwd or self.root, env=environment, capture_output=True,
                timeout=max(0.001, SECONDS - (start - self.started)), check=False,
            )
            executed, code, stdout, stderr = True, result.returncode, result.stdout, result.stderr
        except FileNotFoundError:
            executed, code, stdout, stderr = False, None, b"", b""
        except subprocess.TimeoutExpired:
            raise InteropError("process-budget") from None
        record = {
            "label": label, "argv": argv, "cwd": str(cwd or self.root), "executed": executed,
            "exit_code": code, "expected_exit": expected, "readiness_probe": readiness,
            "elapsed_seconds": round(time.monotonic() - start, 6),
        }
        safe_protocol = protocol is None or (stdout == b"" if protocol[0] is None else self.matches(stdout, protocol[0]))
        if protocol is not None:
            safe_protocol = safe_protocol and (stderr == b"" if protocol[1] is None else self.matches(stderr, protocol[1]))
        for name, content in (("stdout", stdout), ("stderr", stderr)):
            stream = {"bytes": len(content), "sha256": digest(content), "path": None}
            # Unexpected runtime output is hash-only: even a broken bridge cannot log Money.
            if safe_protocol and len(content) <= MAX_DOCUMENT:
                path = self.directory / "commands" / f"{len(self.records):02d}-{label}.{name}.txt"
                write_file(self.root, path, content)
                stream["path"] = path.as_posix()
            record[name] = stream
        self.records.append(record)
        write_json(self.root, self.directory / "commands" / f"{len(self.records) - 1:02d}-{label}.json", record)
        require(safe_protocol, "command-protocol")
        require(len(stdout) <= MAX_DOCUMENT and len(stderr) <= MAX_DOCUMENT, "command-output-size")
        if not readiness:
            require(executed and code == expected, "command-failed-" + label)
        return code, stdout, stderr

    @staticmethod
    def matches(content, expected):
        try:
            return json_bytes(sources.json_document(content)) == json_bytes(expected)
        except sources.MaterializationError:
            return False


def prepare_tools(root, runner, producer_dir):
    pins = read_json(producer_dir / "toolchain/versions.json")
    tools = AREA / "producer/.toolchain"
    jar = owned(root, tools / f"openapi-generator-cli-{pins['openapi_generator']['version']}.jar")
    if jar.exists():
        require(binding(jar)["sha256"] == pins["openapi_generator"]["sha256"], "generator-cache-tampered")
    suffix = ".exe" if os.name == "nt" else ""
    binary = owned(root, tools / f"oasdiff-{pins['oasdiff']['version']}{suffix}")
    marker = owned(root, tools / (binary.name + ".verified.json"))
    require(binary.exists() == marker.exists(), "tool-cache-partial")
    if binary.exists():
        record = read_json(marker)
        platform = "windows_amd64" if os.name == "nt" else "linux_amd64"
        require(record == {"binary_sha256": binding(binary)["sha256"],
                           "tarball_sha256": pins["oasdiff"]["assets"][platform]["sha256"]}, "tool-cache-tampered")
    probe = runner.call("toolchain-probe", [sys.executable, "-I", "-B", producer_dir / "scripts/toolchain.py", "verify"], readiness=True)
    require(probe[0] == 0 or not (jar.exists() and binary.exists()), "toolchain-refused")
    return pins


def runtime_dependencies(root, directory, runner, producer_dir, pins):
    interpreter = owned(root, AREA / "venv" / ("Scripts" if os.name == "nt" else "bin")) / ("python.exe" if os.name == "nt" else "python")
    if interpreter.is_symlink():
        require(os.name != "nt" and interpreter.resolve() == Path(sys.executable).resolve(), "python-runtime-link")
    require(not interpreter.is_junction(), "python-runtime-link")
    requirements = producer_dir / "smoke/python/requirements.txt"
    versions = re.findall(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+)", read_file(requirements).decode("utf-8"), re.MULTILINE)
    require(versions and len(dict(versions)) == len(versions), "python-lock")
    probe_code = (
        "import importlib.metadata as m,json,sys\n"
        "missing=[]; wrong=[]\n"
        "for name,version in json.loads(sys.argv[1]):\n"
        " try:\n"
        "  if m.version(name)!=version: wrong.append(name)\n"
        " except m.PackageNotFoundError: missing.append(name)\n"
        "print(json.dumps({'missing':len(missing),'wrong':len(wrong)}))\n"
        "sys.exit(1 if wrong else (3 if missing else 0))\n"
    )
    command = [interpreter, "-I", "-B", "-c", probe_code, json.dumps(versions)]
    probe = runner.call("python-probe", command, readiness=True)
    if probe[0] is None:
        require(not owned(root, AREA / "venv").exists(), "python-runtime-partial")
        runner.call("python-venv", [sys.executable, "-I", "-B", "-m", "venv", owned(root, AREA / "venv")])
    if probe[0] in (None, 3):
        runner.call("python-install", [
            interpreter, "-I", "-B", "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
            "--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", requirements,
        ])
        runner.call("python-ready", command, protocol=({"missing": 0, "wrong": 0}, None))
    else:
        require(probe[0] == 0 and Runner.matches(probe[1], {"missing": 0, "wrong": 0}), "python-runtime-refused")
    node = shutil.which("node")
    require(node is not None, "node-missing")
    tsc = owned(root, AREA / "producer/node_modules/typescript/bin/tsc")
    node_probe = runner.call("typescript-probe", [node, tsc, "--version"], readiness=True)
    if node_probe[0] != 0:
        require(not tsc.exists(), "typescript-runtime-refused")
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        require(npm is not None, "npm-missing")
        command = [os.environ.get("ComSpec", "cmd.exe"), "/c", npm] if os.name == "nt" else [npm]
        runner.call("typescript-install", [*command, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--quiet"], cwd=producer_dir)
        node_probe = runner.call("typescript-ready", [node, tsc, "--version"])
    require(node_probe[1].decode("utf-8").strip() == "Version " + pins["typescript"]["version"], "typescript-version")
    bridge = write_file(root, directory / "bridge/typescript.ts", read_file(owned(root, FIXTURES / "typescript.ts")))
    config = read_json(producer_dir / "smoke/typescript/tsconfig.json")
    config["compilerOptions"].update({
        "rootDir": str(owned(root, directory)), "outDir": str(owned(root, directory / "typescript")),
        "typeRoots": [str(producer_dir / "node_modules/@types")],
    })
    config["include"] = [str(owned(root, directory / "generated/typescript/src")) + "/**/*.ts", str(bridge)]
    config_path = write_json(root, directory / "tsconfig.json", config)
    runner.call("compile-typescript", [node, tsc, "-p", config_path])
    write_json(root, directory / "typescript/package.json", {"type": "module"})
    return interpreter, node


def compile_kotlin(root, directory, runner, producer_dir, java):
    project = producer_dir / "smoke/kotlin"
    arguments = [
        "--no-daemon", "--no-configuration-cache", "--console=plain", "-Pkotlin.compiler.execution.strategy=in-process",
        "--project-cache-dir", owned(root, AREA / "kotlin-cache"), "--init-script", owned(root, FIXTURES / "kotlin.init.gradle"),
        "-Dinterop.root=" + str(owned(root, directory)), "-Dinterop.fixtures=" + str(owned(root, FIXTURES)),
        "moneyClientInteropCompile",
    ]
    command = [os.environ.get("ComSpec", "cmd.exe"), "/c", project / "gradlew.bat"] if os.name == "nt" else ["sh", project / "gradlew"]
    runner.call("compile-kotlin", [*command, *arguments], cwd=project)
    launch = read_json(owned(root, directory / "kotlin-launch.json"))
    require(type(launch) is dict and set(launch) == {"classpath"}, "client-launch-shape")
    return [java, "-cp", classpath(launch["classpath"]), "com.pennilogic.interop.MoneyClientInteropGenerated"]


def required_commands():
    labels = ["generator-golden", "generator-models", "compile-typescript", "compile-kotlin", "backend-emit", "backend-reject"]
    for language in LANGUAGES:
        labels.extend(language + "-" + operation for operation in (
            "convert", "consume", "reject", "disagree", "refuse-disagreement", "recover", "consume-recovery",
        ))
    return labels


def validate_commands(records):
    require(type(records) is list and records, "commands-missing")
    require(all(type(record) is dict and type(record.get("label")) is str for record in records), "commands-shape")
    labels = [record["label"] for record in records]
    require(len(labels) == len(set(labels)), "commands-duplicate")
    required = required_commands()
    require([label for label in labels if label in required] == required, "commands-unexecuted")
    allowed = set(required) | {
        "toolchain-probe", "python-probe", "python-venv", "python-install", "python-ready",
        "typescript-probe", "typescript-install", "typescript-ready",
    }
    require(set(labels) <= allowed, "commands-unknown")
    for record in records:
        require(set(record) == {
            "label", "argv", "cwd", "executed", "exit_code", "expected_exit", "readiness_probe",
            "elapsed_seconds", "stdout", "stderr",
        }, "commands-shape")
        require(type(record["argv"]) is list and record["argv"]
                and all(type(arg) is str and arg for arg in record["argv"]), "commands-argv")
        require(type(record["cwd"]) is str and Path(record["cwd"]).is_absolute(), "commands-cwd")
        require(type(record.get("executed")) is bool and type(record.get("readiness_probe")) is bool, "commands-shape")
        duration = record.get("elapsed_seconds")
        require(type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration < SECONDS, "commands-duration")
        if not record["readiness_probe"]:
            expected = 1 if record["label"].endswith("-refuse-disagreement") else 0
            require(record["executed"] and type(record.get("exit_code")) is int
                    and type(record["expected_exit"]) is int
                    and record["exit_code"] == record["expected_exit"] == expected, "commands-failed")
        require(record["label"] not in required or not record["readiness_probe"], "commands-skipped")
        require(record["readiness_probe"] == (record["label"] in {"toolchain-probe", "python-probe", "typescript-probe"}),
                "commands-probe")
        for name in ("stdout", "stderr"):
            stream = record[name]
            require(type(stream) is dict and set(stream) == {"path", "bytes", "sha256"}, "commands-stream")
            require(type(stream["path"]) is str and type(stream["bytes"]) is int and 0 <= stream["bytes"] <= MAX_DOCUMENT
                    and type(stream["sha256"]) is str and sources.DIGEST.fullmatch(stream["sha256"]), "commands-stream")


def verify_commands(root, directory, records, run_id):
    validate_commands(records)
    protocols = {
        "backend-emit": (receipt("backend", "emit", COUNT, run_id), None),
        "backend-reject": (receipt("backend", "reject", INVALID_COUNT, run_id), None),
    }
    for language in LANGUAGES:
        for label, operation in (("convert", "convert"), ("recover", "convert"), ("disagree", "disagree"), ("reject", "reject")):
            protocols[language + "-" + label] = (receipt(language, operation, INVALID_COUNT if operation == "reject" else COUNT, run_id), None)
        for label in ("consume", "consume-recovery"):
            protocols[language + "-" + label] = (receipt("backend", "consume", COUNT, run_id), None)
        protocols[language + "-refuse-disagreement"] = (None, DISAGREEMENT)
    for index, record in enumerate(records):
        prefix = directory / "commands" / f"{index:02d}-{record['label']}"
        require(read_json(owned(root, Path(str(prefix) + ".json"))) == record, "command-record-binding")
        for offset, name in enumerate(("stdout", "stderr")):
            path = Path(str(prefix) + "." + name + ".txt")
            stream = record[name]
            require(stream["path"] == path.as_posix(), "command-stream-path")
            content = read_file(owned(root, path))
            require(len(content) == stream["bytes"] and digest(content) == stream["sha256"], "command-stream-binding")
            if record["label"] in protocols:
                expected = protocols[record["label"]][offset]
                require(content == b"" if expected is None else Runner.matches(content, expected), "command-receipt")


def verify_transports(root, directory, run_id):
    expected = transport(run_id, corpus(root)["values"])
    invalid = transport(run_id, read_json(owned(root, AREA / "inputs/spec/fixtures/money-wire-fixtures.v1.json"))["invalid"], INVALID_SHA256)
    validate_transport(read_json(owned(root, directory / "backend.json")), expected)
    validate_transport(read_json(owned(root, directory / "invalid.json")), invalid)
    for language in LANGUAGES:
        for suffix in (".json", "-recovered.json"):
            validate_transport(read_json(owned(root, directory / (language + suffix))), expected)
        validate_transport(read_json(owned(root, directory / (language + "-rejected.json"))), invalid, rejections=True)
        disagree = validate_transport(read_json(owned(root, directory / (language + "-disagreement.json"))),
                                     expected, compare_values=False)
        require(disagree["cases"][0]["envelope"] != expected["cases"][0]["envelope"], "fault-not-planted")
        require(disagree["cases"][1:] == expected["cases"][1:], "fault-scope")


def run_gate(root, run_id):
    require(re.fullmatch(r"[0-9a-f]{32}", run_id) is not None, "run-identity")
    current = read_json(owned(root, AREA / "current.json"))
    require(current == {"schema": REPORT_SCHEMA, "status": "running", "run_id": run_id}, "run-stale")
    require(read_json(owned(root, AREA / "run.lock")) == {"run_id": run_id}, "run-owner")
    directory = AREA / "runs" / run_id
    require(not owned(root, directory).exists(), "run-reused")
    owned(root, directory).mkdir(parents=True)
    runner = Runner(root, directory)
    prepare(root)
    sources.verify_provider(root)
    source = producer(root)
    pl = module_at("interop_accepted_pl", source / "scripts/pl_contracts.py")
    launch = backend_launch(root)
    initial_inputs = api_inputs(root)
    backend_bindings = classpath_bindings(launch["classpath"])
    pins = prepare_tools(root, runner, source)
    verify_default_outputs(root, source, pl)
    generator = [sys.executable, "-I", "-B", source / "scripts/generate_clients.py"]
    runner.call("generator-golden", [*generator, "--verify"], cwd=source)
    require(all(not (source / "build/generated" / language / MODELS[language]).exists() for language in LANGUAGES),
            "default-is-not-scratch")
    specification = scratch_spec(root, directory)
    runner.call("generator-models", [
        *generator, "--spec", specification, "--language", "kotlin", "--language", "typescript",
        "--language", "python", "--output-dir", owned(root, directory / "generated"),
    ], cwd=source)
    clients = [generated(root, directory, specification, language, pl) for language in LANGUAGES]
    interpreter, node = runtime_dependencies(root, directory, runner, source, pins)
    kotlin = compile_kotlin(root, directory, runner, source, launch["java"])
    client_commands = {
        "kotlin": kotlin,
        "typescript": [node, owned(root, directory / "typescript/bridge/typescript.js")],
        "python": [interpreter, "-I", "-B", owned(root, FIXTURES / "python_bridge.py")],
    }
    backend = [launch["java"], "-cp", classpath(launch["classpath"]), "com.pennilogic.money.MoneyClientInteropBridge"]
    corpus_file = owned(root, AREA / "inputs/spec/fixtures/money-roundtrip-generated.v1.json")
    invalid_file = owned(root, AREA / "inputs/spec/fixtures/money-wire-fixtures.v1.json")
    expected = transport(run_id, corpus(root)["values"])
    vectors = read_json(invalid_file)["invalid"]
    require(len(vectors) == INVALID_COUNT, "invalid-corpus-count")
    invalid = transport(run_id, vectors, INVALID_SHA256)
    bad = write_json(root, directory / "invalid.json", invalid)
    emitted = owned(root, directory / "backend.json")
    runner.call("backend-emit", [*backend, "emit", corpus_file, emitted, run_id],
                protocol=(receipt("backend", "emit", COUNT, run_id), None))
    validate_transport(read_json(emitted), expected)
    runner.call("backend-reject", [*backend, "reject", invalid_file, bad, run_id],
                protocol=(receipt("backend", "reject", INVALID_COUNT, run_id), None))
    for client in clients:
        language = client["target"]

        def convert(operation, label, input_file, output_file, count):
            arguments = [operation]
            if language == "python":
                arguments.append(owned(root, directory / "generated/python"))
            arguments.extend([input_file, output_file])
            runner.call(language + "-" + label, [*client_commands[language], *arguments],
                        protocol=(receipt(language, operation, count, run_id), None))

        output = owned(root, directory / (language + ".json"))
        convert("convert", "convert", emitted, output, COUNT)
        validate_transport(read_json(output), expected)
        runner.call(language + "-consume", [*backend, "consume", corpus_file, output, run_id],
                    protocol=(receipt("backend", "consume", COUNT, run_id), None))
        rejected = owned(root, directory / (language + "-rejected.json"))
        convert("reject", "reject", bad, rejected, INVALID_COUNT)
        validate_transport(read_json(rejected), invalid, rejections=True)
        disagreement = owned(root, directory / (language + "-disagreement.json"))
        convert("disagree", "disagree", emitted, disagreement, COUNT)
        disagree = validate_transport(read_json(disagreement), expected, compare_values=False)
        require(disagree["cases"][0]["envelope"] != expected["cases"][0]["envelope"], "fault-not-planted")
        require(disagree["cases"][1:] == expected["cases"][1:], "fault-scope")
        runner.call(language + "-refuse-disagreement", [*backend, "consume", corpus_file, disagreement, run_id],
                    expected=1, protocol=(None, DISAGREEMENT))
        recovered = owned(root, directory / (language + "-recovered.json"))
        convert("convert", "recover", emitted, recovered, COUNT)
        validate_transport(read_json(recovered), expected)
        runner.call(language + "-consume-recovery", [*backend, "consume", corpus_file, recovered, run_id],
                    protocol=(receipt("backend", "consume", COUNT, run_id), None))
        client.update({"round_trip": COUNT, "invalid_rejected": INVALID_COUNT, "disagreement_refused": True, "recovery": COUNT, "skipped": 0})
    require(api_inputs(root) == initial_inputs and classpath_bindings(launch["classpath"]) == backend_bindings, "inputs-changed-during-run")
    producer(root)
    validate_commands(runner.records)
    report = {
        "schema": REPORT_SCHEMA, "status": "passed", "run_id": run_id,
        "scope": "api-test-only-generated-model-round-trip", "release_consumed": False,
        "source_ref": SOURCE, "source_tree": SOURCE_TREE, "catalog_sha256": CATALOG_SHA256,
        "spec_version": "0.1.0", "scratch_spec_sha256": digest(read_file(specification)),
        "corpus_sha256": CORPUS_SHA256, "canonical_sha256": CANONICAL_SHA256,
        "case_count": COUNT, "generated_count": 10000, "boundary_count": 30, "invalid_count": INVALID_COUNT,
        "currencies": ["INR", "JPY", "KWD"], "default_money_model_emitted": False,
        "languages": clients, "inputs": initial_inputs, "backend_classpath": backend_bindings,
        "client_classpath": classpath_bindings(read_json(owned(root, directory / "kotlin-launch.json"))["classpath"]),
        "commands": runner.records, "outputs": inventory(owned(root, directory)),
        "elapsed_seconds": round(time.monotonic() - runner.started, 6),
    }
    require(report["elapsed_seconds"] < SECONDS, "process-budget")
    report_file = write_json(root, directory / "report.json", report)
    write_json(root, AREA / "current.json", {
        "schema": REPORT_SCHEMA, "status": "passed", "run_id": run_id, "report_sha256": binding(report_file)["sha256"],
    }, replace=True)
    verify(root)
    return {"event": "money_client_interop", "status": "passed", "run_id": run_id,
            "case_count": COUNT, "languages": list(LANGUAGES), "invalid_cases_per_language": INVALID_COUNT,
            "disagreement_refusals": 3, "recoveries": 3, "elapsed_seconds": round(time.monotonic() - runner.started, 6),
            "report": (directory / "report.json").as_posix()}


def completion(root, run_id=None):
    current = read_json(owned(root, AREA / "current.json"))
    require(type(current) is dict and set(current) == {"schema", "status", "run_id", "report_sha256"}
            and current["schema"] == REPORT_SCHEMA and current["status"] == "passed", "evidence-incomplete")
    require(type(current["run_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", current["run_id"]), "evidence-identity")
    require(run_id is None or current["run_id"] == run_id, "evidence-stale")
    directory = AREA / "runs" / current["run_id"]
    report_file = owned(root, directory / "report.json")
    require(binding(report_file)["sha256"] == current["report_sha256"], "evidence-tampered")
    report = read_json(report_file)
    require(type(report) is dict and set(report) == {
        "schema", "status", "run_id", "scope", "release_consumed", "source_ref", "source_tree", "catalog_sha256",
        "spec_version", "scratch_spec_sha256", "corpus_sha256", "canonical_sha256", "case_count", "generated_count",
        "boundary_count", "invalid_count", "currencies", "default_money_model_emitted", "languages", "inputs",
        "backend_classpath", "client_classpath", "commands", "outputs", "elapsed_seconds",
    }, "evidence-shape")
    require(report["schema"] == REPORT_SCHEMA and report["status"] == "passed"
            and report["run_id"] == current["run_id"], "evidence-stale")
    return directory, report


def verify(root=ROOT):
    verify_sources(root)
    sources.verify_provider(root)
    directory, report = completion(root)
    require(report["scope"] == "api-test-only-generated-model-round-trip" and report["release_consumed"] is False
            and report["source_ref"] == SOURCE and report["source_tree"] == SOURCE_TREE
            and report["catalog_sha256"] == CATALOG_SHA256 and report["spec_version"] == "0.1.0", "evidence-source")
    require(report["corpus_sha256"] == CORPUS_SHA256 and report["canonical_sha256"] == CANONICAL_SHA256, "evidence-corpus")
    require(all(type(report[key]) is int and report[key] == value for key, value in {
        "case_count": COUNT, "generated_count": 10000, "boundary_count": 30, "invalid_count": INVALID_COUNT,
    }.items()), "evidence-count")
    require(report["currencies"] == ["INR", "JPY", "KWD"] and report["default_money_model_emitted"] is False, "evidence-scope")
    require(report["inputs"] == api_inputs(root), "evidence-stale-inputs")
    require(report["backend_classpath"] == classpath_bindings(backend_launch(root)["classpath"]), "evidence-backend")
    require(report["client_classpath"] == classpath_bindings(read_json(owned(root, directory / "kotlin-launch.json"))["classpath"]), "evidence-client")
    files = inventory(owned(root, directory))
    del files["report.json"]
    require(report["outputs"] == files, "evidence-outputs")
    verify_commands(root, directory, report["commands"], report["run_id"])
    verify_transports(root, directory, report["run_id"])
    require(type(report["languages"]) is list and [client["target"] for client in report["languages"]] == list(LANGUAGES), "evidence-languages")
    pl = module_at("interop_verify_pl", producer(root) / "scripts/pl_contracts.py")
    specification = owned(root, directory / "spec/openapi.yaml")
    require(report["scratch_spec_sha256"] == digest(read_file(specification)), "evidence-spec")
    for client in report["languages"]:
        expected = generated(root, directory, specification, client["target"], pl)
        expected.update({"round_trip": COUNT, "invalid_rejected": INVALID_COUNT, "disagreement_refused": True, "recovery": COUNT, "skipped": 0})
        require(client == expected and client["disagreement_refused"] is True
                and all(type(client[key]) is int for key in ("round_trip", "invalid_rejected", "recovery", "skipped")),
                "evidence-client-count")
    require(type(report["elapsed_seconds"]) in (int, float) and math.isfinite(report["elapsed_seconds"])
            and 0 < report["elapsed_seconds"] < SECONDS, "evidence-budget")
    return {"event": "money_client_interop", "status": "verified", "run_id": report["run_id"], "case_count": COUNT}


@contextmanager
def exclusive_run(root, run_id):
    relative = AREA / "run.lock"
    path = owned(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.open("xb")
    except FileExistsError:
        raise InteropError("run-busy-or-stale-lock") from None
    with stream:
        identity = os.fstat(stream.fileno())
        stream.write(json_bytes({"run_id": run_id}))
    try:
        yield
    finally:
        sources.rollback_attempt(root, [(relative, identity)])


def launch_run(root, args):
    run_id = uuid.uuid4().hex
    with exclusive_run(root, run_id):
        write_json(root, AREA / "current.json", {"schema": REPORT_SCHEMA, "status": "running", "run_id": run_id}, replace=True)
        try:
            if args.java is not None:
                write_json(root, AREA / "backend-launch.json", {
                    "java": args.java, "classpath": args.classpath.split(os.pathsep),
                }, replace=True)
            completed = process_budget.run([sys.executable, "-I", "-B", str(Path(__file__).absolute()),
                                            "_run", "--run-id", run_id], root, SECONDS)
            require(completed.returncode == 0, "gate-refused")
            # The owned worker already verified every binding before returning. Recheck its
            # exact completion, not the entire inventory a second time outside the process budget.
            completion(root, run_id)
        except (InteropError, sources.MaterializationError, process_budget.BudgetExceeded,
                OSError, ValueError, KeyError, TypeError, RecursionError):
            write_json(root, AREA / "current.json", {"schema": REPORT_SCHEMA, "status": "refused", "run_id": run_id}, replace=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "verify", "_run"))
    parser.add_argument("--offline-source", type=Path)
    parser.add_argument("--backend-input-file", type=Path)
    parser.add_argument("--java")
    parser.add_argument("--classpath")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        require(args.offline_source is None or args.command == "prepare", "argument-mode")
        require(args.run_id is None or args.command == "_run", "argument-mode")
        if args.java is not None or args.classpath is not None:
            require(args.command == "run" and args.java is not None and args.classpath is not None
                    and args.backend_input_file is None, "argument-mode")
        if args.backend_input_file is not None:
            require(args.command == "run" and args.backend_input_file.absolute() == owned(ROOT, AREA / "backend-launch.json"), "backend-launch-path")
        if args.command == "prepare":
            result = prepare(offline_source=args.offline_source)
        elif args.command == "verify":
            result = verify()
        elif args.command == "_run":
            require(args.run_id is not None, "run-identity")
            result = run_gate(ROOT, args.run_id)
        else:
            launch_run(ROOT, args)
            return 0
        print(json.dumps(result, sort_keys=True))
        return 0
    except (InteropError, sources.MaterializationError) as error:
        code = str(error)
    except process_budget.BudgetExceeded:
        code = "process-budget"
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        code = "interop-input-or-io"
    print(json.dumps({"event": "money_client_interop", "status": "refused", "code": code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

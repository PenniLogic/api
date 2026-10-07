"""API-owned, test-only backend -> emitted client model -> backend Money gate."""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
import zlib


ROOT = Path(__file__).absolute().parents[1]
AREA = Path("build") / "money-client-interop"
FIXTURES = Path("scripts") / "tests" / "fixtures" / "money_client_interop"
BACKEND = Path("src/test/kotlin/com/pennilogic/money/MoneyClientInteropBridge.kt")
SOURCE = "aa8d90cb98cec9b6dd08c91b3a4d869e47362662"
SOURCE_TREE = "da0d17d9deaee9c049776d16c1511c5840fa16fe"
ARCHIVE_URL = "https://codeload.github.com/PenniLogic/contracts/tar.gz/" + SOURCE
CATALOG_SHA256 = "da45087cec04f9cdfee7cf880533b2e0527159a535c872f55f8d27c8b9e33016"
CORPUS_SHA256 = "22ed8a8ff3204a8962c10c8fcfada7d20dd6ed147fc2a07f98edef827ea28efe"
INVALID_SHA256 = "3c95a95c290fd1aad5d58300c301ab3c8c6b3b2883a95fbb586b58336ab1cf1d"
CANONICAL_SHA256 = "6d3172f611bc69f7d529912f68539a1e66101a1c188710ff2400ceab4fbb1ef6"
WIRE_SCHEMA = "pennilogic.api-money-client-wire/1"
REJECTIONS_SCHEMA = "pennilogic.api-money-client-rejections/1"
REPORT_SCHEMA = "pennilogic.api-money-client-interop/2"
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
        "native-public-git-tree-and-archive-get", "offline-hash-and-git-blob-verified",
    }, "acquisition-mode")
    native = value["mode"] == "native-public-git-tree-and-archive-get"
    archive_fields = {"response_bytes", "elapsed_seconds", "api_requests", "archive_requests", "archive_bytes",
                      "archive_unpacked_bytes", "archive_members", "archive_sha256", "archive_canonicalized_paths"}
    require(set(value) == common | (archive_fields if native else set()), "acquisition-shape")
    require(value["schema"] == "pennilogic.api-money-client-acquisition/" + ("2" if native else "1")
            and value["source_ref"] == SOURCE and value["source_tree"] == SOURCE_TREE
            and value["catalog_sha256"] == CATALOG_SHA256, "acquisition-source")
    require(all(type(value[key]) is int and value[key] == expected for key, expected in {
        "inputs": 39, "requests": 4 if native else 0, "reused_provider_inputs": 10 if native else 0,
    }.items()), "acquisition-count")
    if native:
        require(all(type(value[key]) is int and value[key] == expected
                    for key, expected in {"api_requests": 3, "archive_requests": 1}.items()), "acquisition-count")
        require(all(type(value[key]) is int for key in
                    ("response_bytes", "archive_bytes", "archive_unpacked_bytes", "archive_members"))
                and 0 < value["archive_bytes"] <= min(value["response_bytes"], sources.MAX_RESPONSE_BYTES)
                and value["response_bytes"] <= sources.MAX_TOTAL_BYTES
                and 0 < value["archive_unpacked_bytes"] <= sources.MAX_TOTAL_BYTES
                and 29 <= value["archive_members"] <= 4097,
                "acquisition-size")
        require(type(value["archive_sha256"]) is str and sources.DIGEST.fullmatch(value["archive_sha256"]),
                "acquisition-archive")
        require(type(value["archive_canonicalized_paths"]) is list
                and value["archive_canonicalized_paths"] in ([], ["smoke/kotlin/gradlew.bat"]), "acquisition-archive")
        require(type(value["elapsed_seconds"]) in (int, float) and math.isfinite(value["elapsed_seconds"])
                and 0 < value["elapsed_seconds"] < sources.DEADLINE_SECONDS, "acquisition-budget")


def fetch_archive(client):
    """One binary response; reuse the native no-proxy/no-redirect opener and every wire budget."""
    require(not client.authenticated_local, "acquisition-authentication")
    request = urllib.request.Request(ARCHIVE_URL, method="GET", headers={
        "Accept": "application/gzip", "User-Agent": "PenniLogic-API-Money-client-interoperability",
    })
    started = client.budget.clock()
    timeout = client.budget.request()
    deadline = started + timeout
    try:
        with client.opener.open(request, timeout=timeout) as response:
            client.budget.remaining(deadline)
            require(response.status == 200, "source-http")
            require(response.geturl() == request.full_url, "source-redirect")
            length = sources.response_length(response)
            encoding = response.headers.get_all("Content-Encoding", [])
            require(not encoding or len(encoding) == 1 and encoding[0].strip().lower() == "identity",
                    "source-protocol")
            completion = None
            if response.chunked:
                completion = sources.ChunkedCompletion(response.fp)
                response.fp = completion
            parts, size = [], 0
            while True:
                client.budget.remaining(deadline)
                part = response.read1(min(65536, sources.MAX_RESPONSE_BYTES + 1 - size))
                client.budget.remaining(deadline)
                if not part:
                    break
                size += len(part)
                client.budget.bytes += len(part)
                require(size <= sources.MAX_RESPONSE_BYTES and client.budget.bytes <= sources.MAX_TOTAL_BYTES,
                        "source-size")
                parts.append(part)
            require(length is None or size == length, "source-protocol")
            require(completion is None or completion.last_line in (b"\r\n", b"\n"), "source-protocol")
        client.budget.remaining(deadline)
        return b"".join(parts)
    except urllib.error.HTTPError as error:
        code = sources.http_refusal(error)
        error.close()
        raise InteropError(code) from None
    except http.client.HTTPException:
        raise InteropError("source-protocol") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise InteropError("source-unavailable") from None


def archive_contents(content, snapshot, bindings):
    budget = snapshot.client.budget
    budget.remaining()
    require(0 < len(content) <= sources.MAX_RESPONSE_BYTES, "source-size")
    try:
        decoder = zlib.decompressobj(31)
        unpacked = decoder.decompress(content, sources.MAX_TOTAL_BYTES + 1)
    except zlib.error:
        raise InteropError("archive-compression") from None
    require(len(unpacked) <= sources.MAX_TOTAL_BYTES, "archive-size")
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, "archive-completion")
    budget.remaining()
    prefix = "contracts-" + SOURCE + "/"
    selected, seen, canonicalized, end, count = {}, set(), [], 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
            for member in archive:
                budget.remaining()
                count += 1
                require(count <= min(4097, len(snapshot.entries) + 1), "archive-count")
                name = member.name
                require(0 < len(name) <= 1024 + len(prefix) and "\\" not in name
                        and all(ord(char) >= 32 and char != "\x7f" for char in name)
                        and all(part not in ("", ".", "..") for part in name.split("/")), "archive-path")
                require(name.casefold() not in seen, "archive-duplicate")
                seen.add(name.casefold())
                directory = member.type == tarfile.DIRTYPE
                require((directory or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE))
                        and member.sparse is None and not member.linkname
                        and 0 <= member.mode <= 0o777 and member.size >= 0, "archive-kind")
                require(set(member.pax_headers) <= {"path", "comment"}
                        and member.pax_headers.get("comment", SOURCE) == SOURCE, "archive-metadata")
                if name == prefix[:-1]:
                    require(directory and member.size == 0, "archive-root")
                else:
                    require(name.startswith(prefix), "archive-root")
                    relative = name[len(prefix):]
                    entry = snapshot.entries.get(relative)
                    require(entry is not None, "archive-unbound-member")
                    require(entry["type"] == ("tree" if directory else "blob")
                            and entry["mode"] in (("040000",) if directory else ("100644", "100755")),
                            "archive-kind")
                    exported_crlf = not directory and relative == "smoke/kotlin/gradlew.bat" and relative in bindings \
                        and entry["size"] < member.size <= entry["size"] * 2
                    require((member.size == (0 if directory else entry["size"]) or exported_crlf)
                            and (directory or member.mode & 0o111 == int(entry["mode"], 8) & 0o111), "archive-binding")
                    if relative in bindings:
                        binding = bindings[relative]
                        require(not directory and entry["mode"] == binding["mode"]
                                and entry["sha"] == binding["git_blob"] and entry["size"] == binding["bytes"],
                                "source-blob-binding")
                        with archive.extractfile(member) as stream:
                            value = stream.read(member.size + 1)
                        if exported_crlf:
                            # GitHub exports this batch wrapper with CRLF; require the unchanged canonical blob below.
                            value = value.replace(b"\r\n", b"\n")
                            canonicalized.append(relative)
                        sources.check_bytes(value, binding)
                        selected[relative] = value
                end = member.offset_data + ((member.size + 511) // 512) * 512
                require(end <= len(unpacked), "archive-completion")
        require(len(unpacked) - end >= 1024 and not any(unpacked[end:]), "archive-completion")
    except (tarfile.TarError, UnicodeError, ValueError, OverflowError) as error:
        if isinstance(error, (InteropError, sources.MaterializationError)):
            raise
        raise InteropError("archive-format") from None
    require(set(selected) == set(bindings), "archive-inputs")
    budget.remaining()
    return selected, {"archive_bytes": len(content), "archive_unpacked_bytes": len(unpacked),
                      "archive_members": count, "archive_sha256": digest(content),
                      "archive_canonicalized_paths": canonicalized}


def acquire(data, root=ROOT):
    """Reuse verified provider inputs, then prove one bounded public archive against the native Git tree."""
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
    client = sources.ReadOnlyClient(data)
    snapshot = sources.GitSnapshot(client, data["sources"][0], 335295566)
    require(snapshot.root == SOURCE_TREE, "source-tree-binding")
    downloaded, archive = archive_contents(fetch_archive(client), snapshot, {entry["path"]: entry for entry in missing})
    contents.update(downloaded)
    client.budget.remaining()
    require(client.budget.requests == 4, "acquisition-count")
    return contents, {
        "mode": "native-public-git-tree-and-archive-get", "requests": client.budget.requests,
        "api_requests": 3, "archive_requests": 1, "response_bytes": client.budget.bytes,
        "reused_provider_inputs": 10, "elapsed_seconds": client.budget.clock() - client.budget.started, **archive,
    }


def prepare(root=ROOT, offline_source=None):
    data = catalog(root)
    target = owned(root, AREA / "inputs")
    if target.exists():
        verify_sources(root)
        verify_acquisition(root)
        return {"event": "money_client_inputs", "status": "verified_existing", "inputs": 39, "requests": 0}
    require(not owned(root, AREA / "acquisition.json").exists(), "acquisition-output-partial")
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
        "schema": "pennilogic.api-money-client-acquisition/" + ("1" if offline_source is not None else "2"), "source_ref": SOURCE,
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
                require(json_bytes(row["envelope"]) == json_bytes(expected["cases"][index]["envelope"]), "wire-disagreement")
    return data


def validate_disagreement(data, expected):
    validate_transport(data, expected, compare_values=False)
    require(expected["case_count"] >= 2, "fault-scope")
    first = expected["cases"][0]["envelope"]
    replacement = expected["cases"][1]["envelope"]["total"]
    require(json_bytes(first["total"]) != json_bytes(replacement), "fault-not-planted")
    planted = {**first, "total": replacement}
    require(json_bytes(data["cases"][0]["envelope"]) == json_bytes(planted), "fault-not-planted")
    require(json_bytes(data["cases"][1:]) == json_bytes(expected["cases"][1:]), "fault-scope")
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
    require(type(launch) is dict and set(launch) == {"java", "classpath", "python", "node", "npm"}, "backend-launch-shape")
    require(type(launch["java"]) is str and Path(launch["java"]).is_absolute()
            and Path(launch["java"]).name in {"java", "java.exe"} and Path(launch["java"]).is_file(), "backend-java")
    require(launch["python"] == str(Path(sys.executable).absolute()), "backend-python")
    for key in ("node", "npm"):
        require(type(launch[key]) is str and Path(launch[key]).is_absolute()
                and Path(launch[key]).is_file(), "backend-runtime")
    classpath(launch["classpath"])
    return launch


def node_runtime(explicit=None):
    if explicit is not None:
        candidates = [explicit]
    elif os.name == "nt":
        candidates = [Path(process_budget.system_paths()[0].anchor) / "Program Files/nodejs/node.exe"]
    else:
        candidates = [Path("/usr/local/bin/node"), Path("/usr/bin/node")]
    node = next((Path(path).absolute() for path in candidates if Path(path).is_absolute() and Path(path).is_file()), None)
    require(node is not None and node.name in {"node", "node.exe"}, "node-missing")
    candidates = [node.parent / "node_modules/npm/bin/npm-cli.js",
                  node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    if os.name != "nt":
        candidates.append(Path("/usr/share/nodejs/npm/bin/npm-cli.js"))
    npm = next((path for path in candidates if path.is_file()), None)
    require(npm is not None, "npm-missing")
    return str(node), str(npm)


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
    def __init__(self, root, directory, *, runtime=None, inherit_tree=False):
        self.root, self.directory = root, directory
        self.started, self.records = time.monotonic(), []
        self.runtime, self.inherit_tree = runtime or {}, inherit_tree

    def child_environment(self, label):
        support = {key: value for key, value in os.environ.items() if key in process_budget.ENVIRONMENT_KEYS}
        java = label.startswith(("generator-", "backend-", "kotlin-")) or label in {"toolchain-probe", "compile-kotlin"}
        if java and self.runtime.get("java"):
            support["JAVA_HOME"] = str(Path(self.runtime["java"]).parent.parent)
        elif not java:
            support.pop("JAVA_HOME", None)
            support.pop("GRADLE_USER_HOME", None)
        if java and "GRADLE_USER_HOME" not in support:
            support["GRADLE_USER_HOME"] = str(Path.home() / ".gradle")
        home = owned(self.root, self.directory / "home")
        temporary = owned(self.root, self.directory / "tmp")
        home.mkdir(parents=True, exist_ok=True)
        temporary.mkdir(parents=True, exist_ok=True)
        support.update(HOME=str(home), USERPROFILE=str(home), TEMP=str(temporary),
                       TMP=str(temporary), TMPDIR=str(temporary))
        directories = []
        if (label.startswith("typescript-") or label == "compile-typescript") and self.runtime.get("node"):
            directories.append(Path(self.runtime["node"]).parent)
        return process_budget.environment(support, runtime_dirs=directories)

    def call(self, label, argv, *, cwd=None, expected=0, protocol=None, readiness=False):
        require(label not in {record["label"] for record in self.records}, "command-identity")
        argv = [str(arg) for arg in argv]
        start = time.monotonic()
        environment = self.child_environment(label)
        failure, observed = None, None
        try:
            result = process_budget.run(
                argv, cwd or self.root, max(0.001, SECONDS - (start - self.started)), capture=True,
                env=environment, separate=True, inherit_tree=self.inherit_tree,
            )
            executed, code, stdout, stderr = True, result.returncode, result.stdout, result.stderr
        except FileNotFoundError:
            executed, code, stdout, stderr = False, None, b"", b""
        except process_budget.BudgetExceeded as error:
            executed, code, stdout, stderr = True, None, error.output or b"", error.stderr or b""
            observed = error.streams
            failure = "command-output-size" if isinstance(error, process_budget.OutputExceeded) else "process-budget"
        record = {
            "label": label, "argv": argv, "cwd": str(cwd or self.root), "executed": executed,
            "exit_code": code, "expected_exit": expected, "readiness_probe": readiness,
            "elapsed_seconds": round(time.monotonic() - start, 6), "environment": environment,
        }
        safe_protocol = failure is None and (protocol is None or (stdout == b"" if protocol[0] is None else self.matches(stdout, protocol[0])))
        if protocol is not None and safe_protocol:
            safe_protocol = safe_protocol and (stderr == b"" if protocol[1] is None else self.matches(stderr, protocol[1]))
        for name, content in (("stdout", stdout), ("stderr", stderr)):
            stream = {**(observed[name] if observed is not None else {"bytes": len(content), "sha256": digest(content)}),
                      "path": None}
            # Unexpected runtime output is hash-only: even a broken bridge cannot log Money.
            if safe_protocol and len(content) <= MAX_DOCUMENT:
                path = self.directory / "commands" / f"{len(self.records):02d}-{label}.{name}.txt"
                write_file(self.root, path, content)
                stream["path"] = path.as_posix()
            record[name] = stream
        if failure is not None:
            record.update(capture_complete=False, failure=failure)
        self.records.append(record)
        write_json(self.root, self.directory / "commands" / f"{len(self.records) - 1:02d}-{label}.json", record)
        require(failure is None, failure)
        require(safe_protocol, "command-protocol")
        require(len(stdout) <= MAX_DOCUMENT and len(stderr) <= MAX_DOCUMENT
                and len(stdout) + len(stderr) <= MAX_DOCUMENT, "command-output-size")
        if label == "typescript-install" and executed and code != expected:
            codes = set(re.findall(rb"^npm (?:error|ERR!) code ([A-Z][A-Z0-9_]{0,31})\r?$", stderr, re.MULTILINE))
            allowed = {
                b"EBADENGINE", b"EUSAGE", b"EINTEGRITY", b"EACCES", b"EPERM", b"ENOSPC",
                b"ENOTFOUND", b"EAI_AGAIN", b"ECONNRESET", b"ETIMEDOUT", b"E401", b"E403", b"E404",
            }
            npm_code = next(iter(codes)).decode("ascii") if len(codes) == 1 and codes <= allowed else "unclassified"
            print(json.dumps({
                "event": "money_client_dependency_failure", "status": "refused",
                "command": "typescript-install", "exit_code": code, "npm_code": npm_code,
                **{name: {key: record[name][key] for key in ("bytes", "sha256")} for name in ("stdout", "stderr")},
            }, sort_keys=True), file=sys.stderr)
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
    probe = runner.call("toolchain-probe", [sys.executable, "-I", "-S", "-B", producer_dir / "scripts/toolchain.py", "verify"], readiness=True)
    require(probe[0] == 0 or not (jar.exists() and binary.exists()), "toolchain-refused")
    return pins


def runtime_dependencies(root, directory, runner, producer_dir, pins):
    require(re.fullmatch(r"[0-9a-f]{32}", directory.name), "runtime-identity")
    base = AREA / "runtimes" / directory.name
    require(not owned(root, base).exists(), "runtime-storage-exists")
    owned(root, base).mkdir(parents=True)
    interpreter = owned(root, base / "venv" / ("Scripts" if os.name == "nt" else "bin")) / ("python.exe" if os.name == "nt" else "python")
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
    missing_probe = [sys.executable, "-I", "-S", "-B", "-c",
                     "import pathlib,sys;sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 3)"]
    probe = runner.call("python-probe", [*missing_probe, interpreter], readiness=True)
    require(probe[0] == 3, "python-runtime-not-fresh")
    runner.call("python-venv", [sys.executable, "-I", "-S", "-B", "-m", "venv", "--copies", owned(root, base / "venv")])
    runner.call("python-install", [
        interpreter, "-I", "-B", "-m", "pip", "--isolated", "install", "--quiet", "--disable-pip-version-check",
        "--no-cache-dir", "--no-compile", "--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", requirements,
    ])
    runner.call("python-ready", [interpreter, "-I", "-B", "-c", probe_code, json.dumps(versions)],
                protocol=({"missing": 0, "wrong": 0}, None))
    node, npm = runner.runtime["node"], runner.runtime["npm"]
    project = owned(root, base / "node")
    for name in ("package.json", "package-lock.json", ".npmrc"):
        write_file(root, base / "node" / name, read_file(producer_dir / name))
    tsc = owned(root, base / "node/node_modules/typescript/bin/tsc")
    probe = runner.call("typescript-probe", [*missing_probe, tsc], readiness=True)
    require(probe[0] == 3, "typescript-runtime-not-fresh")
    runner.call("typescript-install", [
        node, npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--quiet", "--bin-links=false",
        "--engine-strict", "--userconfig", project / ".npmrc", "--cache", owned(root, base / "npm-cache"),
    ], cwd=project)
    node_probe = runner.call("typescript-ready", [node, tsc, "--version"])
    require(node_probe[1].decode("utf-8").strip() == "Version " + pins["typescript"]["version"], "typescript-version")
    write_json(root, directory / "runtime-inputs.json", runtime_inputs(root, directory, runner.runtime))
    bridge = write_file(root, directory / "bridge/typescript.ts", read_file(owned(root, FIXTURES / "typescript.ts")))
    config = read_json(producer_dir / "smoke/typescript/tsconfig.json")
    config["compilerOptions"].update({
        "rootDir": str(owned(root, directory)), "outDir": str(owned(root, directory / "typescript")),
        "typeRoots": [str(project / "node_modules/@types")],
    })
    config["include"] = [str(owned(root, directory / "generated/typescript/src")) + "/**/*.ts", str(bridge)]
    config_path = write_json(root, directory / "tsconfig.json", config)
    runner.call("compile-typescript", [node, tsc, "-p", config_path])
    write_json(root, directory / "typescript/package.json", {"type": "module"})
    return interpreter, node


def runtime_inputs(root, directory, runtime):
    base = owned(root, AREA / "runtimes" / directory.name)
    scripts = base / "venv" / ("Scripts" if os.name == "nt" else "bin")
    library = base / "venv" / ("Lib" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}")
    return {
        "schema": "pennilogic.api-money-client-runtime-inputs/1",
        "origin": "fresh-hash-required-wheels-and-npm-lock", "run_id": directory.name,
        "python_config": binding(base / "venv/pyvenv.cfg"),
        "python_scripts": inventory(scripts),
        "python_packages": inventory(library / "site-packages", limit=16384),
        "node_packages": inventory(base / "node", limit=16384),
        "tools": {
            key: {"path": runtime[key], "resolved_path": str(Path(runtime[key]).resolve(strict=True)),
                  **binding(Path(runtime[key]).resolve(strict=True))}
            for key in ("java", "python", "node", "npm")
        },
    }


def compile_kotlin(root, directory, runner, producer_dir, java):
    project = producer_dir / "smoke/kotlin"
    arguments = [
        "--no-daemon", "--no-configuration-cache", "--no-build-cache", "--rerun-tasks",
        "--console=plain", "-Pkotlin.compiler.execution.strategy=in-process",
        "--project-cache-dir", owned(root, AREA / "kotlin-cache"), "--init-script", owned(root, FIXTURES / "kotlin.init.gradle"),
        "-Dinterop.root=" + str(owned(root, directory)), "-Dinterop.fixtures=" + str(owned(root, FIXTURES)),
        "moneyClientInteropCompile",
    ]
    command = [process_budget.system_paths()[0] / "cmd.exe", "/c", project / "gradlew.bat"] if os.name == "nt" else ["/bin/sh", project / "gradlew"]
    compiled = runner.call("compile-kotlin", [*command, *arguments], cwd=project)
    require(compiled[1].splitlines().count(b"> Task :compileKotlin") == 1, "command-compiler-unexecuted")
    launch = read_json(owned(root, directory / "kotlin-launch.json"))
    require(type(launch) is dict and set(launch) == {"classpath"}, "client-launch-shape")
    return [java, "-cp", classpath(launch["classpath"]), "com.pennilogic.interop.MoneyClientInteropGenerated"]


def required_commands():
    labels = ["generator-golden", "generator-models", "python-venv", "python-install", "python-ready",
              "typescript-install", "typescript-ready", "compile-typescript", "compile-kotlin", "backend-emit", "backend-reject"]
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
            "elapsed_seconds", "environment", "stdout", "stderr",
        }, "commands-shape")
        require(type(record["environment"]) is dict and set(record["environment"]) <= process_budget.ENVIRONMENT_KEYS
                and all(type(value) is str for value in record["environment"].values()), "commands-environment")
        require(type(record["argv"]) is list and record["argv"]
                and all(type(arg) is str and arg for arg in record["argv"]), "commands-argv")
        if record["label"] == "compile-kotlin":
            require("--no-build-cache" in record["argv"] and "--rerun-tasks" in record["argv"],
                    "commands-compiler-cache")
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
        require(record["stdout"]["bytes"] + record["stderr"]["bytes"] <= MAX_DOCUMENT, "commands-stream")


def verify_commands(root, directory, records, run_id):
    validate_commands(records)
    protocols = {
        "python-ready": ({"missing": 0, "wrong": 0}, None),
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
        require(json_bytes(read_json(owned(root, Path(str(prefix) + ".json")))) == json_bytes(record),
                "command-record-binding")
        for offset, name in enumerate(("stdout", "stderr")):
            path = Path(str(prefix) + "." + name + ".txt")
            stream = record[name]
            require(stream["path"] == path.as_posix(), "command-stream-path")
            content = read_file(owned(root, path))
            require(len(content) == stream["bytes"] and digest(content) == stream["sha256"], "command-stream-binding")
            if record["label"] in protocols:
                expected = protocols[record["label"]][offset]
                require(content == b"" if expected is None else Runner.matches(content, expected), "command-receipt")
            if record["label"] == "typescript-ready":
                expected = ("Version " + read_json(owned(root, AREA / "inputs/toolchain/versions.json"))["typescript"]["version"]).encode("ascii")
                require(content.strip() == expected if name == "stdout" else content == b"", "command-receipt")
            if record["label"] == "compile-kotlin" and name == "stdout":
                require(content.splitlines().count(b"> Task :compileKotlin") == 1, "command-receipt")


def verify_transports(root, directory, run_id):
    expected = transport(run_id, corpus(root)["values"])
    invalid = transport(run_id, read_json(owned(root, AREA / "inputs/spec/fixtures/money-wire-fixtures.v1.json"))["invalid"], INVALID_SHA256)
    validate_transport(read_json(owned(root, directory / "backend.json")), expected)
    validate_transport(read_json(owned(root, directory / "invalid.json")), invalid)
    for language in LANGUAGES:
        for suffix in (".json", "-recovered.json"):
            validate_transport(read_json(owned(root, directory / (language + suffix))), expected)
        validate_transport(read_json(owned(root, directory / (language + "-rejected.json"))), invalid, rejections=True)
        validate_disagreement(read_json(owned(root, directory / (language + "-disagreement.json"))), expected)


def run_gate(root, run_id):
    require(re.fullmatch(r"[0-9a-f]{32}", run_id) is not None, "run-identity")
    current = read_json(owned(root, AREA / "current.json"))
    require(current == {"schema": REPORT_SCHEMA, "status": "running", "run_id": run_id}, "run-stale")
    require(read_json(owned(root, AREA / "run.lock")) == {"run_id": run_id}, "run-owner")
    directory = AREA / "runs" / run_id
    require(not owned(root, directory).exists(), "run-reused")
    owned(root, directory).mkdir(parents=True)
    runner = Runner(root, directory, inherit_tree=True)
    prepare(root)
    sources.verify_provider(root)
    source = producer(root)
    pl = module_at("interop_accepted_pl", source / "scripts/pl_contracts.py")
    launch = backend_launch(root)
    # The worker's outer budget owns the process group; inner commands must not escape it.
    runner.runtime = launch
    initial_inputs = api_inputs(root)
    backend_bindings = classpath_bindings(launch["classpath"])
    pins = prepare_tools(root, runner, source)
    verify_default_outputs(root, source, pl)
    generator = [sys.executable, "-I", "-S", "-B", source / "scripts/generate_clients.py"]
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
        validate_disagreement(read_json(disagreement), expected)
        runner.call(language + "-refuse-disagreement", [*backend, "consume", corpus_file, disagreement, run_id],
                    expected=1, protocol=(None, DISAGREEMENT))
        recovered = owned(root, directory / (language + "-recovered.json"))
        convert("convert", "recover", emitted, recovered, COUNT)
        validate_transport(read_json(recovered), expected)
        runner.call(language + "-consume-recovery", [*backend, "consume", corpus_file, recovered, run_id],
                    protocol=(receipt("backend", "consume", COUNT, run_id), None))
        client.update({"round_trip": COUNT, "invalid_rejected": INVALID_COUNT, "disagreement_refused": True, "recovery": COUNT, "skipped": 0})
    require(api_inputs(root) == initial_inputs and classpath_bindings(launch["classpath"]) == backend_bindings, "inputs-changed-during-run")
    require(json_bytes(read_json(owned(root, directory / "runtime-inputs.json"))) == json_bytes(runtime_inputs(root, directory, launch)),
            "runtime-inputs-changed")
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
    require(json_bytes(report["inputs"]) == json_bytes(api_inputs(root)), "evidence-stale-inputs")
    require(json_bytes(report["backend_classpath"]) == json_bytes(classpath_bindings(backend_launch(root)["classpath"])),
            "evidence-backend")
    require(json_bytes(report["client_classpath"]) == json_bytes(classpath_bindings(
        read_json(owned(root, directory / "kotlin-launch.json"))["classpath"])), "evidence-client")
    require(json_bytes(read_json(owned(root, directory / "runtime-inputs.json"))) == json_bytes(runtime_inputs(root, directory, backend_launch(root))),
            "evidence-runtime-inputs")
    files = inventory(owned(root, directory))
    del files["report.json"]
    require(json_bytes(report["outputs"]) == json_bytes(files), "evidence-outputs")
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
                node, npm = node_runtime(args.node)
                write_json(root, AREA / "backend-launch.json", {
                    "java": args.java, "classpath": args.classpath.split(os.pathsep),
                    "python": str(Path(sys.executable).absolute()), "node": node, "npm": npm,
                }, replace=True)
            completed = process_budget.run([sys.executable, "-I", "-S", "-B", str(Path(__file__).absolute()),
                                            "_run", "--run-id", run_id], root, SECONDS,
                                           env=process_budget.environment())
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
    parser.add_argument("--node", type=Path)
    parser.add_argument("--classpath")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        require(args.offline_source is None or args.command == "prepare", "argument-mode")
        require(args.run_id is None or args.command == "_run", "argument-mode")
        require(args.node is None or args.command == "run" and args.java is not None, "argument-mode")
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

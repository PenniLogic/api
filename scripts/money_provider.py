"""Prepare only the hash-pinned accepted Contracts Money source for local API use."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REF = "ea56c63d5c9b679537bd9205b04626049c20c572"
DOCS_REF = "a700e639585c61a4610e7b99dbd02b2dab28bdcc"
BUNDLE = Path("build/contracts-money")
MONEY = "src/main/kotlin/com/pennilogic/contracts/money/Money.kt"
REGISTRY = "src/main/kotlin/com/pennilogic/contracts/money/CurrencyRegistry.kt"
SOURCE_FILES = {
    "scripts/generate_clients.py": (15699, "cdfa31079fb5d327348fa311ed28ecb6da1fd8d9d62b83602fcf640261f86130"),
    "scripts/pl_contracts.py": (5163, "ce38c232e82e2d221655709cf944fb0a226462e78b98276f1be39c2928a9be61"),
    "scripts/toolchain.py": (7775, "9bbcb66cfd8d05de1b926872e653762c1eabe1a7395af55008b001e3184f46d7"),
    "runtime/kotlin/" + MONEY: (8263, "7d973d9f2a43859cc20c79789c57ebb793c051b9e7ce7652126d32e79cb97459"),
    "spec/currency-registry.v1.json": (615, "d9fd1f021993feb4c4b4c8bb131372ea5bd278a456978e5386992879b9fd44d5"),
    "spec/fixtures/money-wire-fixtures.v1.json": (14863, "3c95a95c290fd1aad5d58300c301ab3c8c6b3b2883a95fbb586b58336ab1cf1d"),
    "spec/fixtures/money-roundtrip-generated.v1.json": (901529, "22ed8a8ff3204a8962c10c8fcfada7d20dd6ed147fc2a07f98edef827ea28efe"),
    "generator/golden.json": (7609, "e33f2691b56e0c76ff40c0df67486b3b9d162f6dfbffe258c6fd43e681dda2d8"),
    "generator/kotlin.json": (652, "8988671f62d532f76551c4bf25d8a46a2b0abc1ff8be8a834b427ad8cec5e248"),
    "spec/openapi.yaml": (6875, "4f315bc28a8e1d25841f8110e5dd90126e7b101a76cbfcfba5b6348dd0bfaba9"),
}
OUTPUT_HASHES = {
    MONEY: "7d973d9f2a43859cc20c79789c57ebb793c051b9e7ce7652126d32e79cb97459",
    REGISTRY: "10d491ae90bc8f077afe5d45be671af52a5b7891c88eacfccd437a10f4956b21",
}


class ProviderError(ValueError):
    pass


def refused_walk(error):
    raise ProviderError("Accepted Money source inventory is unreadable; preserve it and stop.") from error


def digest(content):
    return hashlib.sha256(content).hexdigest()


def safe_path(root, relative):
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ProviderError("Accepted source root is missing or linked; supply the immutable source snapshot.")
    path = root / relative
    for component in (path, *path.parents):
        if component == root.parent:
            break
        if component.is_symlink() or component.is_junction():
            raise ProviderError(f"Linked provider input refused: {relative}")
    return path


def read_source(source):
    contents = {}
    for relative, (size, expected) in SOURCE_FILES.items():
        path = safe_path(source, relative)
        if not path.is_file():
            raise ProviderError(f"Missing accepted provider input: {relative}")
        content = path.read_bytes()
        if len(content) != size or digest(content) != expected:
            raise ProviderError(f"Accepted provider source mismatch: {relative}; preserve it and stop.")
        contents[relative] = content
    expected_scripts = {source / name for name in SOURCE_FILES if name.startswith("scripts/")}
    for directory, directories, files in os.walk(source / "scripts", followlinks=False, onerror=refused_walk):
        parent = Path(directory)
        if directories or any(parent / name not in expected_scripts for name in files):
            raise ProviderError("Unpinned executable/cache input beside the accepted renderer; preserve it and stop.")
    return contents


def owned_target(root, relative):
    path = root / relative
    for component in (path, *path.parents):
        if component == root.parent:
            break
        if component.is_symlink() or component.is_junction():
            raise ProviderError("Linked Money output path refused; no write is authorized.")
    return path


def render_registry(source):
    # Execute only the accepted renderer, not OpenAPI generation, tool installation or publication.
    code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "import generate_clients; path,content=generate_clients.render_registry('kotlin'); "
        "print(json.dumps({'path':path,'content':content}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(source / "scripts")],
        cwd=source, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode:
        raise ProviderError("Accepted currency renderer failed; no substitute registry is permitted.")
    output = json.loads(result.stdout)
    content = output["content"].encode("utf-8")
    if output["path"] != REGISTRY or digest(content) != OUTPUT_HASHES[REGISTRY]:
        raise ProviderError("Accepted currency renderer output does not match the pinned golden source.")
    return content


def source_outputs(contents, registry):
    golden = json.loads(contents["generator/golden.json"])["kotlin"]["files"]
    if any(golden.get(path) != expected for path, expected in OUTPUT_HASHES.items()):
        raise ProviderError("Accepted Kotlin golden source bindings differ.")
    mapping = json.loads(contents["generator/kotlin.json"])
    if mapping["importMappings"]["Money"] != "com.pennilogic.contracts.money.Money":
        raise ProviderError("Accepted Kotlin Money import mapping differs.")
    return {
        MONEY: contents["runtime/kotlin/" + MONEY].replace(b"\r\n", b"\n"),
        REGISTRY: registry,
    }


def verify_outputs(root=ROOT):
    bundle = owned_target(root, BUNDLE)
    read_source(bundle / "source")
    sources = bundle / "kotlin"
    expected_paths = {sources / path for path in OUTPUT_HASHES}
    actual_paths = set()
    if sources.is_symlink() or sources.is_junction() or not sources.is_dir():
        raise ProviderError("Prepared Money dependency source is missing or linked.")
    for directory, directories, files in os.walk(sources, followlinks=False, onerror=refused_walk):
        parent = Path(directory)
        if any((parent / name).is_symlink() or (parent / name).is_junction() for name in directories):
            raise ProviderError("Linked prepared Money source directory refused.")
        for name in files:
            path = parent / name
            if path.suffix.lower() in {".kt", ".java", ".kts"}:
                actual_paths.add(path)
    if actual_paths != expected_paths:
        raise ProviderError("Prepared Money dependency source inventory differs.")
    for relative, expected in OUTPUT_HASHES.items():
        path = safe_path(sources, relative)
        if digest(path.read_bytes()) != expected:
            raise ProviderError(f"Prepared Money source mismatch: {relative}; preserve it and stop.")
    return sorted(expected_paths)


def prepare(source=None, root=ROOT):
    bundle = owned_target(root, BUNDLE)
    cache = bundle / "source"
    source = (source or cache).absolute()
    if not source.is_dir():
        raise ProviderError(
            "Accepted Money SOURCE is not prepared. Run python scripts/money_provider.py "
            "--source-root <owned snapshot of contracts ea56c63>; no release or fallback is assumed."
        )
    contents = read_source(source)
    outputs = source_outputs(contents, render_registry(source))
    for relative, content in outputs.items():
        target = owned_target(root, BUNDLE / "kotlin" / relative)
        if target.exists() and (
            target.is_symlink() or digest(target.read_bytes()) != OUTPUT_HASHES[relative]
        ):
            raise ProviderError(f"Prepared Money source mismatch: {relative}; preserve it and stop.")
    if cache.exists():
        read_source(cache)
    for relative, content in contents.items():
        target = owned_target(root, BUNDLE / "source" / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for relative, content in outputs.items():
        target = owned_target(root, BUNDLE / "kotlin" / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    verify_outputs(root)
    record = {
        "kind": "accepted_source_only",
        "source_repository": "PenniLogic/contracts",
        "source_ref": SOURCE_REF,
        "docs_ref": DOCS_REF,
        "source_inputs": {name: {"bytes": size, "sha256": sha} for name, (size, sha) in SOURCE_FILES.items()},
        "kotlin_source_sha256": OUTPUT_HASHES,
        "release_consumed": False,
        "generated_clients_tested": False,
    }
    owned_target(root, BUNDLE / "provider.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    print(json.dumps({
        "event": "money_provider", "source_ref": SOURCE_REF, "source_files_verified": len(contents),
        "dependency_sources_verified": len(outputs), "kind": "accepted_source_only",
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, help="Owned immutable accepted Contracts source snapshot")
    parser.add_argument("--verify", action="store_true", help="Verify prepared inputs/output without writing")
    args = parser.parse_args()
    try:
        if args.verify:
            verify_outputs()
            print(json.dumps({"event": "money_provider_verified", "source_ref": SOURCE_REF, "dependency_sources_verified": 2}))
        else:
            prepare(args.source_root)
    except ProviderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("Money source provider I/O or decoding failed; no source was accepted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

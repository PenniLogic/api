import base64
import contextlib
import copy
from email.message import Message
import gzip
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).absolute().parents[2]
spec = importlib.util.spec_from_file_location("money_client_interop", ROOT / "scripts/money_client_interop.py")
interop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(interop)


class InteropTest(unittest.TestCase):
    def setUp(self):
        build = interop.owned(ROOT, Path("build"))
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="interop-unit-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_id = "a" * 32
        self.expected = interop.transport(self.run_id, [
            {"wire": {"amount": "synthetic-first", "currency": "INR"}},
            {"wire": {"amount": "synthetic-second", "currency": "JPY"}},
        ])

    def rejected(self, value, cause, **kwargs):
        with self.assertRaisesRegex(interop.InteropError, "^" + cause + "$"):
            interop.validate_transport(value, self.expected, **kwargs)


class CatalogTest(InteropTest):
    def test_loading_a_pinned_helper_does_not_write_into_its_source_inventory(self):
        path = self.root / "helper.py"
        path.write_bytes(b"answer = 42\n")
        with mock.patch.object(sys, "dont_write_bytecode", False):
            self.assertEqual(interop.module_at("synthetic_helper", path).answer, 42)
        self.assertEqual(set(interop.inventory(self.root)), {"helper.py"})

    def test_loading_a_pinned_helper_does_not_execute_unbound_cached_bytecode(self):
        path = self.root / "helper.py"
        path.write_bytes(b"answer = 42\n")
        metadata = path.stat()
        py_compile.compile(str(path), doraise=True)
        path.write_bytes(b"answer = 43\n")
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(interop.module_at("synthetic_helper", path).answer, 43)

    def test_offline_acquisition_evidence_cannot_claim_a_different_source_or_mode(self):
        record = {
            "schema": "pennilogic.api-money-client-acquisition/1", "source_ref": interop.SOURCE,
            "source_tree": interop.SOURCE_TREE, "catalog_sha256": interop.CATALOG_SHA256,
            "inputs": 39, "mode": "offline-hash-and-git-blob-verified", "requests": 0, "reused_provider_inputs": 0,
        }
        path = self.root / interop.AREA / "acquisition.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(interop.json_bytes(record))
        interop.verify_acquisition(self.root)
        for key, value in (("source_ref", "0" * 40), ("mode", "authenticated-bypass"), ("inputs", 0), ("requests", True)):
            changed = {**record, key: value}
            path.write_bytes(interop.json_bytes(changed))
            with self.subTest(key=key):
                with self.assertRaises(interop.InteropError):
                    interop.verify_acquisition(self.root)

    def test_orphan_acquisition_evidence_is_refused_before_any_download_or_source_write(self):
        interop.write_file(self.root, interop.FIXTURES / "source-inputs.json",
                           interop.read_file(ROOT / interop.FIXTURES / "source-inputs.json"))
        receipt = interop.write_file(self.root, interop.AREA / "acquisition.json", b"preserve-orphan")
        with mock.patch.object(interop, "acquire") as acquire:
            with self.assertRaisesRegex(interop.InteropError, "acquisition-output-partial"):
                interop.prepare(self.root)
        acquire.assert_not_called()
        self.assertEqual(receipt.read_bytes(), b"preserve-orphan")
        self.assertFalse((self.root / interop.AREA / "inputs").exists())

    def test_new_catalog_binds_all_three_generators_without_changing_the_old_catalog(self):
        data = interop.catalog()
        bindings = interop.source_bindings(data)
        self.assertEqual(len(bindings), 39)
        old = interop.sources.validate_catalog(interop.sources.CATALOG)
        self.assertEqual(len(interop.sources.input_bindings(old)), 11)
        for entry in old["sources"][0]["files"]:
            self.assertEqual(bindings[entry["path"]], entry)
        for language in interop.LANGUAGES:
            self.assertIn("generator/" + language + ".json", bindings)
            self.assertTrue(any(name.startswith("runtime/" + language + "/") for name in bindings))
        self.assertIn("smoke/python/requirements.txt", bindings)
        self.assertIn("package-lock.json", bindings)
        self.assertIn("smoke/kotlin/gradle/verification-metadata.xml", bindings)

    def test_catalog_tampering_wrong_source_and_versions_never_repin_themselves(self):
        target = self.root / interop.FIXTURES / "source-inputs.json"
        target.parent.mkdir(parents=True)
        original = json.loads((ROOT / interop.FIXTURES / "source-inputs.json").read_bytes())
        for field in ("schema", "consumer", "sources"):
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data[field] = None
                target.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(interop.InteropError, "catalog-binding"):
                    interop.catalog(self.root)

    def test_catalog_line_endings_are_portable_but_source_hashes_are_not_relaxed(self):
        target = self.root / interop.FIXTURES / "source-inputs.json"
        target.parent.mkdir(parents=True)
        raw = (ROOT / interop.FIXTURES / "source-inputs.json").read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(raw.replace(b"\n", b"\r\n"))
        self.assertEqual(len(interop.catalog(self.root)["sources"][0]["files"]), 39)
        with self.assertRaises(interop.sources.MaterializationError):
            interop.sources.check_bytes(b"tampered", interop.catalog()["sources"][0]["files"][0])

    def test_acquisition_rejects_an_unexpected_tree_before_downloading_a_blob(self):
        data = interop.catalog()
        client = mock.Mock()
        snapshot = mock.Mock(root="0" * 40)
        with mock.patch.object(interop.sources, "verify_inputs"), mock.patch.object(interop.sources, "verify_provider"), \
                mock.patch.object(interop.sources, "ReadOnlyClient", return_value=client), \
                mock.patch.object(interop.sources, "GitSnapshot", return_value=snapshot), \
                mock.patch.object(interop.sources, "check_bytes"), \
                mock.patch.object(interop, "read_file", return_value=b"verified-shared-input"), \
                mock.patch.object(interop, "fetch_archive") as download:
            with self.assertRaisesRegex(interop.InteropError, "source-tree-binding"):
                interop.acquire(data, self.root)
        snapshot.blob.assert_not_called()
        download.assert_not_called()

    def test_immutable_source_inventory_never_accepts_an_extra_executable(self):
        folder = self.root / "snapshot"
        folder.mkdir()
        (folder / "fixture.txt").write_bytes(b"fixture")
        entries = {"fixture.txt": {"bytes": 7, "sha256": interop.digest(b"fixture")}}
        interop.sources.verify_directory(self.root, Path("snapshot"), entries)
        (folder / "injected.py").write_bytes(b"raise SystemExit(0)\n")
        with self.assertRaises(interop.sources.MaterializationError):
            interop.sources.verify_directory(self.root, Path("snapshot"), entries)

    def test_default_generation_is_checked_before_the_accepted_generator_can_replace_it(self):
        source = self.root / interop.AREA / "producer"
        interop.verify_default_outputs(self.root, source, None)
        base = source / "build"
        for language in interop.LANGUAGES:
            (base / "generated" / language).mkdir(parents=True)
        (base / "generated-verify").mkdir()
        with mock.patch.object(interop, "generated") as verify:
            interop.verify_default_outputs(self.root, source, None)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual([call.kwargs for call in verify.call_args_list], [{"scratch": False}] * 3)
        with mock.patch.object(interop, "generated", side_effect=interop.InteropError("default-generation-tampered")):
            with self.assertRaisesRegex(interop.InteropError, "default-generation-tampered"):
                interop.verify_default_outputs(self.root, source, None)
        (base / "generated-verify" / "partial").mkdir()
        with self.assertRaisesRegex(interop.InteropError, "default-output-partial"):
            interop.verify_default_outputs(self.root, source, None)

    def test_partial_or_unknown_default_output_is_not_silently_deleted(self):
        source = self.root / interop.AREA / "producer"
        base = source / "build"
        (base / "generated" / "kotlin").mkdir(parents=True)
        with self.assertRaisesRegex(interop.InteropError, "default-output-partial"):
            interop.verify_default_outputs(self.root, source, None)
        (base / "unowned.txt").write_bytes(b"preserve")
        with self.assertRaisesRegex(interop.InteropError, "default-output-inventory"):
            interop.verify_default_outputs(self.root, source, None)
        self.assertEqual((base / "unowned.txt").read_bytes(), b"preserve")


class GateSelfTestCopyTest(InteropTest):
    def test_real_self_test_copy_preserves_all_required_interop_inputs(self):
        quality = interop.module_at("interop_quality_copy", ROOT / "scripts/quality.py")
        required = (
            "scripts/money_client_interop.py",
            "scripts/tests/fixtures/money_client_interop",
            "build/source-materialization",
        )

        class CopyVerified(Exception):
            pass

        def inspect_copy(*tasks, root, **_kwargs):
            self.assertEqual(tasks, ("test", "spotlessCheck"))
            self.assertNotEqual(root, ROOT)
            self.assertEqual([], [name for name in required if not (root / name).exists()])
            for name in required:
                with self.subTest(path=name):
                    if (ROOT / name).is_dir():
                        self.assertEqual(interop.inventory(ROOT / name), interop.inventory(root / name))
                    else:
                        self.assertEqual(interop.binding(ROOT / name), interop.binding(root / name))
            self.assertEqual(interop.catalog(ROOT), interop.catalog(root))
            self.assertEqual(interop.binding(ROOT / interop.BACKEND), interop.binding(root / interop.BACKEND))
            raise CopyVerified

        with mock.patch.object(quality, "gradle", side_effect=inspect_copy) as gradle, self.assertRaises(CopyVerified):
            quality.gate_self_test(self.root)
        self.assertEqual(gradle.call_count, 1)
        self.assertEqual([], list(self.root.iterdir()))


class PublicInputResponses:
    """Exact accepted bytes and Git proofs; one shared anonymous REST quota, no sockets."""

    archive_url = "https://codeload.github.com/PenniLogic/contracts/tar.gz/" + interop.SOURCE

    def __init__(self, limit=60):
        self.limit = limit
        self.calls, self.api_requests, self.archive_requests, self.response_bytes = [], 0, 0, 0
        self.metadata = interop.read_json(ROOT / interop.FIXTURES / "acquisition-metadata.json")
        self.blobs = {}
        self.database = interop.module_at("interop_database_inputs", ROOT / "scripts/prepare_database_admission.py")
        self.installation = self.database.read_installation(ROOT, interop.sources)
        self.contracts = {}
        for entry in interop.catalog()["sources"][0]["files"]:
            content = interop.read_file(ROOT / interop.AREA / "inputs" / entry["path"])
            self.add_blob("PenniLogic/contracts", entry, content)
            self.contracts[entry["path"]] = content
        for source in interop.sources.CATALOG["sources"]:
            for entry in source["files"]:
                self.add_blob(source["repository"], entry,
                              interop.read_file(ROOT / interop.sources.INPUTS / source["snapshot"] / entry["path"]))
        prepared = interop.read_json(ROOT / self.database.OUTPUT / "inputs.json")
        envelopes = {"policy": prepared["policy"], "evidence": prepared["inventory"]["source"]}
        for role, source in self.installation["binding"]["sources"].items():
            if role in envelopes:
                contents = {item["path"]: base64.b64decode(item["content_base64"], validate=True)
                            for item in envelopes[role]["files"]}
            else:
                prefix = self.database.OUTPUT if role == "infra" else Path(".")
                contents = {entry["path"]: interop.read_file(ROOT / prefix / entry["path"])
                            for entry in source["files"]}
                if role == "inventory":
                    contents = {name: content.replace(b"\r\n", b"\n") for name, content in contents.items()}
            for entry in source["files"]:
                self.add_blob(source["repository"], entry, contents[entry["path"]])
        self.archive = self.archive_bytes()

    def add_blob(self, repository, entry, content):
        interop.sources.check_bytes(content, entry)
        self.blobs["/repos/" + repository + "/git/blobs/" + entry["git_blob"]] = {
            "sha": entry["git_blob"], "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }

    def archive_bytes(self):
        stream = io.BytesIO()
        prefix = "contracts-" + interop.SOURCE + "/"
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT,
                          pax_headers={"comment": interop.SOURCE}) as archive:
            root = tarfile.TarInfo(prefix)
            root.type, root.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(root)
            for entry in interop.catalog()["sources"][0]["files"]:
                member = tarfile.TarInfo(prefix + entry["path"])
                member.mode, member.size = int(entry["mode"], 8) & 0o777, entry["bytes"]
                archive.addfile(member, io.BytesIO(self.contracts[entry["path"]]))
        return gzip.compress(stream.getvalue(), mtime=0)

    def open(self, request, timeout):
        self.calls.append({"url": request.full_url, "method": request.get_method(),
                           "headers": dict(request.header_items()), "timeout": timeout})
        if request.full_url == self.archive_url:
            self.archive_requests += 1
            body = self.archive
        else:
            if not request.full_url.startswith("https://api.github.com/"):
                raise AssertionError("unexpected-source-host")
            self.api_requests += 1
            if self.api_requests > self.limit:
                headers = Message()
                headers["X-RateLimit-Remaining"] = "0"
                raise urllib.error.HTTPError(request.full_url, 403, "synthetic-quota", headers, io.BytesIO())
            endpoint = request.full_url.removeprefix("https://api.github.com")
            body = interop.json_bytes(self.metadata[endpoint] if endpoint in self.metadata else self.blobs[endpoint])
        owner = self

        class Response(io.BytesIO):
            status, chunked = 200, False

            def geturl(self):
                return request.full_url

            def read1(self, size):
                content = super().read1(size)
                owner.response_bytes += len(content)
                return content

        response = Response(body)
        response.headers = Message()
        response.headers["Content-Length"] = str(len(body))
        return response


class ColdAcquisitionTest(InteropTest):
    def test_complete_cold_pipeline_fits_one_anonymous_pool(self):
        responses = PublicInputResponses()
        client_type = interop.sources.ReadOnlyClient

        def client(catalog, **kwargs):
            return client_type(catalog, opener=responses, environ={"GH_TOKEN": "synthetic-unused-token"}, **kwargs)

        interop.write_file(self.root, interop.FIXTURES / "source-inputs.json",
                           interop.read_file(ROOT / interop.FIXTURES / "source-inputs.json"))
        self.assertFalse((self.root / interop.sources.INPUTS).exists())
        self.assertFalse((self.root / interop.sources.PROVIDER).exists())
        self.assertFalse((self.root / responses.database.OUTPUT).exists())
        self.assertFalse((self.root / interop.AREA / "inputs").exists())
        with mock.patch.object(interop.sources, "ReadOnlyClient", side_effect=client):
            money = interop.sources.materialize(root=self.root)
            provider = interop.module_at("interop_cold_provider", ROOT / "scripts/money_provider.py")
            provider.prepare(
                root=self.root,
                source=self.root / interop.sources.INPUTS / ("contracts-" + interop.SOURCE),
                strategy_file=self.root / interop.sources.INPUTS
                / "docs-a700e639585c61a4610e7b99dbd02b2dab28bdcc/governance/test-strategy.json",
            )
            database = responses.database.prepare(
                self.root, responses.installation, interop.sources, fetch=True,
            )
            self.assertEqual((money["requests"], database["requests"]), (17, 28))
            try:
                result = interop.prepare(self.root)
            finally:
                print(interop.json_bytes({
                    "event": "cold_public_input_fixture", "money_requests": money["requests"],
                    "database_requests": database["requests"], "api_requests": responses.api_requests,
                    "archive_requests": responses.archive_requests, "http_requests": len(responses.calls),
                    "response_bytes": responses.response_bytes,
                    "authorization_headers": sum(any(name.lower() in {"authorization", "proxy-authorization", "cookie"}
                                                     for name in call["headers"]) for call in responses.calls),
                }).decode("ascii").strip())
        self.assertEqual(result["requests"], 4)
        self.assertEqual((responses.api_requests, responses.archive_requests, len(responses.calls)), (48, 1, 49))
        self.assertTrue(all(call["method"] == "GET" and 0 < call["timeout"] <= interop.sources.REQUEST_SECONDS
                            for call in responses.calls), "bounded-read-only-requests")
        self.assertTrue(all(not any(name.lower() in {"authorization", "proxy-authorization", "cookie"}
                                    for name in call["headers"]) for call in responses.calls), "credential-free-requests")
        interop.sources.verify_inputs(self.root)
        interop.sources.verify_provider(self.root)
        responses.database.verify(self.root, responses.installation, interop.sources)
        interop.verify_sources(self.root)
        interop.verify_acquisition(self.root)
        before = len(responses.calls)
        self.assertEqual(interop.prepare(self.root)["requests"], 0)
        self.assertEqual(len(responses.calls), before)


class ArchiveAcquisitionTest(InteropTest):
    def setUp(self):
        super().setUp()
        self.responses = PublicInputResponses()
        self.data = interop.catalog()
        self.client = interop.sources.ReadOnlyClient(self.data, opener=self.responses)
        self.snapshot = interop.sources.GitSnapshot(self.client, self.data["sources"][0], 335295566)
        reused = {entry["path"] for entry in interop.sources.CATALOG["sources"][0]["files"]}
        self.bindings = {name: value for name, value in interop.source_bindings(self.data).items() if name not in reused}
        self.selected = next(iter(self.bindings))
        self.prefix = "contracts-" + interop.SOURCE + "/"

    def rewrite(self, change):
        destination = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(self.responses.archive)), mode="r:") as original:
            with tarfile.open(fileobj=destination, mode="w", format=tarfile.PAX_FORMAT,
                              pax_headers={"comment": interop.SOURCE}) as output:
                for member in original:
                    value = original.extractfile(member).read() if member.isfile() else b""
                    member.pax_headers = {}
                    for item, content in change(member, value):
                        item.size = len(content)
                        output.addfile(item, io.BytesIO(content))
        return gzip.compress(destination.getvalue(), mtime=0)

    def contents(self, archive):
        return interop.archive_contents(archive, self.snapshot, self.bindings)

    def test_exact_selected_files_are_verified_without_extracting_unselected_members(self):
        before = interop.inventory(self.root)
        values, record = self.contents(self.responses.archive)
        self.assertTrue(values == {name: self.responses.contracts[name] for name in self.bindings}, "exact-source-bytes")
        self.assertEqual((len(values), record["archive_members"]), (29, 40))
        self.assertEqual(record["archive_sha256"], interop.digest(self.responses.archive))
        self.assertEqual(interop.inventory(self.root), before)

    def test_codeload_batch_wrapper_crlf_exports_still_require_the_exact_canonical_blob(self):
        name = "smoke/kotlin/gradlew.bat"
        archive = self.rewrite(lambda member, content: [
            (member, content.replace(b"\n", b"\r\n") if member.name == self.prefix + name else content),
        ])
        values, record = self.contents(archive)
        self.assertTrue(values[name] == self.responses.contracts[name], "exact-canonical-wrapper")
        self.assertEqual(record["archive_canonicalized_paths"], [name])
        interop.sources.check_bytes(values[name], self.bindings[name])
        for tampered in (True, False):
            def change(member, content):
                if member.name == self.prefix + (name if tampered else "generator/golden.json"):
                    content = content.replace(b"\n", b"\r\n")
                    if tampered:
                        content = b"x" + content[1:]
                return [(member, content)]

            with self.subTest(tampered_wrapper=tampered):
                with self.assertRaises((interop.InteropError, interop.sources.MaterializationError)):
                    self.contents(self.rewrite(change))

    def test_compressed_and_unpacked_byte_limits_are_enforced_before_publication(self):
        cases = (
            (b"x" * (interop.sources.MAX_RESPONSE_BYTES + 1), "source-size"),
            (gzip.compress(b"\0" * (interop.sources.MAX_TOTAL_BYTES + 1), mtime=0), "archive-size"),
        )
        for value, cause in cases:
            with self.subTest(control=cause), self.assertRaisesRegex(interop.InteropError, cause):
                self.contents(value)
        exact = gzip.decompress(self.responses.archive).ljust(interop.sources.MAX_TOTAL_BYTES, b"\0")
        self.assertEqual(len(self.contents(gzip.compress(exact, mtime=0))[0]), 29)
        self.assertEqual(interop.inventory(self.root), {})

    def test_empty_corrupt_truncated_concatenated_and_trailing_compression_refuse(self):
        archive = self.responses.archive
        for label, value in (
            ("empty", b""), ("corrupt", b"not-a-compressed-archive"),
            ("truncated", archive[:-1]), ("concatenated", archive + archive), ("trailing", archive + b"x"),
        ):
            with self.subTest(control=label), self.assertRaises(interop.InteropError):
                self.contents(value)
        self.assertEqual(len(self.contents(archive)[0]), 29)

    def test_tar_requires_complete_zero_termination_and_no_hidden_following_content(self):
        raw = gzip.decompress(self.responses.archive)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            last = archive.getmembers()[-1]
        end = last.offset_data + ((last.size + 511) // 512) * 512
        for label, value in (("single-zero-block", raw[:end + 512]), ("trailing-nonzero", raw + b"x"),
                             ("truncated-file", raw[:end - 1]), ("second-tar", raw + raw)):
            with self.subTest(control=label), self.assertRaises((interop.InteropError, interop.sources.MaterializationError)):
                self.contents(gzip.compress(value, mtime=0))

    def test_missing_and_tampered_selected_members_refuse_and_clean_bytes_recover(self):
        for label in ("missing", "tampered"):
            def change(member, content):
                if member.name != self.prefix + self.selected:
                    return [(member, content)]
                return [] if label == "missing" else [(member, bytes([content[0] ^ 1]) + content[1:])]

            with self.subTest(control=label), self.assertRaises((interop.InteropError, interop.sources.MaterializationError)):
                self.contents(self.rewrite(change))
        self.assertEqual(len(self.contents(self.responses.archive)[0]), 29)

    def test_paths_duplicates_links_special_modes_and_unbound_members_refuse(self):
        changes = {
            "traversal": lambda member: setattr(member, "name", self.prefix + "../escape"),
            "absolute": lambda member: setattr(member, "name", "/outside"),
            "backslash": lambda member: setattr(member, "name", self.prefix + "bad\\name"),
            "unbound": lambda member: setattr(member, "name", self.prefix + "unbound-file"),
            "wrong-root": lambda member: setattr(member, "name", "contracts-" + "0" * 40 + "/file"),
            "symlink": lambda member: (setattr(member, "type", tarfile.SYMTYPE), setattr(member, "linkname", "target")),
            "hardlink": lambda member: (setattr(member, "type", tarfile.LNKTYPE), setattr(member, "linkname", "target")),
            "fifo": lambda member: setattr(member, "type", tarfile.FIFOTYPE),
            "special-mode": lambda member: setattr(member, "mode", 0o4755),
            "wrong-executable": lambda member: setattr(member, "mode", 0o755),
            "wrong-comment": lambda member: setattr(member, "pax_headers", {"comment": "0" * 40}),
            "sparse-metadata": lambda member: setattr(member, "pax_headers", {"GNU.sparse.size": "0"}),
        }
        for label, mutate in changes.items():
            def change(member, content):
                if member.name == self.prefix + self.selected:
                    mutate(member)
                return [(member, content)]

            with self.subTest(control=label), self.assertRaises(interop.InteropError):
                self.contents(self.rewrite(change))
        duplicate = self.rewrite(
            lambda member, content: [(member, content)] * (2 if member.name == self.prefix + self.selected else 1),
        )
        with self.assertRaisesRegex(interop.InteropError, "archive-duplicate"):
            self.contents(duplicate)

    def test_tree_blob_identity_is_required_in_addition_to_catalog_sha256(self):
        self.bindings[self.selected] = {**self.bindings[self.selected], "git_blob": "0" * 40}
        with self.assertRaisesRegex(interop.InteropError, "source-blob-binding"):
            self.contents(self.responses.archive)

    def test_binary_reader_reuses_native_framing_redirect_and_byte_guards(self):
        controls = {
            "redirect": lambda response: setattr(response, "geturl", lambda: "https://example.invalid/source"),
            "duplicate-length": lambda response: response.headers.add_header("Content-Length", "1"),
            "short-body": lambda response: response.headers.replace_header("Content-Length", str(len(self.responses.archive) + 1)),
            "oversized-length": lambda response: response.headers.replace_header("Content-Length", str(interop.sources.MAX_RESPONSE_BYTES + 1)),
            "conflicting-framing": lambda response: response.headers.add_header("Transfer-Encoding", "chunked"),
            "encoded-body": lambda response: response.headers.add_header("Content-Encoding", "gzip"),
        }
        for label, mutate in controls.items():
            def open_response(request, timeout):
                response = self.responses.open(request, timeout)
                mutate(response)
                return response

            client = interop.sources.ReadOnlyClient(self.data, opener=mock.Mock(open=open_response))
            with self.subTest(control=label), self.assertRaises((interop.InteropError, interop.sources.MaterializationError)):
                interop.fetch_archive(client)

    def test_binary_reader_accepts_exact_response_bound_and_rejects_plus_one(self):
        for size in (interop.sources.MAX_RESPONSE_BYTES, interop.sources.MAX_RESPONSE_BYTES + 1):
            self.responses.archive = b"x" * size
            client = interop.sources.ReadOnlyClient(self.data, opener=self.responses)
            if size == interop.sources.MAX_RESPONSE_BYTES:
                self.assertEqual(len(interop.fetch_archive(client)), size)
            else:
                with self.assertRaisesRegex(interop.sources.MaterializationError, "source-size"):
                    interop.fetch_archive(client)

    def test_request_overall_deadline_and_aggregate_byte_budgets_are_not_reset(self):
        clock = [0.0]
        budget = interop.sources.Budget(clock=lambda: clock[0])
        client = interop.sources.ReadOnlyClient(self.data, opener=self.responses, budget=budget)
        clock[0] = interop.sources.DEADLINE_SECONDS
        with self.assertRaisesRegex(interop.sources.MaterializationError, "source-deadline"):
            interop.fetch_archive(client)
        budget = interop.sources.Budget()
        budget.requests = interop.sources.MAX_REQUESTS
        client = interop.sources.ReadOnlyClient(self.data, opener=self.responses, budget=budget)
        with self.assertRaisesRegex(interop.sources.MaterializationError, "source-request-limit"):
            interop.fetch_archive(client)
        budget = interop.sources.Budget()
        budget.bytes = interop.sources.MAX_TOTAL_BYTES - len(self.responses.archive) + 1
        client = interop.sources.ReadOnlyClient(self.data, opener=self.responses, budget=budget)
        with self.assertRaisesRegex(interop.InteropError, "source-size"):
            interop.fetch_archive(client)
        clock[0] = 0.0
        budget = interop.sources.Budget(clock=lambda: clock[0])

        def slow_response(request, timeout):
            response = self.responses.open(request, timeout)
            clock[0] = interop.sources.REQUEST_SECONDS
            return response

        client = interop.sources.ReadOnlyClient(self.data, opener=mock.Mock(open=slow_response), budget=budget)
        with self.assertRaisesRegex(interop.sources.MaterializationError, "source-request-deadline"):
            interop.fetch_archive(client)

    def test_rate_refusal_has_no_retry_authentication_or_success_fallback(self):
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        failure = urllib.error.HTTPError(PublicInputResponses.archive_url, 403, "synthetic", headers, io.BytesIO())
        opener = mock.Mock()
        opener.open.side_effect = failure
        client = interop.sources.ReadOnlyClient(self.data, opener=opener)
        with self.assertRaisesRegex(interop.InteropError, "source-rate-exhausted"):
            interop.fetch_archive(client)
        self.assertEqual(opener.open.call_count, 1)
        client.authenticated_local = True
        with self.assertRaisesRegex(interop.InteropError, "acquisition-authentication"):
            interop.fetch_archive(client)
        self.assertEqual(opener.open.call_count, 1)

    def test_chunked_binary_transfer_requires_its_actual_trailer_terminator(self):
        content = self.responses.archive

        class MemoryConnection:
            def __init__(self, raw):
                self.raw = raw

            def makefile(self, *_args):
                return io.BytesIO(self.raw)

        for complete in (True, False):
            wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + hex(len(content))[2:].encode("ascii") + b"\r\n" + content + b"\r\n0\r\n"
                    + (b"\r\n" if complete else b""))
            response = http.client.HTTPResponse(MemoryConnection(wire))
            response.begin()
            response.url = PublicInputResponses.archive_url
            client = interop.sources.ReadOnlyClient(self.data, opener=mock.Mock(open=lambda *_args, **_kwargs: response))
            if complete:
                self.assertTrue(interop.fetch_archive(client) == content, "exact-chunked-body")
            else:
                with self.assertRaisesRegex(interop.InteropError, "source-protocol"):
                    interop.fetch_archive(client)

    def test_native_receipt_rejects_stale_shapes_source_counts_and_nonfinite_budgets(self):
        _, archive = self.contents(self.responses.archive)
        record = {
            "schema": "pennilogic.api-money-client-acquisition/2",
            "source_ref": interop.SOURCE, "source_tree": interop.SOURCE_TREE,
            "catalog_sha256": interop.CATALOG_SHA256, "inputs": 39,
            "mode": "native-public-git-tree-and-archive-get", "requests": 4,
            "api_requests": 3, "archive_requests": 1, "reused_provider_inputs": 10,
            "response_bytes": len(self.responses.archive) + 1, "elapsed_seconds": 1, **archive,
        }
        path = self.root / interop.AREA / "acquisition.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(interop.json_bytes(record))
        interop.verify_acquisition(self.root)
        changes = {
            "schema": "pennilogic.api-money-client-acquisition/1", "source_ref": "0" * 40,
            "source_tree": "0" * 40, "catalog_sha256": "0" * 64,
            "inputs": 0, "requests": 35, "api_requests": True, "archive_requests": 0,
            "reused_provider_inputs": 0, "archive_members": 0, "archive_sha256": "bad",
            "archive_unpacked_bytes": interop.sources.MAX_TOTAL_BYTES + 1, "response_bytes": 0,
            "elapsed_seconds": float("inf"), "mode": "native-public-git-object-get",
        }
        for key, value in changes.items():
            with self.subTest(field=key):
                with mock.patch.object(interop, "read_json", return_value={**record, key: value}):
                    with self.assertRaises(interop.InteropError):
                        interop.verify_acquisition(self.root)
        for value in ({**record, "unexecuted": True}, {key: value for key, value in record.items() if key != "archive_sha256"}):
            with mock.patch.object(interop, "read_json", return_value=value):
                with self.assertRaises(interop.InteropError):
                    interop.verify_acquisition(self.root)


class TransportTest(InteropTest):
    def test_seeded_rows_need_no_invented_names_to_retain_canonical_order(self):
        values = [
            {"wire": {"amount": "synthetic", "currency": ("INR", "JPY", "KWD")[index % 3]}, "minor_units": "synthetic"}
            for index in range(interop.COUNT)
        ]
        for index in range(30):
            values[index]["name"] = "boundary-" + str(index)
        data = {
            "schema_version": 1, "generated_count": 10000, "boundary_count": 30, "count": interop.COUNT,
            "round_trip_sha256": interop.CANONICAL_SHA256, "values": values,
        }
        with mock.patch.object(interop, "read_file", return_value=interop.json_bytes(data)), \
                mock.patch.object(interop, "digest", return_value=interop.CORPUS_SHA256):
            actual = interop.corpus(self.root)
        self.assertEqual(len(actual["values"]), interop.COUNT)
        self.assertEqual(sum("name" in row for row in actual["values"]), 30)

    def test_exact_identity_order_currency_and_strings_are_preserved(self):
        self.assertIs(interop.validate_transport(self.expected, self.expected), self.expected)

    def test_missing_malformed_truncated_duplicate_and_nonfinite_json_is_refused(self):
        target = self.root / "input.json"
        with self.assertRaises(FileNotFoundError):
            interop.read_json(target)
        for content in (
            b"", b"{", b'{"cases":[]', b'{"schema":1,"schema":1}',
            b'{"cases":[{"envelope":{"total":{"amount":"first","amount":"second"}}}]}',
            b'{"count":NaN}', b'{"count":Infinity}', b"\xef\xbb\xbf{}", b"\xff",
        ):
            with self.subTest(size=len(content)):
                target.write_bytes(content)
                with self.assertRaises(interop.sources.MaterializationError):
                    interop.read_json(target)

    def test_wrong_version_source_fixture_and_stale_run_are_refused(self):
        for field in ("schema", "source_ref", "fixture_sha256", "run_id"):
            with self.subTest(field=field):
                value = copy.deepcopy(self.expected)
                value[field] = "wrong"
                self.rejected(value, "transport-" + field.replace("_", "-"))

    def test_missing_extra_and_empty_evidence_is_refused(self):
        value = copy.deepcopy(self.expected)
        del value["cases"]
        self.rejected(value, "transport-shape")
        value = copy.deepcopy(self.expected)
        value["skipped"] = 0
        self.rejected(value, "transport-shape")
        for rows in ([], self.expected["cases"][:1], self.expected["cases"] * 2):
            value = copy.deepcopy(self.expected)
            value["cases"] = rows
            self.rejected(value, "transport-count")

    def test_case_counters_must_be_exact_integers_not_boolean_or_string(self):
        for count in (0, -1, True, "2", 2.0):
            value = copy.deepcopy(self.expected)
            value["case_count"] = count
            self.rejected(value, "transport-case-count")

    def test_reordering_or_duplicate_identity_is_not_a_round_trip(self):
        value = copy.deepcopy(self.expected)
        value["cases"].reverse()
        self.rejected(value, "case-order")
        value = copy.deepcopy(self.expected)
        value["cases"][1]["id"] = value["cases"][0]["id"]
        self.rejected(value, "case-order")

    def test_changed_value_currency_or_numeric_money_is_not_equality(self):
        for field, replacement in (("amount", "different"), ("currency", "KWD"), ("amount", 1), ("amount", None)):
            with self.subTest(field=field, replacement_type=type(replacement).__name__):
                value = copy.deepcopy(self.expected)
                value["cases"][0]["envelope"]["total"][field] = replacement
                self.rejected(value, "wire-disagreement")

    def test_missing_money_time_and_extra_envelope_members_are_refused(self):
        for key in ("total", "recorded_at", "booked_on"):
            value = copy.deepcopy(self.expected)
            del value["cases"][0]["envelope"][key]
            self.rejected(value, "envelope-shape")
        value = copy.deepcopy(self.expected)
        value["cases"][0]["envelope"]["unowned"] = "extra"
        self.rejected(value, "envelope-shape")

    def test_disagreement_mode_does_not_relax_identity_or_cardinality(self):
        value = copy.deepcopy(self.expected)
        value["cases"][0]["envelope"]["total"] = value["cases"][1]["envelope"]["total"]
        interop.validate_transport(value, self.expected, compare_values=False)
        value["cases"][0]["id"] = "wrong"
        self.rejected(value, "case-order", compare_values=False)

    def test_all_rejections_must_execute_and_retain_every_case_identity(self):
        rejected = copy.deepcopy(self.expected)
        rejected["schema"] = interop.REJECTIONS_SCHEMA
        rejected["cases"] = [{"id": row["id"], "status": "rejected"} for row in rejected["cases"]]
        interop.validate_transport(rejected, self.expected, rejections=True)
        for status in ("skipped", "passed", None, False):
            value = copy.deepcopy(rejected)
            value["cases"][0]["status"] = status
            self.rejected(value, "case-unexecuted", rejections=True)

    def test_no_zero_case_transport_can_qualify_itself(self):
        empty = interop.transport(self.run_id, [])
        with self.assertRaisesRegex(interop.InteropError, "transport-count"):
            interop.validate_transport(empty, empty)


class RetainedTransportTest(InteropTest):
    """Small retained-data controls, not generated-client or runtime-execution evidence."""

    def setUp(self):
        super().setUp()
        self.values = interop.corpus(ROOT)["values"][:2]
        self.expected = interop.transport(self.run_id, self.values)
        self.invalid_vectors = interop.read_json(
            ROOT / interop.AREA / "inputs/spec/fixtures/money-wire-fixtures.v1.json",
        )
        interop.write_json(self.root, interop.AREA / "inputs/spec/fixtures/money-wire-fixtures.v1.json",
                           self.invalid_vectors)
        self.invalid = interop.transport(self.run_id, self.invalid_vectors["invalid"], interop.INVALID_SHA256)
        self.directory = Path("retained-data-controls")
        self.plant = copy.deepcopy(self.expected)
        self.plant["cases"][0]["envelope"]["total"] = copy.deepcopy(self.expected["cases"][1]["envelope"]["total"])
        self.write("backend.json", self.expected)
        self.write("invalid.json", self.invalid)
        rejected = {**self.invalid, "schema": interop.REJECTIONS_SCHEMA,
                    "cases": [{"id": row["id"], "status": "rejected"} for row in self.invalid["cases"]]}
        for language in interop.LANGUAGES:
            self.write(language + ".json", self.expected)
            self.write(language + "-recovered.json", self.expected)
            self.write(language + "-rejected.json", rejected)
            self.write(language + "-disagreement.json", self.plant)

    def write(self, name, value):
        interop.write_json(self.root, self.directory / name, value, replace=True)

    def verify_retained(self):
        with mock.patch.object(interop, "corpus", return_value={"values": self.values}):
            interop.verify_transports(self.root, self.directory, self.run_id)

    def test_exact_money_field_plant_and_clean_recovery_are_retained(self):
        self.verify_retained()

    def test_nested_json_types_never_conflate_boolean_integer_and_float_neighbors(self):
        groups = ((True, 1, 1.0), (False, 0, 0.0))
        for group in groups:
            for original in group:
                for replacement in group:
                    if type(original) is type(replacement):
                        continue
                    with self.subTest(original_type=type(original).__name__, replacement_type=type(replacement).__name__):
                        expected = copy.deepcopy(self.expected)
                        expected["cases"][0]["envelope"]["total"]["amount"] = original
                        changed = copy.deepcopy(expected)
                        changed["cases"][0]["envelope"]["total"]["amount"] = replacement
                        with self.assertRaisesRegex(interop.InteropError, "wire-disagreement"):
                            interop.validate_transport(changed, expected)

    def test_retained_invalid_boolean_cannot_be_replaced_by_equal_numbers(self):
        for replacement in (1, 1.0):
            changed = copy.deepcopy(self.invalid)
            self.assertIs(changed["cases"][3]["envelope"]["total"]["amount"], True)
            changed["cases"][3]["envelope"]["total"]["amount"] = replacement
            self.write("invalid.json", changed)
            with self.subTest(replacement_type=type(replacement).__name__):
                with self.assertRaisesRegex(interop.InteropError, "wire-disagreement"):
                    self.verify_retained()
        self.write("invalid.json", self.invalid)
        self.verify_retained()

    def test_retained_disagreement_must_be_the_exact_money_field_plant(self):
        mutations = {
            "unchanged-money": lambda row: row.update(copy.deepcopy(self.expected["cases"][0]["envelope"])),
            "date-only": lambda row: row.update({**self.expected["cases"][0]["envelope"], "booked_on": "2026-10-01"}),
            "plant-plus-date": lambda row: row.update(booked_on="2026-10-01"),
            "numeric-total": lambda row: row.update(total=1),
            "boolean-total": lambda row: row.update(total=True),
            "null-total": lambda row: row.update(total=None),
            "array-total": lambda row: row.update(total=[]),
            "numeric-amount": lambda row: row["total"].update(amount=1),
            "missing-currency": lambda row: row["total"].pop("currency"),
        }
        for label, mutate in mutations.items():
            changed = copy.deepcopy(self.plant)
            mutate(changed["cases"][0]["envelope"])
            self.write("kotlin-disagreement.json", changed)
            with self.subTest(control=label):
                with self.assertRaisesRegex(interop.InteropError, "fault-not-planted"):
                    self.verify_retained()
        self.write("kotlin-disagreement.json", self.plant)
        self.verify_retained()

    def test_retained_disagreement_cannot_change_later_rows_or_their_types(self):
        for replacement in (True, 1, 1.0, [], None):
            changed = copy.deepcopy(self.plant)
            changed["cases"][1]["envelope"]["total"]["amount"] = replacement
            self.write("python-disagreement.json", changed)
            with self.subTest(replacement_type=type(replacement).__name__):
                with self.assertRaisesRegex(interop.InteropError, "fault-scope"):
                    self.verify_retained()
        self.write("python-disagreement.json", self.plant)
        self.verify_retained()


class RunnerBoundaryTest(InteropTest):
    def test_failed_typescript_install_emits_only_bounded_allowlisted_diagnostics(self):
        runner = interop.Runner(self.root, Path("install-failure-control"))
        stdout = b"synthetic-private-package-output"
        stderr = b"npm error code EBADENGINE\nsynthetic-private-path-or-value\n"
        result = subprocess.CompletedProcess(["npm"], 1, stdout, stderr)
        public = io.StringIO()
        with mock.patch.object(interop.process_budget, "run", return_value=result), \
                contextlib.redirect_stderr(public), \
                self.assertRaisesRegex(interop.InteropError, "^command-failed-typescript-install$"):
            runner.call("typescript-install", ["approved-node", "approved-npm", "ci"])
        self.assertTrue(public.getvalue(), "installer-failure-diagnostic-missing")
        self.assertEqual(json.loads(public.getvalue()), {
            "event": "money_client_dependency_failure", "status": "refused",
            "command": "typescript-install", "exit_code": 1, "npm_code": "EBADENGINE",
            "stdout": {"bytes": len(stdout), "sha256": interop.digest(stdout)},
            "stderr": {"bytes": len(stderr), "sha256": interop.digest(stderr)},
        })
        self.assertNotIn("synthetic-private", public.getvalue())
        self.assertNotIn(str(self.root), public.getvalue())

    def test_npm_diagnostics_never_echo_unknown_ambiguous_or_injected_codes(self):
        for index, stderr in enumerate((
            b"npm error code SYNTHETIC_PRIVATE\n",
            b"npm error code EBADENGINE trailing-private-data\n",
            b"prefix npm error code EBADENGINE\n",
            b"npm error code EBADENGINE\nnpm error code EACCES\n",
            b"npm error code \x1b[31mEBADENGINE\x1b[0m\n",
            b"\xff\n",
        )):
            runner = interop.Runner(self.root, Path("unclassified-install-" + str(index)))
            public = io.StringIO()
            result = subprocess.CompletedProcess(["npm"], 1, b"", stderr)
            with self.subTest(control=index), \
                    mock.patch.object(interop.process_budget, "run", return_value=result), \
                    contextlib.redirect_stderr(public), \
                    self.assertRaisesRegex(interop.InteropError, "^command-failed-typescript-install$"):
                runner.call("typescript-install", ["approved-node", "approved-npm", "ci"])
            self.assertTrue(public.getvalue(), "unclassified-install-diagnostic-missing")
            diagnostic = json.loads(public.getvalue())
            self.assertEqual(diagnostic["npm_code"], "unclassified")
            self.assertNotIn("PRIVATE", public.getvalue())
            self.assertNotIn("\x1b", public.getvalue())

    def test_dependency_diagnostics_do_not_change_success_or_other_command_protocols(self):
        for label, result in (
            ("typescript-install", subprocess.CompletedProcess(["npm"], 0, b"", b"npm error code EBADENGINE\n")),
            ("python-install", subprocess.CompletedProcess(["python"], 1, b"", b"npm error code EBADENGINE\n")),
        ):
            runner = interop.Runner(self.root, Path("diagnostic-scope-" + label))
            public = io.StringIO()
            with mock.patch.object(interop.process_budget, "run", return_value=result), contextlib.redirect_stderr(public):
                if result.returncode:
                    with self.assertRaisesRegex(interop.InteropError, "^command-failed-python-install$"):
                        runner.call(label, ["approved-runtime"])
                else:
                    self.assertEqual(runner.call(label, ["approved-runtime"])[0], 0)
            self.assertEqual(public.getvalue(), "")

    def test_npm_diagnostics_accept_exact_modern_legacy_and_repeated_code_lines(self):
        for index, (stderr, expected) in enumerate((
            (b"npm ERR! code EBADENGINE\r\n", "EBADENGINE"),
            (b"npm error code EINTEGRITY\n", "EINTEGRITY"),
            (b"npm error code E404\n", "E404"),
            (b"npm error code EBADENGINE\nnpm error code EBADENGINE\n", "EBADENGINE"),
        )):
            runner = interop.Runner(self.root, Path("npm-code-lines-" + str(index)))
            public = io.StringIO()
            result = subprocess.CompletedProcess(["npm"], 1, b"", stderr)
            with self.subTest(control=index), \
                    mock.patch.object(interop.process_budget, "run", return_value=result), \
                    contextlib.redirect_stderr(public), \
                    self.assertRaisesRegex(interop.InteropError, "^command-failed-typescript-install$"):
                runner.call("typescript-install", ["approved-node", "approved-npm", "ci"])
            self.assertEqual(json.loads(public.getvalue())["npm_code"], expected)

    def test_generated_kotlin_is_compiled_not_restored_from_an_executable_build_cache(self):
        directory = Path(self.run_id)
        interop.write_json(self.root, directory / "kotlin-launch.json", {"classpath": [str(self.root)]})
        runner = mock.Mock()
        runner.call.return_value = (0, b"> Task :compileKotlin\n", b"")
        interop.compile_kotlin(self.root, directory, runner, self.root / "producer", "approved-java")
        self.assertEqual(runner.call.call_args.args[0], "compile-kotlin")
        arguments = runner.call.call_args.args[1]
        self.assertIn("--no-build-cache", arguments)
        self.assertIn("--rerun-tasks", arguments)

    def test_zero_exit_without_actual_kotlin_compilation_is_refused(self):
        directory = Path(self.run_id)
        runner = mock.Mock()
        for suffix in (b" FROM-CACHE", b" UP-TO-DATE", b" SKIPPED", b" NO-SOURCE"):
            runner.call.return_value = (0, b"> Task :compileKotlin" + suffix + b"\n", b"")
            with self.subTest(state=suffix.decode("ascii")):
                with self.assertRaisesRegex(interop.InteropError, "command-compiler-unexecuted"):
                    interop.compile_kotlin(self.root, directory, runner, self.root / "producer", "approved-java")

    def test_exact_stream_and_aggregate_limits_preserve_separate_bytes_and_recover(self):
        runner = interop.Runner(self.root, Path("exact-output-control"))
        for label, stdout, stderr in (("stdout-bound", interop.MAX_DOCUMENT, 0),
                                      ("aggregate-bound", interop.MAX_DOCUMENT // 2, interop.MAX_DOCUMENT // 2)):
            code = f"import os;os.write(1,b'a'*{stdout});os.write(2,b'b'*{stderr})"
            result = runner.call(label, [sys.executable, "-I", "-S", "-B", "-c", code])
            self.assertEqual((len(result[1]), len(result[2])), (stdout, stderr))
        result = runner.call("recovery", [sys.executable, "-I", "-S", "-B", "-c", "print('{}')"],
                             protocol=({}, None))
        self.assertEqual(result[0], 0)

    def test_timeout_retains_only_bounded_metadata_and_clean_recovery(self):
        runner = interop.Runner(self.root, Path("timeout-output-control"))
        runner.started = interop.time.monotonic() - interop.SECONDS + 0.5
        with self.assertRaisesRegex(interop.InteropError, "process-budget"):
            runner.call("timeout", [sys.executable, "-I", "-S", "-B", "-c", "import time;time.sleep(30)"])
        self.assertFalse(runner.records[0]["capture_complete"])
        self.assertIsNone(runner.records[0]["stdout"]["path"])
        runner.started = interop.time.monotonic()
        self.assertEqual(runner.call("recovery", [sys.executable, "-I", "-S", "-B", "-c", "print('{}')"],
                                     protocol=({}, None))[0], 0)

    def test_partial_fresh_runtime_storage_is_preserved_before_any_execution(self):
        base = self.root / interop.AREA / "runtimes" / self.run_id
        base.mkdir(parents=True)
        (base / "preserve.txt").write_bytes(b"partial")
        runner = mock.Mock()
        with self.assertRaisesRegex(interop.InteropError, "runtime-storage-exists"):
            interop.runtime_dependencies(self.root, Path(self.run_id), runner, self.root, {})
        runner.call.assert_not_called()
        self.assertEqual((base / "preserve.txt").read_bytes(), b"partial")

    def test_runtime_children_have_a_positive_environment_boundary(self):
        runner = interop.Runner(self.root, Path("environment-control"))
        injected = {
            "JAVA_TOOL_OPTIONS": "synthetic-options", "_JAVA_OPTIONS": "synthetic-options",
            "JDK_JAVA_OPTIONS": "synthetic-options", "NODE_OPTIONS": "synthetic-options",
            "NODE_PATH": str(self.root), "PYTHONPATH": str(self.root), "PYTHONSTARTUP": str(self.root),
            "AWS_SECRET_ACCESS_KEY": "synthetic-noncredential", "NPM_TOKEN": "synthetic-noncredential",
            "UNLISTED_LAUNCHER_SETTING": "synthetic-unapproved",
        }
        code = (
            "import os,json; print(json.dumps({'clean':not any(name in os.environ for name in "
            + repr(tuple(injected)) + ")}))"
        )
        with mock.patch.dict(os.environ, injected):
            result = runner.call("environment", [sys.executable, "-I", "-S", "-B", "-c", code],
                                 protocol=({"clean": True}, None))
        self.assertEqual(result[0], 0)

    def test_stream_overflow_stops_owned_work_before_delayed_continuation(self):
        runner = interop.Runner(self.root, Path("overflow-control"))
        marker = self.root / "overflow-continuation"
        code = (
            "import os,time,pathlib; "
            f"os.write(1,b'x'*{interop.MAX_DOCUMENT + 1}); "
            f"time.sleep(1); pathlib.Path({str(marker)!r}).write_text('unexpected')"
        )
        with self.assertRaisesRegex(interop.InteropError, "command-output-size"):
            runner.call("overflow", [sys.executable, "-I", "-S", "-B", "-c", code])
        self.assertFalse(marker.exists(), "overflow-was-only-checked-after-exit")
        self.assertTrue(all(record[name]["path"] is None for record in runner.records
                            for name in ("stdout", "stderr")), "overflow-must-be-hash-only")

    def test_concurrent_streams_have_an_aggregate_limit(self):
        runner = interop.Runner(self.root, Path("aggregate-control"))
        code = (
            "import os,threading; "
            f"threads=[threading.Thread(target=os.write,args=(fd,b'x'*{interop.MAX_DOCUMENT // 2 + 1})) for fd in (1,2)]; "
            "[thread.start() for thread in threads]; [thread.join() for thread in threads]"
        )
        with self.assertRaisesRegex(interop.InteropError, "command-output-size"):
            runner.call("aggregate", [sys.executable, "-I", "-S", "-B", "-c", code])

    def test_old_dependency_caches_are_not_executed_as_readiness_probes(self):
        producer = self.root / interop.AREA / "producer"
        requirement = Path("smoke/python/requirements.txt")
        interop.write_file(self.root, interop.AREA / "producer" / requirement,
                           interop.read_file(ROOT / interop.AREA / "inputs" / requirement))
        interpreter = self.root / interop.AREA / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"untrusted-cache-control")
        compiler = producer / "node_modules/typescript/bin/tsc"
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(b"untrusted-cache-control")

        class TrustedRestoreReached(Exception):
            pass

        def guard(label, argv, **_kwargs):
            self.assertNotEqual(Path(argv[0]), interpreter, "untrusted-python-cache-was-probed")
            self.assertFalse(any(str(compiler) == str(arg) for arg in argv), "untrusted-compiler-cache-was-probed")
            if label == "python-probe":
                self.assertEqual(Path(argv[0]), Path(sys.executable))
                return 3, b"", b""
            if label == "python-venv":
                self.assertEqual(Path(argv[0]), Path(sys.executable))
                raise TrustedRestoreReached
            self.fail("unexpected-readiness-command")

        with self.assertRaises(TrustedRestoreReached):
            interop.runtime_dependencies(
                self.root, Path(self.run_id), mock.Mock(call=guard), producer,
                interop.read_json(ROOT / interop.AREA / "inputs/toolchain/versions.json"),
            )
        self.assertEqual(interpreter.read_bytes(), b"untrusted-cache-control")
        self.assertEqual(compiler.read_bytes(), b"untrusted-cache-control")


class ExecutionEvidenceTest(InteropTest):
    def test_stored_command_records_preserve_boolean_and_numeric_types(self):
        records = self.records()
        directory = Path("typed-records")
        for name in ("stdout", "stderr"):
            records[0][name]["path"] = (directory / "commands" / f"00-generator-golden.{name}.txt").as_posix()
        for field, value in (("executed", 1), ("readiness_probe", 0), ("exit_code", 0.0)):
            changed = {**records[0], field: value}
            with self.subTest(field=field), \
                    mock.patch.object(interop, "read_json", return_value=changed), \
                    mock.patch.object(interop, "read_file", side_effect=AssertionError("untyped-record-reached-streams")):
                with self.assertRaisesRegex(interop.InteropError, "command-record-binding"):
                    interop.verify_commands(self.root, directory, records, self.run_id)

    def test_retained_binding_maps_preserve_json_numeric_types(self):
        bound = {"fixture": {"bytes": 0, "sha256": interop.digest(b"")}}
        baseline = {
            "scope": "api-test-only-generated-model-round-trip", "release_consumed": False,
            "source_ref": interop.SOURCE, "source_tree": interop.SOURCE_TREE,
            "catalog_sha256": interop.CATALOG_SHA256, "spec_version": "0.1.0",
            "corpus_sha256": interop.CORPUS_SHA256, "canonical_sha256": interop.CANONICAL_SHA256,
            "case_count": interop.COUNT, "generated_count": 10000, "boundary_count": 30,
            "invalid_count": interop.INVALID_COUNT, "currencies": ["INR", "JPY", "KWD"],
            "default_money_model_emitted": False, "run_id": self.run_id, "commands": [],
            **{field: copy.deepcopy(bound) for field in ("inputs", "backend_classpath", "client_classpath", "outputs")},
        }
        causes = {
            "inputs": "evidence-stale-inputs", "backend_classpath": "evidence-backend",
            "client_classpath": "evidence-client", "outputs": "evidence-outputs",
        }
        for field, cause in causes.items():
            for value in (False, 0.0):
                report = copy.deepcopy(baseline)
                report[field]["fixture"]["bytes"] = value
                with self.subTest(field=field, replacement_type=type(value).__name__), contextlib.ExitStack() as stack:
                    replacements = {
                        "verify_sources": None, "completion": (Path("typed-bindings"), report),
                        "api_inputs": bound, "backend_launch": {"classpath": ["fixture"]},
                        "classpath_bindings": bound, "runtime_inputs": {},
                        "inventory": {"report.json": {}, **bound},
                    }
                    for name, result in replacements.items():
                        stack.enter_context(mock.patch.object(interop, name, return_value=result))
                    stack.enter_context(mock.patch.object(interop.sources, "verify_provider"))
                    stack.enter_context(mock.patch.object(
                        interop, "read_json",
                        side_effect=lambda path: {"classpath": ["fixture"]} if path.name == "kotlin-launch.json" else {},
                    ))
                    stack.enter_context(mock.patch.object(
                        interop, "verify_commands", side_effect=AssertionError("untyped-bindings-reached-commands"),
                    ))
                    with self.assertRaisesRegex(interop.InteropError, cause):
                        interop.verify(self.root)

    def test_retained_command_streams_obey_the_same_aggregate_output_limit(self):
        records = self.records()
        records[0]["stdout"]["bytes"] = records[0]["stderr"]["bytes"] = interop.MAX_DOCUMENT // 2
        interop.validate_commands(records)
        records[0]["stderr"]["bytes"] += 1
        with self.assertRaisesRegex(interop.InteropError, "commands-stream"):
            interop.validate_commands(records)

    def records(self):
        return [
            {
                "label": label, "executed": True, "readiness_probe": False,
                "elapsed_seconds": 0.01,
                "exit_code": 1 if label.endswith("-refuse-disagreement") else 0,
                "expected_exit": 1 if label.endswith("-refuse-disagreement") else 0,
                "argv": ["synthetic-command", "--no-build-cache", "--rerun-tasks"], "cwd": str(self.root),
                "environment": {},
                "stdout": {"path": "synthetic.stdout.txt", "bytes": 0, "sha256": interop.digest(b"")},
                "stderr": {"path": "synthetic.stderr.txt", "bytes": 0, "sha256": interop.digest(b"")},
            }
            for label in interop.required_commands()
        ]

    def test_bounded_reads_allocate_for_the_file_not_the_maximum_jar_allowance(self):
        path = self.root / "fixture.txt"
        path.write_bytes(b"fixture")
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"fixture"
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(interop.read_file(path, 256 * 1024 * 1024), b"fixture")
        stream.read.assert_called_once_with(8)

    def test_a_file_growing_or_shrinking_during_a_read_is_refused(self):
        path = self.root / "fixture.txt"
        path.write_bytes(b"fixture")
        for result in (b"short", b"fixture-extra"):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.read.return_value = result
            with mock.patch.object(Path, "open", return_value=stream):
                with self.assertRaisesRegex(interop.InteropError, "input-changed"):
                    interop.read_file(path)

    def test_all_three_real_execution_legs_and_recoveries_are_mandatory(self):
        records = self.records()
        interop.validate_commands(records)
        for index in range(len(records)):
            with self.subTest(index=index):
                missing = records[:index] + records[index + 1:]
                with self.assertRaisesRegex(interop.InteropError, "commands-unexecuted"):
                    interop.validate_commands(missing)

    def test_unexecuted_failed_skipped_or_duplicate_commands_are_refused(self):
        for field, value in (("executed", False), ("exit_code", 1), ("exit_code", None), ("readiness_probe", True)):
            records = self.records()
            records[0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(interop.InteropError):
                    interop.validate_commands(records)
        records = self.records()
        records.append(records[0])
        with self.assertRaisesRegex(interop.InteropError, "commands-duplicate"):
            interop.validate_commands(records)
        records = self.records()
        records[0]["skipped"] = True
        with self.assertRaisesRegex(interop.InteropError, "commands-shape"):
            interop.validate_commands(records)

    def test_a_self_consistent_digest_cannot_hide_zero_or_skipped_runtime_receipts(self):
        interop.write_json(self.root, interop.AREA / "inputs/toolchain/versions.json",
                           {"typescript": {"version": "7.0.2"}})
        for change in ({"case_count": 0}, {"case_count": 10030.0}, {"status": "skipped"}, {"run_id": "b" * 32}, {"extra": "field"}):
            with self.subTest(field=next(iter(change))):
                directory = Path("probe-" + str(len(list(self.root.iterdir()))))
                records = self.records()
                for index, record in enumerate(records):
                    prefix = directory / "commands" / f"{index:02d}-{record['label']}"
                    for name in ("stdout", "stderr"):
                        content = b""
                        if record["label"] == "python-ready" and name == "stdout":
                            content = interop.json_bytes({"missing": 0, "wrong": 0})
                        if record["label"] == "typescript-ready" and name == "stdout":
                            content = b"Version 7.0.2\n"
                        if record["label"] == "compile-kotlin" and name == "stdout":
                            content = b"> Task :compileKotlin\n"
                        if record["label"] == "backend-emit" and name == "stdout":
                            value = {**interop.receipt("backend", "emit", interop.COUNT, self.run_id), **change}
                            content = interop.json_bytes(value)
                        path = Path(str(prefix) + "." + name + ".txt")
                        interop.write_file(self.root, path, content)
                        record[name] = {"path": path.as_posix(), "bytes": len(content), "sha256": interop.digest(content)}
                    interop.write_json(self.root, Path(str(prefix) + ".json"), record)
                with self.assertRaisesRegex(interop.InteropError, "command-receipt"):
                    interop.verify_commands(self.root, directory, records, self.run_id)

    def test_protocol_comparison_does_not_conflate_booleans_floats_and_integers(self):
        self.assertTrue(interop.Runner.matches(b'{"count":0}', {"count": 0}))
        for content in (b'{"count":false}', b'{"count":0.0}', b'{"count":"0"}'):
            self.assertFalse(interop.Runner.matches(content, {"count": 0}))

    def test_planted_disagreement_must_fail_not_merely_exit(self):
        records = self.records()
        failure = next(record for record in records if record["label"].endswith("-refuse-disagreement"))
        failure["exit_code"] = failure["expected_exit"] = 0
        with self.assertRaisesRegex(interop.InteropError, "commands-failed"):
            interop.validate_commands(records)

    def test_invalid_command_timings_are_refused(self):
        for duration in (-1, True, float("inf"), float("nan"), 600, None):
            records = self.records()
            records[0]["elapsed_seconds"] = duration
            with self.assertRaisesRegex(interop.InteropError, "commands-duration"):
                interop.validate_commands(records)

    def test_runtime_errors_cannot_copy_raw_payloads_into_logs_or_reports(self):
        runner = interop.Runner(self.root, Path("run"))
        secret = b"synthetic-must-not-appear-in-evidence"
        result = subprocess.CompletedProcess(["bridge"], 1, secret, secret)
        with mock.patch.object(interop.process_budget, "run", return_value=result):
            with self.assertRaisesRegex(interop.InteropError, "command-protocol"):
                runner.call("conversion", ["bridge"], protocol=({"status": "passed"}, None))
        self.assertIsNone(runner.records[0]["stdout"]["path"])
        self.assertIsNone(runner.records[0]["stderr"]["path"])
        self.assertEqual(runner.records[0]["stdout"]["sha256"], interop.digest(secret))
        for path in (self.root / "run").rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes())

    def test_a_missing_command_is_not_reported_as_executed_or_successful(self):
        runner = interop.Runner(self.root, Path("run"))
        with mock.patch.object(interop.process_budget, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(interop.InteropError, "command-failed-missing"):
                runner.call("missing", ["not-an-installed-tool"])
        self.assertFalse(runner.records[0]["executed"])
        self.assertIsNone(runner.records[0]["exit_code"])

    def test_failure_invalidates_previous_success_before_execution(self):
        prior = self.root / interop.AREA / "current.json"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(interop.json_bytes({"schema": interop.REPORT_SCHEMA, "status": "passed", "run_id": "b" * 32}))
        result = subprocess.CompletedProcess(["worker"], 1)
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", return_value=result), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        current = interop.read_json(prior)
        self.assertEqual(current["status"], "refused")
        self.assertNotEqual(current["run_id"], "b" * 32)

    def test_an_unexecuted_worker_cannot_qualify_by_returning_zero(self):
        result = subprocess.CompletedProcess(["worker"], 0)
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", return_value=result), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json")["status"], "refused")

    def test_a_second_writer_preserves_the_active_run_and_its_lock(self):
        with interop.exclusive_run(self.root, self.run_id):
            initial = {"schema": interop.REPORT_SCHEMA, "status": "running", "run_id": self.run_id}
            interop.write_json(self.root, interop.AREA / "current.json", initial)
            with mock.patch.object(interop, "ROOT", self.root), \
                    mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(interop.main(), 1)
            self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json"), initial)
            self.assertEqual(interop.read_json(self.root / interop.AREA / "run.lock"), {"run_id": self.run_id})
        self.assertFalse((self.root / interop.AREA / "run.lock").exists())

    def test_a_stale_lock_is_refused_not_deleted_or_reused(self):
        interop.write_json(self.root, interop.AREA / "run.lock", {"run_id": "b" * 32})
        with self.assertRaisesRegex(interop.InteropError, "run-busy-or-stale-lock"):
            with interop.exclusive_run(self.root, self.run_id):
                self.fail("A stale lock was reused")
        self.assertEqual(interop.read_json(self.root / interop.AREA / "run.lock"), {"run_id": "b" * 32})

    def test_timeout_invalidates_evidence_without_touching_another_process(self):
        with mock.patch.object(interop, "ROOT", self.root), \
                mock.patch.object(sys, "argv", ["money_client_interop.py", "run"]), \
                mock.patch.object(interop.process_budget, "run", side_effect=interop.process_budget.BudgetExceeded(None)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(interop.main(), 1)
        self.assertEqual(interop.read_json(self.root / interop.AREA / "current.json")["status"], "refused")

    def test_linked_hardlinked_and_escaping_evidence_is_refused(self):
        original = self.root / "original.json"
        original.write_bytes(b"{}")
        os.link(original, self.root / "linked.json")
        with self.assertRaisesRegex(interop.InteropError, "input-kind"):
            interop.read_json(original)
        with self.assertRaises(interop.sources.MaterializationError):
            interop.owned(self.root, Path("../outside"))
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(interop.sources.MaterializationError):
                interop.owned(self.root, Path("output.json"))

    def test_empty_missing_or_duplicate_classpaths_cannot_qualify_a_backend(self):
        for entries in ([], ["relative.jar"], [str(self.root / "absent.jar")], [str(self.root), str(self.root)]):
            with self.assertRaises(interop.InteropError):
                interop.classpath(entries)


if __name__ == "__main__":
    unittest.main()

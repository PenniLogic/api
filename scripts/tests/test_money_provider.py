import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("money_provider", Path(__file__).resolve().parents[1] / "money_provider.py")
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class MoneyProviderBindingTest(unittest.TestCase):
    def test_missing_provider_has_no_currency_or_codec_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(provider.ProviderError, "not prepared"):
                provider.prepare(root=Path(temporary))

    def test_relative_source_selector_is_normalized_before_renderer_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = Path(os.path.relpath(root, Path.cwd()))
            with patch.object(provider, "read_source", side_effect=provider.ProviderError("bounded stop")) as reader:
                with self.assertRaisesRegex(provider.ProviderError, "bounded stop"):
                    provider.prepare(source=relative, root=root)
                self.assertTrue(reader.call_args.args[0].is_absolute())

    def test_unreadable_inventory_reports_no_success_or_error_content(self):
        marker = "synthetic-private-error-marker"
        with self.assertRaisesRegex(provider.ProviderError, "inventory is unreadable") as context:
            provider.refused_walk(OSError(marker))
        self.assertNotIn(marker, str(context.exception))

    def test_source_hash_or_size_mismatch_refuses_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scripts/Synthetic.py"
            path.parent.mkdir()
            expected = b"synthetic approved input"
            path.write_bytes(expected)
            catalog = {"scripts/Synthetic.py": (len(expected), provider.digest(expected))}
            with patch.object(provider, "SOURCE_FILES", catalog):
                self.assertEqual(expected, provider.read_source(root)["scripts/Synthetic.py"])
                altered = b"synthetic changed input"
                path.write_bytes(altered)
                with self.assertRaisesRegex(provider.ProviderError, "source mismatch"):
                    provider.read_source(root)
                self.assertEqual(altered, path.read_bytes())

    def test_extra_python_or_cache_file_cannot_shadow_verified_renderer_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scripts/Synthetic.py"
            path.parent.mkdir()
            content = b"synthetic approved input"
            path.write_bytes(content)
            catalog = {"scripts/Synthetic.py": (len(content), provider.digest(content))}
            with patch.object(provider, "SOURCE_FILES", catalog):
                for relative in ("scripts/hashlib.py", "scripts/__pycache__/Synthetic.pyc"):
                    extra = root / relative
                    extra.parent.mkdir(parents=True, exist_ok=True)
                    extra.write_bytes(b"synthetic unpinned input")
                    with self.assertRaisesRegex(provider.ProviderError, "Unpinned"):
                        provider.read_source(root)
                    extra.unlink()

    def test_extra_generated_source_and_modified_cache_both_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / provider.BUNDLE
            source = bundle / "source/scripts/Synthetic.py"
            source.parent.mkdir(parents=True)
            content = b"synthetic approved input"
            source.write_bytes(content)
            catalog = {"scripts/Synthetic.py": (len(content), provider.digest(content))}
            outputs = {"Synthetic.kt": provider.digest(content)}
            output = bundle / "kotlin/Synthetic.kt"
            output.parent.mkdir()
            output.write_bytes(content)
            with patch.object(provider, "SOURCE_FILES", catalog), patch.object(provider, "OUTPUT_HASHES", outputs):
                self.assertEqual([output], provider.verify_outputs(root))
                extra = output.with_name("Injected.kt")
                extra.write_bytes(b"synthetic injected source")
                with self.assertRaisesRegex(provider.ProviderError, "inventory differs"):
                    provider.verify_outputs(root)
                extra.unlink()
                altered = b"synthetic altered output"
                output.write_bytes(altered)
                with self.assertRaisesRegex(provider.ProviderError, "source mismatch"):
                    provider.verify_outputs(root)
                self.assertEqual(altered, output.read_bytes())

    def test_output_junctions_are_refused_before_writing_outside_owned_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linked = root / "build"
            with patch.object(Path, "is_junction", lambda path: path == linked):
                with self.assertRaisesRegex(provider.ProviderError, "no write"):
                    provider.owned_target(root, provider.BUNDLE / "provider.json")


if __name__ == "__main__":
    unittest.main()

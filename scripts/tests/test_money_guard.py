import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("money_guard", Path(__file__).resolve().parents[1] / "check_money.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class MoneySourceGuardTest(unittest.TestCase):
    def rules(self, source, suffix="kt"):
        return {finding.rule for finding in guard.analyze(f"Synthetic.{suffix}", source)}

    def test_kotlin_fields_parameters_nullable_and_containers_reject_every_unsafe_type(self):
        for kind in ("Double", "Float", "BigDecimal", "java.math.BigDecimal"):
            for declaration in (
                f"data class Entry(val amount: {kind})",
                f"var accountBalance:\n    {kind}? = null",
                f"fun post(payment_total: List<{kind}>) {{}}",
                f"val fees: Map<String, List<{kind}?>> = emptyMap()",
                f"val `net_amount`: {kind} = synthetic()",
            ):
                with self.subTest(kind=kind, declaration=declaration):
                    self.assertIn("MG001", self.rules(declaration))

    def test_java_fields_and_method_parameters_reject_unsafe_types(self):
        for kind in ("double", "float", "Double", "Float", "BigDecimal", "java.math.BigDecimal"):
            for declaration in (
                f"class Entry {{ private final {kind} amount; }}",
                f"void post({kind} bankBalance) {{}}",
                f"List<{kind}> fees;",
            ):
                with self.subTest(kind=kind, declaration=declaration):
                    self.assertIn("MG001", self.rules(declaration, "java"))

    def test_aliases_cannot_hide_unsafe_kotlin_types(self):
        for source in (
            "typealias Exact = BigDecimal\nval amount: Exact",
            "typealias First = Second\ntypealias Second = Double\nval balance: First",
            "import java.math.BigDecimal as Exact\nval paidAmount: List<Exact?>",
        ):
            with self.subTest(source=source):
                self.assertIn("MG001", self.rules(source))

    def test_unresolved_explicit_monetary_types_do_not_hide_cross_file_aliases(self):
        for kind in ("ExternalAlias", "Number", "Any"):
            with self.subTest(kind=kind):
                self.assertIn("MG005", self.rules(f"val amount: {kind}"))

    def test_line_breaks_do_not_hide_unsafe_declarations(self):
        for source, suffix in (
            ("val amount\n:\nDouble", "kt"),
            ("val amount: List<\nDouble\n>", "kt"),
            ("private Double\namount;", "java"),
            ("fun balance(): Double = synthetic()", "kt"),
        ):
            with self.subTest(source=source):
                self.assertIn("MG001", self.rules(source, suffix))

    def test_inferred_money_fields_fail_closed(self):
        for expression in (
            "0.1", "1.2f", "1e3", "BigDecimal(\"0.1\")", "input.toDouble()", "unknownFactory()",
            "Money.MAX_MINOR_UNITS", "Money.ofMinorUnits(1L, \"JPY\").currency",
        ):
            with self.subTest(expression=expression):
                self.assertTrue(self.rules(f"val amount = {expression}") & {"MG001", "MG005"})

    def test_money_wrapper_fields_and_operator_dispatch_are_allowed(self):
        source = (
            "import com.pennilogic.contracts.money.Money\n"
            "data class Entry(val amount: Money, val fees: List<Money>)\n"
            "fun add(left: Money, right: Money): Money {\n"
            "    val total = left + right\n"
            "    return total\n"
            "}\n"
            "val balance = Money.ofMinorUnits(1L, \"JPY\")\n"
        )
        self.assertEqual(set(), self.rules(source))

    def test_class_literals_are_not_financial_field_type_annotations(self):
        self.assertEqual(set(), self.rules(
            "assertThrows(MoneyWireException::class.java) { rejected() }\n"
            "val type = Money::class.java\n"
        ))

    def test_nonfinancial_types_metadata_and_coverage_thresholds_are_allowed(self):
        self.assertEqual(set(), self.rules(
            "val temperature: Double = 1.5\n"
            "val taxRate: BigDecimal = \"0.80\".toBigDecimal()\n"
            "val moneyGuard = tasks.register(\"moneyGuard\")\n"
            "val sourceFileCount: Int = 22\n"
            "minimum = \"0.80\".toBigDecimal()\n"
        ))

    def test_comments_and_literal_examples_are_not_executable_declarations(self):
        self.assertEqual(set(), self.rules(
            '// val amount: Double = 0.1\n'
            '/* outer /* val balance: Float */ end */\n'
            'val example = "val amount: BigDecimal"\n'
            'val json = """{"amount":"0.10","currency":"INR"}"""\n'
        ))

    def test_comment_separation_and_string_interpolation_do_not_hide_code(self):
        self.assertIn("MG001", self.rules("val /* synthetic */ amount /* gap */ : /* gap */ Double"))
        self.assertIn("MG002", self.rules('val output = "${left.minorUnits + right.minorUnits}"'))
        self.assertIn("MG003", self.rules('val output = """${amount.toDouble()}"""'))

    def test_raw_minor_unit_arithmetic_and_aliases_are_refused(self):
        for expression in (
            "left.minorUnits + right.minorUnits",
            "left.minor_units - right.minor_units",
            "left.amountMinor * 2",
            "left.amount_minor / 2",
            "left.minorUnits % 2",
            "left.minorUnits++",
            "-left.minorUnits",
            "Math.addExact(left.minorUnits, right.minorUnits)",
            "left.minorUnits.plus(right.minorUnits)",
            "entries.sumOf { it.minorUnits }",
        ):
            with self.subTest(expression=expression):
                self.assertIn("MG002", self.rules(f"val result = {expression}"))
        self.assertIn("MG002", self.rules(
            "val first = entry.minorUnits\nval second = first\nval third = second + 1"
        ))
        self.assertIn("MG002", self.rules("val amount: Long = 1L\nval result = amount / 2"))

    def test_raw_projection_and_lambda_aliases_cannot_bypass_arithmetic_guard(self):
        for source in (
            "val raw = entries.map { it.minorUnits }\nval result = raw.sum()",
            "val result = entries.map { it.minorUnits }.sum()",
            "val raw = source.let { it.minorUnits }\nval result = raw / 2",
        ):
            with self.subTest(source=source):
                self.assertIn("MG002", self.rules(source))

    def test_float_conversions_and_numeric_serializers_are_refused(self):
        for source in (
            "val result = amount.toDouble()",
            "val result = balance.toFloat()",
            "val result = money.toBigDecimal()",
            "val result = Double.parseDouble(amount)",
            'element<Double>("amount")',
            'element<java.math.BigDecimal>("balance")',
            'element<Long>("amount")',
            'writeNumberField("amount", value)',
            '"amount" to JsonPrimitive(0.1)',
            '"amount" to JsonPrimitive(123456L)',
            'val raw: Long = 1L\n"amount" to JsonPrimitive(raw)',
            'JsonPrimitive(value.minorUnits)',
            'object MoneySerializer { fun encode() = encoder.encodeDouble(value) }',
            'object MoneySerializer { fun decode() = decoder.decodeFloat() }',
        ):
            with self.subTest(source=source):
                self.assertTrue(self.rules(source) & {"MG003", "MG004"})

    def test_unterminated_source_fails_instead_of_truncating_scope(self):
        for source in ('val text = "missing', "/* missing", 'val text = "${value', "val `missing"):
            with self.subTest(source=source), self.assertRaises(guard.ScanFailure):
                guard.analyze("Synthetic.kt", source)

    def test_diagnostics_do_not_echo_amounts_accounts_or_source_lines(self):
        source = 'val amount: Double = 987654321.123\nval account = "synthetic-account-marker"\n'
        rendered = "\n".join(finding.diagnostic() for finding in guard.analyze("Synthetic.kt", source))
        self.assertIn("field=amount", rendered)
        self.assertIn("Synthetic.kt:1:5", rendered)
        self.assertNotIn("987654321", rendered)
        self.assertNotIn("synthetic-account-marker", rendered)
        self.assertNotIn(source, rendered)

    def test_diagnostic_identifiers_are_single_line_and_ascii(self):
        rendered = guard.Finding("path\n\u001b.kt", guard.Token("", "", 2, 3), "MG001", "name\n\u001b").diagnostic()
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\u001b", rendered)
        self.assertTrue(rendered.isascii())

    def check(self, root):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            result = guard.check(root)
        return result, json.loads(output.getvalue()), errors.getvalue()

    def test_real_inventory_covers_jvm_sources_outside_main_and_does_not_obey_gitignore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("src/main/Main.kt", "src/test/Test.kt", "transport/Serializer.java", "build.gradle.kts", "src/build/Nested.kt"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("val temperature: Double = 1.5", encoding="utf-8")
            (root / ".gitignore").write_text("transport/", encoding="utf-8")
            cache = root / "build/Cached.kt"
            cache.parent.mkdir()
            cache.write_text("val amount: Double", encoding="utf-8")
            result, metric, _ = self.check(root)
            self.assertEqual(0, result)
            self.assertEqual(5, metric["source_files_scanned"])
            self.assertEqual(0, metric["violations"])
            (root / "transport/Serializer.java").write_text("double amount;", encoding="utf-8")
            result, metric, errors = self.check(root)
            self.assertEqual(1, result)
            self.assertEqual(5, metric["source_files_scanned"])
            self.assertIn("Serializer.java", errors)
            self.assertIn("MG001", errors)

    def test_empty_or_unreadable_encoding_inventory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, metric, _ = self.check(root)
            self.assertEqual(1, result)
            self.assertEqual(0, metric["source_files_scanned"])
            (root / "Invalid.kt").write_bytes(b"\xff")
            result, metric, errors = self.check(root)
            self.assertEqual(1, result)
            self.assertIn("MG000", errors)

    def test_directory_links_and_windows_junctions_refuse_incomplete_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linked = root / "Linked"
            linked.mkdir()
            (linked / "Hidden.kt").write_text("val amount: Double", encoding="utf-8")
            for predicate in ("is_symlink", "is_junction"):
                with self.subTest(predicate=predicate), patch.object(Path, predicate, lambda path: path == linked):
                    result, metric, errors = self.check(root)
                    self.assertEqual(1, result)
                    self.assertEqual(0, metric["source_files_scanned"])
                    self.assertIn("MG000", errors)

    def test_warn_only_mode_is_not_an_available_cli_option(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "check_money.py"), "--warn-only"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("unrecognized arguments: --warn-only", result.stderr)

    def test_planted_field_is_rejected_and_removal_restores_normal_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            normal = root / "Normal.kt"
            normal.write_text("class Normal", encoding="utf-8")
            self.assertEqual(0, self.check(root)[0])
            planted = root / "Planted.kt"
            planted.write_text("data class Planted(val amount: Double)", encoding="utf-8")
            result, metric, errors = self.check(root)
            self.assertEqual(1, result)
            self.assertEqual(2, metric["source_files_scanned"])
            self.assertIn("MG001", errors)
            planted.unlink()
            self.assertEqual(0, self.check(root)[0])


if __name__ == "__main__":
    unittest.main()

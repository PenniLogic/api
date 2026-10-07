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

    def test_java_later_declarators_reject_every_unsafe_type(self):
        for kind in (
            "double", "float", "Double", "Float", "java.lang.Double", "java.lang.Float",
            "BigDecimal", "java.math.BigDecimal", "double[]", "float[][]",
            "List<Double>", "Map<String, List<java.math.BigDecimal>>",
        ):
            for declaration, fields in (
                (f"private {kind} temperature, amount;", ["amount"]),
                (f"private static {kind} temperature, distance, amount, balance, fee;", ["amount", "balance", "fee"]),
                (
                    f"private final {kind} temperature = sample(), amount = sample(), balance = sample();",
                    ["amount", "balance"],
                ),
                (f"private /* type */ {kind}\n temperature /* gap */,\n /* later */ amount,\n balance;", ["amount", "balance"]),
            ):
                with self.subTest(kind=kind, declaration=declaration):
                    findings = guard.analyze("Synthetic.java", f"class Entry {{ {declaration} }}")
                    self.assertEqual([("MG001", field) for field in fields], [
                        (finding.rule, finding.field) for finding in findings
                    ])

    def test_java_contextual_identifiers_remain_legal_variable_names(self):
        for name_index, name in enumerate((
            "exports", "module", "open", "opens", "permits", "provides", "record", "requires",
            "sealed", "to", "transitive", "uses", "var", "when", "with", "yield",
        )):
            for kind in ("double", "long"):
                for initialized in (False, True):
                    class_name = f"ContextualName{name_index}_{kind}_{int(initialized)}"
                    initializer = " = 0" if initialized else ""
                    source = (
                        f"final class {class_name} {{ "
                        f"private {kind} {name}{initializer}, amount{initializer}; }}"
                    )
                    with self.subTest(name=name, kind=kind, initialized=initialized):
                        self.assertEqual([("MG001", "amount")] if kind == "double" else [], [
                            (finding.rule, finding.field) for finding in guard.analyze(f"{class_name}.java", source)
                        ])
                        tokens = guard.tokenize(source)
                        self.assertEqual([(name, {kind}), ("amount", {kind})], [
                            (tokens[position].text, types)
                            for position, types in guard.java_declaration_types(tokens).items()
                        ])

    def test_java_contextual_declarators_keep_qualified_container_and_array_types(self):
        for name_index, name in enumerate(("record", "sealed", "permits", "yield")):
            for form_index, (declaration, fields) in enumerate((
                ("private double {name}, amount, balance;", ["amount", "balance"]),
                ("private float temperature, {name}, amount, balance;", ["amount", "balance"]),
                ("private Double {name} = null, amount = null, balance = null;", ["amount", "balance"]),
                ("private java.lang.Float {name}, amount;", ["amount"]),
                ("private java.lang.Double temperature = null, {name} = null, amount = null;", ["amount"]),
                ("private java.math.BigDecimal {name} = null, amount = null;", ["amount"]),
                ("private java.util.List<Double> {name} = null, amount = null;", ["amount"]),
                ("private java.util.Map<String, java.util.List<java.math.BigDecimal>> {name}, amount;", ["amount"]),
                ("private double[] {name} = {{}}, amount = {{}};", ["amount"]),
                ("private java.util.List<Long> {name} = null, amount = null;", []),
            )):
                class_name = f"ContextualForm{name_index}_{form_index}"
                source = f"final class {class_name} {{ {declaration.format(name=name)} }}"
                with self.subTest(name=name, declaration=declaration):
                    self.assertEqual([("MG001", field) for field in fields], [
                        (finding.rule, finding.field) for finding in guard.analyze(f"{class_name}.java", source)
                    ])

    def test_java_context_keywords_still_bound_types_and_statements(self):
        for source in (
            "record ContextualRecord(long size) { private static double record, amount; }",
            "sealed class ContextualParent permits ContextualChild { private double sealed, amount; } "
            "final class ContextualChild extends ContextualParent {}",
            "final class ContextualLocal { void post(double record) { double yield = record, amount; } }",
        ):
            with self.subTest(source=source):
                self.assertEqual([("MG001", "amount")], [
                    (finding.rule, finding.field) for finding in guard.analyze("ContextualSyntax.java", source)
                ])
        source = (
            "final class ContextualYield { int read(int mode) { "
            "return switch (mode) { default -> { int result = 0; yield result; } }; } }"
        )
        tokens = guard.tokenize(source)
        self.assertEqual([("result", {"int"})], [
            (tokens[position].text, types)
            for position, types in guard.java_declaration_types(tokens).items()
        ])
        self.assertEqual([], guard.analyze("ContextualYield.java", source))

    def test_java_contextual_package_qualifiers_preserve_shared_types(self):
        for name_index, name in enumerate(("ordinary", "record", "sealed", "permits", "yield")):
            for position, prefix in enumerate((name, f"fixture.{name}")):
                for form, (type_name, declarators, names, unsafe) in enumerate((
                    ("{package}.Holder<Double>", "temperature, amount", ["temperature", "amount"], ["amount"]),
                    (
                        "{package}.Holder<java.lang.Float>", "record = null, amount = null, balance = null",
                        ["record", "amount", "balance"], ["amount", "balance"],
                    ),
                    (
                        "{package}.Holder<java.util.Map<String, java.math.BigDecimal>>",
                        "temperature = null, distance = null, amount = null, balance = null",
                        ["temperature", "distance", "amount", "balance"], ["amount", "balance"],
                    ),
                    (
                        "{package}.Holder<Double>[]", "temperature = null, amount = null",
                        ["temperature", "amount"], ["amount"],
                    ),
                    (
                        "{package}.Holder<Double>", "temperature[] = null, amount[] = null",
                        ["temperature", "amount"], ["amount"],
                    ),
                    ("{package}.Holder<Long>", "temperature, amount, balance", ["temperature", "amount", "balance"], []),
                    ("{package}.Money", "temperature = null, amount = null", ["temperature", "amount"], []),
                    (
                        "{package}.Holder<{package}.Money>", "temperature = null, amount = null",
                        ["temperature", "amount"], [],
                    ),
                    ("{package}.Holder<Double>", "temperature, distance", ["temperature", "distance"], []),
                )):
                    package = f"{prefix}.case{form}"
                    type_name = type_name.format(package=package)
                    class_name = f"QualifiedContext{name_index}_{position}_{form}"
                    source = (
                        f"package {package}; final class Holder<T> {{}} final class Money {{}} "
                        f"final class {class_name} {{ private {type_name} {declarators}; }}"
                    )
                    with self.subTest(package=package, type_name=type_name, declarators=declarators):
                        self.assertEqual([("MG001", field) for field in unsafe], [
                            (finding.rule, finding.field) for finding in guard.analyze(f"{class_name}.java", source)
                        ])
                        tokens = guard.tokenize(source)
                        types = {token.text for token in guard.tokenize(type_name)}
                        self.assertEqual([(field, types) for field in names], [
                            (tokens[index].text, value)
                            for index, value in guard.java_declaration_types(tokens).items()
                        ])

    def test_java_qualified_contextual_types_keep_nested_boundaries(self):
        for name_index, name in enumerate(("record", "sealed", "permits", "yield")):
            for form, body in enumerate((
                "private {type} temperature = sample(1, 2), amount; "
                "private static {type} sample(int left, int right) {{ return null; }}",
                "private {type} @Marker({{1, 2}}) [] temperature = null, amount = null;",
                "private {type} temperature @Marker(1) [] = null, amount @Marker({{1, 2}}) [] = null;",
                "private {type} temperature = null, amount = null; "
                "private {package}.Holder<Long> distance = null, balance = null;",
                "private {package}.Holder<Long>.Nested<Double> temperature, amount;",
                "int read(int mode) {{ return switch (mode) {{ default -> {{ "
                "{type} temperature = null, amount = null; yield mode; }} }}; }}",
            )):
                package = f"{name}.edge{form}"
                class_name = f"QualifiedBoundary{name_index}_{form}"
                body = body.format(type=f"{package}.Holder<Double>", package=package)
                source = (
                    f"package {package}; final class Holder<T> {{ final class Nested<U> {{}} }} "
                    f"final class {class_name} {{ "
                    "@java.lang.annotation.Target(java.lang.annotation.ElementType.TYPE_USE) "
                    "@interface Marker { int[] value(); } "
                    f"{body} }}"
                )
                with self.subTest(package=package, body=body):
                    self.assertEqual([("MG001", "amount")], [
                        (finding.rule, finding.field) for finding in guard.analyze(f"{class_name}.java", source)
                    ])
                    tokens = guard.tokenize(source)
                    declared = {
                        tokens[index].text: types
                        for index, types in guard.java_declaration_types(tokens).items()
                    }
                    self.assertIn("Double", declared["amount"])
                    if form == 3:
                        self.assertIn("Long", declared["balance"])
                        self.assertNotIn("Double", declared["balance"])

    def test_java_local_var_preserves_inferred_money(self):
        for modifier_index, modifier in enumerate(("", "final ")):
            for form, declaration in enumerate((
                "{modifier}var amount = incoming;",
                "{modifier}var balance = incoming;",
                "{modifier}var value = incoming; {modifier}var amount = value;",
                "{modifier}var var = incoming; {modifier}var amount = var;",
                "{modifier}var first = incoming; {modifier}var amount = first;",
            )):
                name = f"InferredMoney{modifier_index}_{form}"
                declaration = declaration.format(modifier=modifier)
                source = (
                    "import com.pennilogic.contracts.money.Money; "
                    f"final class {name} {{ void copy(Money incoming) {{ {declaration} }} }}"
                )
                with self.subTest(modifier=modifier, declaration=declaration):
                    self.assertEqual([], guard.analyze(f"{name}.java", source))
                    self.assertEqual({}, guard.java_declaration_types(guard.tokenize(source)))

    def test_java_local_var_keeps_numeric_and_unknown_money_refusals(self):
        for modifier_index, modifier in enumerate(("", "final ")):
            for form, (expression, rule) in enumerate((
                ("0.5", "MG001"),
                ("1.5f", "MG001"),
                ('new java.math.BigDecimal("1.0")', "MG001"),
                ("1L", "MG005"),
                ("unknown()", "MG005"),
                ("incoming.getMinorUnits()", "MG005"),
                ('Double.valueOf("0.5")', "MG001"),
            )):
                name = f"InferredRefusal{modifier_index}_{form}"
                source = (
                    "import com.pennilogic.contracts.money.Money; "
                    f"final class {name} {{ void copy(Money incoming) {{ "
                    f"{modifier}var amount = {expression}; }} "
                    "private static Object unknown() { return null; } }"
                )
                with self.subTest(modifier=modifier, expression=expression):
                    self.assertEqual([(rule, "amount")], [
                        (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                    ])
                    self.assertEqual({}, guard.java_declaration_types(guard.tokenize(source)))
        source = "final class InferredOrdinary { void read() { var temperature = 0.5; final var count = 1L; } }"
        self.assertEqual([], guard.analyze("InferredOrdinary.java", source))

    def test_java_local_var_preserves_raw_alias_refusal_rules(self):
        for modifier_index, modifier in enumerate(("", "final ")):
            for chained in (False, True):
                for form, (method, operation, extra, rule) in enumerate((
                    ("long increment", "return {alias} + 1L;", "", "MG002"),
                    ("double temperature", "return ((Long) {alias}).doubleValue();", "", "MG003"),
                    ("void emit", "writeNumber({alias});", "void writeNumber(long value) {}", "MG004"),
                )):
                    for parameter in ("minorUnits", "size"):
                        name = f"VarFlow{modifier_index}_{int(chained)}_{form}_{parameter}"
                        alias = "value" if chained else "raw"
                        declarations = f"{modifier}var raw = {parameter}; "
                        if chained:
                            declarations += f"{modifier}var value = raw; "
                        source = (
                            f"final class {name} {{ {method}(long {parameter}) {{ "
                            f"{declarations}{operation.format(alias=alias)} }} {extra} }}"
                        )
                        with self.subTest(modifier=modifier, chained=chained, rule=rule, parameter=parameter):
                            # Name-based tracking also reaches the serializer's value parameter.
                            count = 2 if chained and rule == "MG004" else 1
                            self.assertEqual([(rule, "")] * count if parameter == "minorUnits" else [], [
                                (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                            ])

    def test_java_var_variable_and_package_names_keep_explicit_shared_types(self):
        for index, (kind, initializer, unsafe) in enumerate((
            ("double", "0.0", True), ("long", "0L", False),
            ("com.pennilogic.contracts.money.Money", "null", False),
            ("java.util.List<Double>", "null", True),
        )):
            name = f"ExplicitVar{index}"
            source = (
                f"final class {name} {{ private {kind} var = {initializer}, "
                f"amount = {initializer}, balance = {initializer}; }}"
            )
            with self.subTest(kind=kind):
                self.assertEqual([("MG001", "amount"), ("MG001", "balance")] if unsafe else [], [
                    (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                ])
                tokens = guard.tokenize(source)
                types = {token.text for token in guard.tokenize(kind)}
                self.assertEqual([(field, types) for field in ("var", "amount", "balance")], [
                    (tokens[position].text, value)
                    for position, value in guard.java_declaration_types(tokens).items()
                ])
        for position, prefix in enumerate(("var", "fixture.var")):
            for form, kind in enumerate(("Double", "Long", "Money")):
                package = f"{prefix}.inference{form}"
                name = f"QualifiedVar{position}_{form}"
                kind = f"{package}.Holder<{kind}>"
                source = (
                    f"package {package}; import com.pennilogic.contracts.money.Money; "
                    f"final class Holder<T> {{}} final class {name} {{ "
                    f"private {kind} temperature = null, amount = null; }}"
                )
                with self.subTest(package=package, kind=kind):
                    self.assertEqual([("MG001", "amount")] if form == 0 else [], [
                        (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                    ])
                    tokens = guard.tokenize(source)
                    types = {token.text for token in guard.tokenize(kind)}
                    self.assertEqual([(field, types) for field in ("temperature", "amount")], [
                        (tokens[index].text, value)
                        for index, value in guard.java_declaration_types(tokens).items()
                    ])

    def test_java_later_declarators_survive_nested_initializers_and_array_dimensions(self):
        for declaration in (
            "double temperature = choose(1.0, choose(2.0, 3.0)), amount;",
            "double temperature = new double[]{1.0, 2.0}[0], amount;",
            "double temperature[] = {1.0, 2.0}, amount[];",
            "double temperature[][] = {{1.0, 2.0}, {3.0, 4.0}}, amount[][];",
            "double temperature @Marker [] = {}, amount @Marker [] = {};",
            "double temperature @Marker(values = {1, 2}) [] = {}, amount @Marker [] = {};",
            "Double temperature = Factory.<String, Double>read(), amount;",
            "Double temperature = new Box<String, Map<Integer, Double>>().read(), amount;",
            "Double temperature = new Box<@Marker(values = {1, 2}) String, Double>().read(), amount;",
            "Double temperature = new Supplier<Double>() { public Double get() { return null; } }.get(), amount;",
            "Double temperature = ((Supplier<Double>) () -> { double x = 1.0, y = 2.0; return x; }).get(), amount;",
        ):
            with self.subTest(declaration=declaration):
                findings = guard.analyze("Synthetic.java", f"class Entry {{ {declaration} }}")
                self.assertEqual([("MG001", "amount")], [
                    (finding.rule, finding.field) for finding in findings
                ])

    def test_java_comparisons_and_shifts_do_not_hide_later_declarators(self):
        for expression in (
            "left < right ? 1.0 : 2.0", "left > right ? 1.0 : 2.0",
            "left <= right ? 1.0 : 2.0", "left >= right ? 1.0 : 2.0",
            "1 << 2", "8 >> 2", "8 >>> 2",
            "value instanceof Map<?, ?> ? 1.0 : 2.0",
        ):
            with self.subTest(expression=expression):
                findings = guard.analyze(
                    "Synthetic.java", f"class Entry {{ double temperature = {expression}, amount; }}",
                )
                self.assertEqual([("MG001", "amount")], [
                    (finding.rule, finding.field) for finding in findings
                ])

    def test_java_generic_shift_operands_cannot_replace_the_declaration_type(self):
        for index, kind in enumerate(("Long", "Double", "java.util.List<Long>")):
            name = f"ShiftGeneric{index}"
            source = (
                f"final class {name} {{ private int count = 1; "
                "private static <T> int read() { return 1; } "
                f"private double temperature = 8 << {name}.<{kind}>read() >> count, amount, balance; }}"
            )
            with self.subTest(kind=kind):
                self.assertEqual([("MG001", "amount"), ("MG001", "balance")], [
                    (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                ])
                tokens = guard.tokenize(source)
                self.assertEqual([
                    ("count", {"int"}), ("temperature", {"double"}),
                    ("amount", {"double"}), ("balance", {"double"}),
                ], [
                    (tokens[position].text, types)
                    for position, types in guard.java_declaration_types(tokens).items()
                ])

    def test_java_long_shift_operands_do_not_inherit_an_earlier_floating_type(self):
        for index, expression in enumerate((
            "bits >> count", "bits >>> count", "bits << count",
            "8 << {name}.<Double>read() >> count",
        )):
            name = f"ShiftSafe{index}"
            expression = expression.format(name=name)
            source = (
                f"final class {name} {{ private double temperature; private int count = 1; "
                "private long bits = 8; private static <T> int read() { return 1; } "
                f"private long size = {expression}, amount, balance; }}"
            )
            with self.subTest(expression=expression):
                self.assertEqual([], guard.analyze(f"{name}.java", source))

    def test_java_type_use_annotation_arguments_preserve_shared_array_types(self):
        for type_index, kind in enumerate((
            "long", "double", "float", "java.lang.Long", "java.lang.Double",
            "java.lang.Float", "java.math.BigDecimal",
        )):
            for annotation_index, dimensions in enumerate((
                "[]", "@Marker []", "@Marker(1) []", "@Marker({1, 2}) []",
                "@Marker(value = {1 << 2, 3}) []", "@Marker(1) [] @Marker({2, 3}) []",
            )):
                name = f"AnnotatedArray{type_index}_{annotation_index}"
                source = (
                    f"final class {name} {{ "
                    "@java.lang.annotation.Target(java.lang.annotation.ElementType.TYPE_USE) "
                    "@interface Marker { int[] value() default {}; } "
                    f"private {kind} {dimensions} temperature = {{}}, amount = {{}}; }}"
                )
                expected = [] if kind in {"long", "java.lang.Long"} else [("MG001", "amount")]
                with self.subTest(kind=kind, dimensions=dimensions):
                    self.assertEqual(expected, [
                        (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                    ])

    def test_java_annotation_class_arguments_are_not_part_of_the_declared_type(self):
        for index, (kind, argument, expected) in enumerate((
            ("long", "Double", []),
            ("double", "Long", [("MG001", "amount")]),
            ("java.util.List<Long>", "Double", []),
            ("java.util.List<Double>", "Long", [("MG001", "amount")]),
        )):
            name = f"AnnotationType{index}"
            source = (
                f"final class {name} {{ "
                "@java.lang.annotation.Target(java.lang.annotation.ElementType.TYPE_USE) "
                "@interface Marker { Class<?> value(); } "
                f"private {kind} @Marker({argument}.class) [] temperature = null, amount = null; }}"
            )
            with self.subTest(kind=kind, argument=argument):
                self.assertEqual(expected, [
                    (finding.rule, finding.field) for finding in guard.analyze(f"{name}.java", source)
                ])

    def test_java_initializer_commas_are_not_declarations(self):
        for declaration in (
            "double temperature = sample(null, amount), distance;",
            "double temperature = sample(new Money[]{amount, amount}), distance;",
            "double temperature = values[sample(0, amount)], distance;",
            "Double temperature = Factory.<String, amount, Double>read(), distance;",
            "Double temperature = new Box<String, amount, Double>().read(), distance;",
            "Map<String, Double> temperature = new HashMap<String, Double>(), distance;",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual([], guard.analyze("Synthetic.java", f"class Entry {{ {declaration} }}"))

    def test_java_declarator_types_do_not_leak_between_declarations_or_parameters(self):
        for source in (
            "class Entry { double temperature, distance; Money amount; }",
            "class Entry { double temperature, distance; Money price, amount; }",
            "class Entry { double temperature, distance; long amount; }",
            "class Entry { double temperature, distance; void post(Money amount) {} }",
            "class Entry { Double temperature() { return null; } Money amount; }",
            "void post(double temperature, Money amount) {}",
            "void post(double temperature, Money[] amount) {}",
            "void post(double temperature, List<Money> amount) {}",
            "void post(double temperature, @Marker com.pennilogic.contracts.money.Money amount) {}",
        ):
            with self.subTest(source=source):
                self.assertEqual([], guard.analyze("Synthetic.java", source))
        for source, fields in (
            ("class Entry { Money value, amount; double temperature, balance; }", ["balance"]),
            ("class Entry { double temperature, amount; Money value, balance; }", ["amount"]),
            ("void post(double temperature, java.lang.Float amount) {}", ["amount"]),
            ("class Entry { void post() { double temperature, amount; } float distance, fee; }", ["amount", "fee"]),
        ):
            with self.subTest(source=source):
                self.assertEqual([("MG001", field) for field in fields], [
                    (finding.rule, finding.field) for finding in guard.analyze("Synthetic.java", source)
                ])

    def test_java_nonfinancial_declarators_and_money_wrappers_are_allowed(self):
        for source in (
            "class Entry { private double temperature, distance; }",
            "class Entry { private float temperature, taxRate; }",
            "class Entry { private java.math.BigDecimal temperature, interestRate; }",
            "class Entry { Money value, amount, balance; Money total = amount + balance; }",
        ):
            with self.subTest(source=source):
                self.assertEqual([], guard.analyze("Synthetic.java", source))

    def test_java_later_integer_declarators_keep_raw_arithmetic_tracking(self):
        self.assertEqual({"MG002"}, self.rules(
            "class Entry { long size = 1, amount = 2; long result = amount + 1; }", "java",
        ))

    def test_java_later_declarator_diagnostics_locate_names_without_disclosing_values(self):
        source = (
            "class Entry {\n"
            "    private double temperature = sample(\"synthetic-account-marker\", 1.0),\n"
            "        amount = 987654321.123;\n"
            "}\n"
        )
        findings = guard.analyze("Synthetic.java", source)
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual(("MG001", "amount", 3, 9), (
            finding.rule, finding.field, finding.token.line, finding.token.column,
        ))
        self.assertNotIn("987654321", finding.diagnostic())
        self.assertNotIn("synthetic-account-marker", finding.diagnostic())
        self.assertNotIn(source, finding.diagnostic())

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

    def test_unterminated_java_annotation_fails_instead_of_discarding_declarations(self):
        for source in (
            "class Entry { long @Marker({1, 2} [] temperature, amount; }",
            "class Entry { double temperature, amount; @Marker(",
        ):
            with self.subTest(source=source), self.assertRaises(guard.ScanFailure):
                guard.analyze("Synthetic.java", source)

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

    def test_planted_java_later_field_is_rejected_and_removal_restores_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Normal.java").write_text("class Normal {}", encoding="utf-8")
            self.assertEqual(0, self.check(root)[0])
            planted = root / "Planted.java"
            planted.write_text("class Planted { private double temperature, amount; }", encoding="utf-8")
            result, metric, errors = self.check(root)
            self.assertEqual(1, result)
            self.assertEqual(2, metric["source_files_scanned"])
            self.assertEqual(1, metric["violations"])
            self.assertIn("Planted.java:1:45 MG001 field=amount", errors)
            planted.unlink()
            result, metric, errors = self.check(root)
            self.assertEqual((0, 1, 0), (
                result, metric["source_files_scanned"], metric["violations"],
            ))
            self.assertEqual("", errors)


if __name__ == "__main__":
    unittest.main()

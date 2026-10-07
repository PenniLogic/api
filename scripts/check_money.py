"""Fail closed on unsafe JVM money declarations, conversions and raw arithmetic."""

from bisect import bisect_right
from dataclasses import dataclass
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".kt", ".kts", ".java"}
BUILD_DIRECTORIES = {".git", ".gradle", ".kotlin", ".idea", ".venv", "node_modules", "build"}
UNSAFE_TYPES = {"Double", "Float", "BigDecimal", "double", "float"}
INTEGER_TYPES = {"Long", "Int", "Short", "Byte", "BigInteger", "long", "int", "short", "byte"}
JAVA_MODIFIERS = {"public", "protected", "private", "static", "final", "transient", "volatile"}
JAVA_RESERVED_WORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while", "_", "true", "false", "null",
}
JAVA_NON_TYPES = (JAVA_RESERVED_WORDS - {
    "boolean", "byte", "char", "double", "float", "int", "long", "short",
}) | {"record", "sealed", "permits", "yield"}
MONEY_WORDS = {
    "money", "amount", "balance", "price", "cost", "fee", "salary", "income", "expense",
    "payment", "refund", "budget", "tax", "principal", "debit", "credit", "total", "cash",
}
METADATA_SUFFIXES = {
    "guard", "test", "fixture", "serializer", "codec", "registry", "exponent", "currency",
    "name", "description", "reason", "code", "count", "rate", "ratio", "percent", "percentage",
}
RAW_MEMBERS = {"minorUnits", "minor_units", "amountMinor", "amount_minor"}
ARITHMETIC = {"+", "-", "*", "/", "%", "+=", "-=", "*=", "/=", "%=", "++", "--"}
ARITHMETIC_CALLS = {
    "addExact", "subtractExact", "multiplyExact", "negateExact", "floorDiv", "floorMod",
    "plus", "minus", "times", "div", "rem", "inc", "dec", "unaryMinus", "sum", "sumOf", "average",
}
CONVERSIONS = {
    "toDouble", "toDoubleOrNull", "toFloat", "toFloatOrNull", "toBigDecimal",
    "toBigDecimalOrNull", "doubleValue", "floatValue", "parseDouble", "parseFloat",
}
NUMBER_SERIALIZERS = {
    f"{operation}{kind}{suffix}"
    for operation in ("encode", "decode")
    for kind in ("Double", "Float", "Long", "Int", "Short", "Byte")
    for suffix in ("", "Element")
} | {"writeNumber", "writeNumberField", "numberValue"}
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUMBER = re.compile(r"(?:[0-9][A-Za-z0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?|\.[0-9]+[fFdD]?)")
OPERATORS = re.compile(r"::|\?\.|->|\+=|-=|\*=|/=|%=|\+\+|--|==|!=|<=|>=|&&|\|\|")
MESSAGES = {
    "MG001": "floating/decimal money type; use the Contracts Money wrapper",
    "MG002": "raw money arithmetic; use the Money value type's checked operators",
    "MG003": "floating/decimal money conversion is forbidden",
    "MG004": "numeric money serialization is forbidden; use the accepted Contracts seam",
    "MG005": "unresolved money type; declare the Contracts Money type explicitly",
    "MG000": "source inventory or tokenization failed; the guard cannot certify this source",
}


@dataclass(frozen=True)
class Token:
    text: str
    kind: str
    line: int
    column: int


@dataclass(frozen=True)
class Finding:
    path: str
    token: Token
    rule: str
    field: str = ""

    def diagnostic(self):
        path = json.dumps(self.path, ensure_ascii=True)[1:-1]
        field = json.dumps(self.field, ensure_ascii=True)[1:-1][:80]
        suffix = f" field={field}" if field else ""
        return f"{path}:{self.token.line}:{self.token.column} {self.rule}{suffix}: {MESSAGES[self.rule]}"


class ScanFailure(ValueError):
    pass


def money_name(name):
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().split("_")
    words = [word[:-1] if word.endswith("s") else word for word in words if word]
    if not words or words[-1] in METADATA_SUFFIXES:
        return False
    return name in RAW_MEMBERS or bool(set(words) & MONEY_WORDS)


def tokenize(source):
    newlines = [-1] + [index for index, character in enumerate(source) if character == "\n"]
    tokens = []

    def emit(text, kind, offset):
        line = bisect_right(newlines, offset)
        tokens.append(Token(text, kind, line, offset - newlines[line - 1]))

    def comment(index):
        if source.startswith("//", index):
            end = source.find("\n", index)
            return len(source) if end == -1 else end
        depth, index = 1, index + 2
        while index < len(source) and depth:
            if source.startswith("/*", index):
                depth, index = depth + 1, index + 2
            elif source.startswith("*/", index):
                depth, index = depth - 1, index + 2
            else:
                index += 1
        if depth:
            raise ScanFailure("unterminated comment")
        return index

    def string(index):
        start = index
        quote = '"""' if source.startswith('"""', index) else source[index]
        index += len(quote)
        body_start = index
        interpolated = False
        while index < len(source):
            if source.startswith(quote, index):
                label = source[body_start:index]
                label = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), label)
                if not interpolated:
                    kind = "money_key" if IDENTIFIER.fullmatch(label) and money_name(label) else "literal"
                    emit(label if kind == "money_key" else "", kind, start)
                return index + len(quote)
            if quote != '"""' and source[index] == "\\":
                index += 2
            elif quote != "'" and source.startswith("${", index):
                interpolated = True
                index = code(index + 2, template=True)
            elif quote != "'" and source[index] == "$":
                match = IDENTIFIER.match(source, index + 1)
                if match:
                    interpolated = True
                    emit(match[0], "identifier", index + 1)
                    index = match.end()
                else:
                    index += 1
            else:
                index += 1
        raise ScanFailure("unterminated string")

    def code(index, template=False):
        depth = 0
        while index < len(source):
            if source[index].isspace():
                index += 1
            elif source.startswith(("//", "/*"), index):
                index = comment(index)
            elif source[index] in "\"'":
                index = string(index)
            elif source[index] == "`":
                end = source.find("`", index + 1)
                if end == -1 or "\n" in source[index + 1:end]:
                    raise ScanFailure("unterminated identifier")
                emit(source[index + 1:end], "identifier", index)
                index = end + 1
            elif template and source[index] == "}" and depth == 0:
                return index + 1
            else:
                match = IDENTIFIER.match(source, index)
                number = NUMBER.match(source, index) if not match else None
                operator = OPERATORS.match(source, index) if not match and not number else None
                value = match[0] if match else number[0] if number else operator[0] if operator else source[index]
                kind = "identifier" if match else "number" if number else "symbol"
                emit(value, kind, index)
                if template and value in {"{", "}"}:
                    depth += 1 if value == "{" else -1
                index += len(value)
        if template:
            raise ScanFailure("unterminated interpolation")
        return index

    code(0)
    return sorted(tokens, key=lambda token: (token.line, token.column))


def statements(tokens):
    start = 0
    depth = 0
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else None
        continuation = previous and (
            previous.text in ARITHMETIC | {"=", ":", ".", "?.", ",", "->", "<"}
            or token.text in ARITHMETIC | {".", "?.", ":", "=", ",", ">"}
        )
        new_line = previous and token.line > previous.line and depth == 0 and not continuation
        if token.text in {";", "{", "}"} or new_line:
            if start < index:
                yield tokens[start:index]
            start = index + 1 if token.text in {";", "{", "}"} else index
        if token.text in {"(", "["}:
            depth += 1
        elif token.text in {")", "]"}:
            depth = max(0, depth - 1)
    if start < len(tokens):
        yield tokens[start:]


def declared_type(tokens, index):
    result, depth = [], 0
    previous = None
    for token in tokens[index:]:
        if depth == 0 and token.text in {"=", ",", ")", ";", "{", "}"}:
            break
        if previous and depth == 0 and token.line > previous.line and (
            previous.text not in {".", "->", "@"} and token.text not in {".", "?", "[", "<", "->"}
        ):
            break
        if depth == 0 and token.text in {"get", "set", "by"}:
            break
        if token.text in {"<", "(", "["}:
            depth += 1
        elif token.text in {">", ")", "]"}:
            depth -= 1
        result.append(token.text)
        previous = token
    return set(result)


def expression_after(tokens, index):
    result, depth = [], 0
    for token in tokens[index:]:
        if depth == 0 and token.text in {";", "}", ")"}:
            break
        if result and depth == 0 and token.line > result[-1].line and (
            result[-1].text not in ARITHMETIC | {"=", ".", "?.", ",", "->"}
            and token.text not in ARITHMETIC | {".", "?.", "else"}
        ):
            break
        if token.text in {"(", "[", "{"}:
            depth += 1
        elif token.text in {")", "]", "}"}:
            depth -= 1
        result.append(token)
    return result


def java_type_prefix(tokens, index):
    result, depth = [], 0
    for token in reversed(tokens[:index]):
        if depth == 0 and token.text in {";", "{", "}", "(", ",", "="}:
            break
        if token.text in {">", "]"}:
            depth += 1
        elif token.text in {"<", "["}:
            depth -= 1
        result.append(token.text)
    return set(result)


def java_group_end(tokens, index):
    closing = {"(": ")", "[": "]", "{": "}", "<": ">"}
    stack = [closing[tokens[index].text]]
    for end in range(index + 1, len(tokens)):
        token = tokens[end]
        if token.text == stack[-1]:
            stack.pop()
            if not stack:
                return end + 1
        elif token.text in {"(", "[", "{"} or token.text == "<" and stack[-1] == ">":
            stack.append(closing[token.text])
        elif stack[-1] == ">" and token.kind != "identifier" and token.text not in {".", ",", "?", "@", "&"}:
            # Outside annotation arguments, a generic group contains types, not comparisons.
            return None
    return None


def java_declarators(tokens, index):
    while index < len(tokens) and (
        tokens[index].kind == "identifier" and tokens[index].text not in JAVA_RESERVED_WORDS
    ):
        end = index + 1
        while end + 1 < len(tokens) and tokens[end].text == "[" and tokens[end + 1].text == "]":
            end += 2
        if end >= len(tokens) or tokens[end].text not in {"=", ",", ";"}:
            return
        yield index
        if tokens[end].text == "=":
            end += 1
            while end < len(tokens) and tokens[end].text not in {",", ";", ")", "}"}:
                text = tokens[end].text
                if text in {"(", "[", "{", "<"}:
                    following = java_group_end(tokens, end)
                    if following is not None:
                        end = following
                        continue
                    if text != "<":
                        return
                end += 1
        if end >= len(tokens) or tokens[end].text != ",":
            return
        index = end + 1


def java_declaration_types(tokens):
    code, positions = [], []
    index = 0
    while index < len(tokens):
        if (
            tokens[index].text == "@" and index + 1 < len(tokens)
            and tokens[index + 1].kind == "identifier" and tokens[index + 1].text != "interface"
        ):
            end = index + 2
            while end + 1 < len(tokens) and tokens[end].text == "." and tokens[end + 1].kind == "identifier":
                end += 2
            if end < len(tokens) and tokens[end].text == "(":
                end = java_group_end(tokens, end)
                if end is None:
                    raise ScanFailure("unterminated Java annotation")
            index = end
        else:
            code.append(tokens[index])
            positions.append(index)
            index += 1

    declarations = {}
    for start in range(len(code)):
        if start and code[start - 1].text not in {";", "{", "}", "(", ",", ":"}:
            continue
        index = start
        while index < len(code) and code[index].text in JAVA_MODIFIERS:
            index += 1
        if index >= len(code) or code[index].kind != "identifier" or code[index].text in JAVA_NON_TYPES:
            continue
        end = index + 1
        while end < len(code):
            if code[end].text == "<":
                following = java_group_end(code, end)
                if following is None:
                    break
                end = following
            if end + 1 < len(code) and code[end].text == "." and code[end + 1].kind == "identifier":
                end += 2
            else:
                break
        while end + 1 < len(code) and code[end].text == "[" and code[end + 1].text == "]":
            end += 2
        if end >= len(code) or code[end].kind != "identifier" or code[end].text in JAVA_RESERVED_WORDS:
            continue
        # Only a complete type at a declaration boundary may supply shared declarator types.
        types = java_type_prefix(code[index:end], end - index)
        declarations.update((positions[declarator], types) for declarator in java_declarators(code, end))
    return declarations


def floating_literal(token):
    text = token.text.replace("_", "")
    return token.kind == "number" and (
        "." in text or bool(re.search(r"[eE]", text)) or text.endswith(("f", "F", "d", "D"))
    ) and not text.lower().startswith(("0x", "0b"))


def analyze(path, source):
    tokens = tokenize(source)
    chunks = list(statements(tokens))
    aliases = {name: {name} for name in UNSAFE_TYPES | INTEGER_TYPES | {"Money"}}
    alias_declarations = []
    for chunk in chunks:
        for index, token in enumerate(chunk):
            if token.text == "typealias" and index + 2 < len(chunk) and chunk[index + 2].text == "=":
                alias_declarations.append((chunk[index + 1].text, {item.text for item in chunk[index + 3:]}))
            elif token.text == "import" and any(item.text == "as" for item in chunk):
                position = next(i for i, item in enumerate(chunk) if item.text == "as")
                if position and position + 1 < len(chunk):
                    alias_declarations.append((chunk[position + 1].text, {chunk[position - 1].text}))
    for _ in range(len(alias_declarations) + 1):
        for name, targets in alias_declarations:
            aliases[name] = set().union(*(aliases.get(target, {target}) for target in targets))

    findings = set()
    raw = set()
    wrapped = set()
    numeric = set()
    inferred = []

    def find(token, rule, field=""):
        findings.add(Finding(path, token, rule, field))

    def expanded(names):
        return set().union(*(aliases.get(name, {name}) for name in names))

    def raw_reference(chunk):
        return any(
            token.kind == "identifier" and (
                token.text in raw
                or token.text in RAW_MEMBERS and index and chunk[index - 1].text in {".", "?."}
            )
            for index, token in enumerate(chunk)
        )

    java_types = {
        index: expanded(types) for index, types in java_declaration_types(tokens).items()
    } if Path(path).suffix.lower() == ".java" else {}
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        types = set()
        if index in java_types:
            types = java_types[index]
        elif index + 1 < len(tokens) and tokens[index + 1].text == ":":
            types = expanded(declared_type(tokens, index + 2))
        elif index and tokens[index - 1].text in {"val", "var"}:
            if index + 1 < len(tokens) and tokens[index + 1].text == "=":
                inferred.append((token, expression_after(tokens, index + 2)))
        elif index and tokens[index - 1].text == "fun" and index + 1 < len(tokens) and tokens[index + 1].text == "(":
            depth, end = 1, index + 2
            while end < len(tokens) and depth:
                if tokens[end].text in {"(", ")"}:
                    depth += 1 if tokens[end].text == "(" else -1
                end += 1
            if end < len(tokens) and tokens[end].text == ":":
                types = expanded(declared_type(tokens, end + 1))
        elif index and tokens[index - 1].text in aliases:
            types = expanded({tokens[index - 1].text})
        elif index and (
            tokens[index - 1].text in {">", "]"}
            or money_name(token.text) and tokens[index - 1].kind == "identifier"
            and tokens[index - 1].text not in {"return", "class", "object", "interface", "typealias", "new", "throw"}
        ):
            types = expanded(java_type_prefix(tokens, index))
        if types & (UNSAFE_TYPES | INTEGER_TYPES):
            numeric.add(token.text)
        if "Money" in types and not types & (UNSAFE_TYPES | INTEGER_TYPES):
            wrapped.add(token.text)
        elif money_name(token.text) and types:
            if types & (UNSAFE_TYPES | INTEGER_TYPES):
                raw.add(token.text)
                if types & UNSAFE_TYPES:
                    find(token, "MG001", token.text)
            else:
                find(token, "MG005", token.text)

    def wrapped_expression(expression):
        if not expression or raw_reference(expression):
            return False
        if len(expression) == 1 and expression[0].text in wrapped:
            return True
        if len(expression) >= 4 and (
            "Money" in expanded({expression[0].text})
            and expression[1].text == "."
            and expression[2].text in {"parse", "ofMinorUnits", "fromWire"}
            and expression[3].text == "("
        ):
            depth = 0
            for index, token in enumerate(expression[3:], 3):
                if token.text in {"(", ")"}:
                    depth += 1 if token.text == "(" else -1
                if depth == 0:
                    return index == len(expression) - 1
            return False
        return (
            any(token.text in {"+", "-"} for token in expression)
            and all(token.text in wrapped for token in expression if token.kind == "identifier")
            and all(token.kind == "identifier" or token.text in {"+", "-", "(", ")"} for token in expression)
        )

    for _ in range(len(inferred) + 1):
        for token, expression in inferred:
            identifiers = {item.text for item in expression if item.kind == "identifier"}
            if raw_reference(expression):
                raw.add(token.text)
            if wrapped_expression(expression):
                wrapped.add(token.text)
            if money_name(token.text) and (
                any(floating_literal(item) for item in expression)
                or expanded(identifiers) & UNSAFE_TYPES
                or identifiers & CONVERSIONS
            ):
                raw.add(token.text)
                find(token, "MG001", token.text)
            if token.text not in wrapped and any(item.kind == "number" for item in expression):
                numeric.add(token.text)
    for token, _ in inferred:
        if money_name(token.text) and token.text not in wrapped and token.text not in raw:
            find(token, "MG005", token.text)

    money_context = any(
        tokens[index - 1].text in {"class", "object", "interface"}
        and (token.text == "Money" or token.text.endswith("MoneySerializer"))
        for index, token in enumerate(tokens) if index
    )
    for chunk in chunks + [expression for _, expression in inferred]:
        identifiers = {token.text for token in chunk if token.kind == "identifier"}
        keys = [token for token in chunk if token.kind == "money_key"]
        raw_flow = raw_reference(chunk)
        monetary = raw_flow or bool(keys) or any(money_name(name) for name in identifiers)
        for index, token in enumerate(chunk):
            if token.text in ARITHMETIC and raw_flow:
                find(token, "MG002")
            if token.text in ARITHMETIC_CALLS and raw_flow:
                find(token, "MG002")
            if token.text in CONVERSIONS and (monetary or money_context):
                find(token, "MG003")
            if token.text in NUMBER_SERIALIZERS and (monetary or money_context):
                find(token, "MG004", keys[0].text if keys else "")
            if token.text == "JsonPrimitive" and (
                raw_flow or monetary and (
                    any(item.kind == "number" for item in chunk) or bool(identifiers & numeric)
                )
            ):
                find(token, "MG004", keys[0].text if keys else "")
            if keys and expanded(identifiers) & (UNSAFE_TYPES | INTEGER_TYPES):
                find(keys[0], "MG004", keys[0].text)
    # A numeric aggregation's lambda is a separate statement, but its raw member still escapes.
    for index, token in enumerate(tokens):
        if token.text in {"sumOf", "average"}:
            tail = tokens[index + 1:]
            end = next((i for i, item in enumerate(tail) if item.text == "}"), len(tail))
            if raw_reference(tail[:end]):
                find(token, "MG002")
    return sorted(findings, key=lambda item: (item.path, item.token.line, item.token.column, item.rule, item.field))


def source_inventory(root):
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ScanFailure("invalid root")
    paths = []

    def refused_walk(error):
        raise ScanFailure("unreadable source directory") from error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=refused_walk):
        parent = Path(directory)
        if parent == root:
            directories[:] = [name for name in directories if name not in BUILD_DIRECTORIES]
        for name in directories:
            if (parent / name).is_symlink() or (parent / name).is_junction():
                raise ScanFailure("source directory symlink")
        for name in files:
            path = parent / name
            if path.suffix.lower() in SOURCE_SUFFIXES:
                if path.is_symlink():
                    raise ScanFailure("source file symlink")
                paths.append(path)
    if not paths:
        raise ScanFailure("empty JVM source inventory")
    return sorted(paths)


def check(root=ROOT):
    findings, scanned, dependency_sources = [], 0, 0
    if root == ROOT:
        provider_path = ROOT / "scripts/money_provider.py"
        spec = importlib.util.spec_from_file_location("money_provider", provider_path)
        provider = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(provider)
            dependency_sources = len(provider.verify_outputs(root))
        except (OSError, ValueError):
            findings.append(Finding("build/contracts-money", Token("", "", 1, 1), "MG000"))
    try:
        paths = source_inventory(root)
    except (OSError, ScanFailure):
        findings.append(Finding(".", Token("", "", 1, 1), "MG000"))
        paths = []
    for path in paths:
        name = str(path.relative_to(root))
        try:
            source = path.read_text(encoding="utf-8")
            scanned += 1
            findings.extend(analyze(name, source))
        except (OSError, UnicodeError, ScanFailure):
            findings.append(Finding(name, Token("", "", 1, 1), "MG000"))
    for finding in findings:
        print(finding.diagnostic(), file=sys.stderr)
    metric = {"event": "money_guard", "source_files_scanned": scanned, "violations": len(findings)}
    if dependency_sources:
        metric["dependency_sources_verified"] = dependency_sources
    print(json.dumps(metric))
    return 1 if findings else 0


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.exit(check())

"""Reject unsafe JVM money declarations, arithmetic and supported direct logging."""

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
JAVA_MODIFIERS = {
    "abstract", "default", "final", "native", "private", "protected", "public",
    "static", "strictfp", "synchronized", "transient", "volatile",
}
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
}) | {"record", "sealed", "permits", "var", "yield"}
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
OUTPUT_METHODS = {"print", "println", "printf", "format", "append"}
LOGGING_TYPES = {
    "java.io.PrintStream": OUTPUT_METHODS,
    "java.io.PrintWriter": OUTPUT_METHODS,
    "org.slf4j.Logger": {"trace", "debug", "info", "warn", "error"},
}
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUMBER = re.compile(r"(?:[0-9][A-Za-z0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?|\.[0-9]+[fFdD]?)")
OPERATORS = re.compile(r"::|\?\.|->|\+=|-=|\*=|/=|%=|\+\+|--|==|!=|<=|>=|&&|\|\|")
MESSAGES = {
    "MG001": "floating/decimal money type; use the Contracts Money wrapper",
    "MG002": "raw money arithmetic; use the Money value type's checked operators",
    "MG003": "floating/decimal money conversion is forbidden",
    "MG004": "numeric money serialization is forbidden; use the accepted Contracts seam",
    "MG005": "unresolved money type; declare the Contracts Money type explicitly",
    "MG006": "direct money logging/output is forbidden; use fixed MoneyDiagnostic metadata",
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


def tokenize(source, *, logging=False, kotlin=True):
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
        template = logging and kotlin and quote != "'"
        if template:
            emit("(", "template", start)
        index += len(quote)
        body_start = index
        interpolated = False
        while index < len(source):
            if source.startswith(quote, index):
                label = source[body_start:index]
                label = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), label)
                if template:
                    emit(")", "symbol", index)
                elif not interpolated:
                    kind = "money_key" if IDENTIFIER.fullmatch(label) and money_name(label) else "literal"
                    emit(label if kind == "money_key" else "", kind, start)
                return index + len(quote)
            if quote != '"""' and source[index] == "\\":
                index += 2
            elif quote != "'" and (not logging or kotlin) and source.startswith("${", index):
                interpolated = True
                if template:
                    emit("(", "symbol", index)
                index = code(index + 2, template=True)
                if template:
                    emit(")", "symbol", index - 1)
                    emit(",", "symbol", index - 1)
            elif quote != "'" and (not logging or kotlin) and source[index] == "$":
                match = IDENTIFIER.match(source, index + 1)
                if match:
                    interpolated = True
                    emit(match[0], "identifier", index + 1)
                    if template:
                        emit(",", "symbol", match.end())
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
        if end >= len(tokens) or tokens[end].text not in {"=", ",", ";", ":"}:
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


def java_type_scope(tokens, start, following):
    parameters = {}
    index = start + 1
    while index < following - 1:
        name = tokens[index].text
        end = index + 1
        if end < following - 1 and tokens[end].text == "extends":
            end += 1
            bound = end
            while end < following - 1 and tokens[end].text != ",":
                if tokens[end].text in {"<", "["}:
                    end = java_group_end(tokens, end)
                    if end is None:
                        raise ScanFailure("unterminated Java type bound")
                else:
                    end += 1
            parameters[name] = tokens[bound:end]
        else:
            if end < following - 1 and tokens[end].text != ",":
                raise ScanFailure("invalid Java type parameter")
            parameters[name] = []
        index = end + 1

    end = following
    while end < len(tokens) and tokens[end].text not in {"{", ";"}:
        if tokens[end].text in {"(", "[", "<"}:
            end = java_group_end(tokens, end)
            if end is None:
                raise ScanFailure("unterminated Java generic declaration")
        else:
            end += 1
    if end < len(tokens):
        end = java_group_end(tokens, end) if tokens[end].text == "{" else end + 1
        if end is None:
            raise ScanFailure("unterminated Java generic body")
    return start, end, parameters


def java_resolved_type(tokens, position, scopes):
    names, visited = set(), set()
    pending = [(tokens, position)]
    references_parameter = False
    while pending:
        current, position = pending.pop()
        for index, token in enumerate(current):
            qualified = (
                index > 0 and current[index - 1].text == "."
                or index + 2 < len(current) and current[index + 1].text == "."
                and current[index + 2].kind == "identifier"
            )
            scope = next((
                scope for scope in reversed(scopes)
                if not qualified and scope[0] <= position < scope[1] and token.text in scope[2]
            ), None)
            if scope is None:
                names.add(token.text)
                continue
            references_parameter = True
            key = (scope[0], token.text)
            if key in visited:
                continue
            visited.add(key)
            bound = scope[2][token.text]
            if bound:
                # An outer bound keeps its declaration environment through inner shadowing.
                pending.append((bound, scope[0]))
            else:
                names.add("Object")
    # Unbounded or cyclic parameters never recover an imported type with the same name.
    return names or {"Object"}, references_parameter


def java_declaration_types(tokens, type_positions=None, declaration_tokens=None):
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

    scopes = []
    for index in range(len(code) - 2):
        if (
            code[index].text in {"class", "interface", "record"}
            and code[index + 1].kind == "identifier" and code[index + 2].text == "<"
        ):
            following = java_group_end(code, index + 2)
            if following is None:
                raise ScanFailure("unterminated Java type parameters")
            scopes.append(java_type_scope(code, index + 2, following))
            if type_positions is not None:
                type_positions.update(positions[index + 2:following])

    candidates = []
    for start in range(len(code)):
        if start and code[start - 1].text not in {";", "{", "}", "(", ",", ":"}:
            continue
        index = start
        while index < len(code) and code[index].text in JAVA_MODIFIERS:
            index += 1
        type_start = index
        if index < len(code) and code[index].text == "<":
            following = java_group_end(code, index)
            if following is None:
                continue
            index = following
        if index >= len(code) or code[index].kind != "identifier":
            continue
        constructor = (
            type_start < index and code[index].text not in JAVA_NON_TYPES
            and index + 1 < len(code) and code[index + 1].text == "("
        )
        if (
            constructor or type_start < index and code[index].text == "void" and index + 2 < len(code)
            and code[index + 1].kind == "identifier" and code[index + 2].text == "("
        ):
            scopes.append(java_type_scope(code, type_start, index))
            if type_positions is not None:
                type_positions.update(positions[type_start:index])
                if constructor:
                    type_positions.add(positions[index])
        if constructor:
            # A constructor has no return type, and its name is not a value declaration.
            continue
        end = index + 1
        valid_type = True
        while end < len(code):
            # A qualifier may name a package rather than a restricted simple type.
            non_types = JAVA_RESERVED_WORDS if code[end].text == "." else JAVA_NON_TYPES
            if code[end - 1].text in non_types:
                valid_type = False
                break
            if code[end].text == "<":
                following = java_group_end(code, end)
                if following is None:
                    break
                end = following
            if end + 1 < len(code) and code[end].text == "." and code[end + 1].kind == "identifier":
                end += 2
            else:
                break
        if not valid_type:
            continue
        while end + 1 < len(code) and code[end].text == "[" and code[end + 1].text == "]":
            end += 2
        if [token.text for token in code[end:end + 3]] == [".", ".", "."]:
            end += 3
        if end >= len(code) or code[end].kind != "identifier" or code[end].text in JAVA_RESERVED_WORDS:
            continue
        tail = end + 1
        while tail + 1 < len(code) and code[tail].text == "[" and code[tail + 1].text == "]":
            tail += 2
        recognized_tail = tail < len(code) and code[tail].text in {"=", ",", ";", "(", ")", ":"}
        if recognized_tail and type_start < index and code[tail].text == "(":
            scopes.append(java_type_scope(code, type_start, index))
        if type_positions is not None and recognized_tail:
            # Keep type spans, not their spelling, out of the value-identifier fallback.
            type_positions.update(positions[type_start:end])
        candidates.append((index, end, recognized_tail))

    declarations = {}
    scopes.sort(key=lambda scope: scope[0])
    for index, end, recognized_tail in candidates:
        types, references_parameter = java_resolved_type(code[index:end], end, scopes)
        # Only a complete type at a declaration boundary may supply shared declarator types.
        declarators = list(java_declarators(code, end))
        declarations.update((positions[declarator], types) for declarator in declarators)
        if declaration_tokens is not None and recognized_tail:
            for declarator in {*declarators, end}:
                declaration_tokens[positions[declarator]] = [] if references_parameter else code[index:end]
        if references_parameter and recognized_tail:
            declarations[positions[end]] = types
    return declarations


def floating_literal(token):
    text = token.text.replace("_", "")
    return token.kind == "number" and (
        "." in text or bool(re.search(r"[eE]", text)) or text.endswith(("f", "F", "d", "D"))
    ) and not text.lower().startswith(("0x", "0b"))


def direct_logging(source, tokens, declarations, inferred, wrapped, raw, aliases, kotlin, java_types):
    # MG000..005 keep their original tokens; only logging needs language-aware literal boundaries.
    original = tokens
    tokens = tokenize(source, logging=True, kotlin=kotlin)
    positions = {token: index for index, token in enumerate(tokens) if token.kind == "identifier"}
    declarations = {positions[original[index]]: names for index, names in declarations.items() if original[index] in positions}
    java_types = {positions[original[index]]: names for index, names in java_types.items() if original[index] in positions}
    inferred = [
        (token, expression_after(tokens, positions[token] + 2))
        for token, _ in inferred if token in positions
    ]
    chunks = list(statements(tokens))
    imports = {"System": "java.lang.System", "String": "java.lang.String"}
    for chunk in chunks:
        if not chunk or chunk[0].text != "import":
            continue
        names = [token.text for token in chunk[1:] if token.text not in {"static", ";"}]
        split = names.index("as") if "as" in names else len(names)
        path = "".join(names[:split])
        name = names[split + 1] if split < len(names) else path.rsplit(".", 1)[-1]
        imports[name] = path

    typed_names = {tokens[index].text for index in declarations}
    declared_names = typed_names | {token.text for token, _ in inferred}
    alias_positions = {}
    for token, _ in inferred:
        alias_positions.setdefault(token.text, set()).add(token)
    unambiguous_aliases = {name for name, positions in alias_positions.items() if len(positions) == 1} - typed_names
    local_types = {
        tokens[index + 1].text for index, token in enumerate(tokens[:-1])
        if token.text in {"class", "interface", "object", "record"} and tokens[index + 1].kind == "identifier"
    }
    callable_positions = {
        index for index in declarations if index + 1 < len(tokens) and tokens[index + 1].text == "("
    } | {index + 1 for index, token in enumerate(tokens[:-1]) if token.text == "fun"}
    callables = {tokens[index].text for index in callable_positions}

    def qualified(parts, static=False):
        if not parts or parts[0] in local_types or static and parts[0] in declared_names | callables:
            return None
        return ".".join([imports.get(parts[0], parts[0]), *parts[1:]])

    def chain(expression, start):
        parts = [expression[start].text]
        end = start + 1
        while end + 1 < len(expression) and expression[end].text in {".", "?."} and expression[end + 1].kind == "identifier":
            parts.append(expression[end + 1].text)
            end += 2
        return parts, end

    def sink_type(index, names):
        if names & {"<", "[", "(", "->"}:
            return None
        expression = java_types.get(index, [])
        if kotlin and index + 2 < len(tokens) and tokens[index + 1].text == ":":
            expression = tokens[index + 2:]
        if not expression or expression[0].kind != "identifier":
            return None
        parts, _ = chain(expression, 0)
        if kotlin and len(parts) == 1 and parts[0] in typed_names:
            return None
        name = qualified(parts)
        return name if name in LOGGING_TYPES else None

    receiver_types = {}
    values = {
        name: "money" if name in wrapped else "raw"
        for name in (wrapped | raw) - callables - alias_positions.keys()
    }
    for index, names in declarations.items():
        name = tokens[index].text
        receiver_types.setdefault(name, set()).add(sink_type(index, names))
        expanded = set().union(*(aliases.get(item, {item}) for item in names))
        if name in wrapped and "Money" not in expanded or name in raw and not expanded & (INTEGER_TYPES | UNSAFE_TYPES):
            values.pop(name, None)
    receivers = {name: next(iter(kinds)) for name, kinds in receiver_types.items() if len(kinds) == 1 and None not in kinds}

    def receiver(parts):
        if len(parts) == 1 and parts[0] in receivers:
            return receivers[parts[0]]
        if qualified(parts, static=True) in {"java.lang.System.out", "java.lang.System.err"}:
            return "java.io.PrintStream"
        return None

    def expression_parts(expression, separators):
        parts, start, index = [], 0, 0
        while index < len(expression):
            if expression[index].text in {"(", "[", "{"}:
                end = java_group_end(expression, index)
                if end is None:
                    return [expression]
                index = end
                continue
            if expression[index].text in separators and not (
                expression[index].text == "-"
                and (index == start or expression[index - 1].text in ARITHMETIC)
            ):
                parts.append(expression[start:index])
                start = index + 1
            index += 1
        return [*parts, expression[start:]]

    def carries_value(arguments):
        return any(value_kind(argument) is not None for argument in expression_parts(arguments, {","}))

    def value_kind(expression):
        if not expression:
            return None
        comparisons = {"==", "!=", "<", ">", "<=", ">=", "is", "instanceof", "&&", "||"}
        if len(expression_parts(expression, comparisons)) > 1:
            return None
        additions = expression_parts(expression, {"+"})
        if len(additions) > 1:
            kinds = [value_kind(part) for part in additions]
            return "money" if all(kind == "money" for kind in kinds) else "rendered" if any(kinds) else None
        if kotlin:
            subtractions = expression_parts(expression, {"-"})
            if len(subtractions) > 1:
                return "money" if all(value_kind(part) == "money" for part in subtractions) else None
            if expression[0].text == "-":
                return "money" if value_kind(expression[1:]) == "money" else None
        if expression[0].text == "(":
            index = java_group_end(expression, 0)
            if index is None:
                return None
            inner = expression[1:index - 1]
            kind = ("rendered" if carries_value(inner) else None) if expression[0].kind == "template" else value_kind(inner)
        elif expression[0].kind == "identifier":
            parts, end = chain(expression, 0)
            if end < len(expression) and expression[end].text == "(":
                stop = java_group_end(expression, end)
                if stop is None:
                    return None
                arguments = expression[end + 1:stop - 1]
                name = qualified(parts, static=True)
                kind = None
                if name in {
                    "com.pennilogic.contracts.money.Money.parse",
                    "com.pennilogic.contracts.money.Money.ofMinorUnits",
                    "com.pennilogic.contracts.money.Money.fromWire",
                    "com.pennilogic.contracts.money.Money.Companion.parse",
                    "com.pennilogic.contracts.money.Money.Companion.ofMinorUnits",
                    "com.pennilogic.contracts.money.Money.Companion.fromWire",
                }:
                    kind = "money"
                elif name == "java.lang.String.valueOf" and carries_value(arguments):
                    kind = "rendered"
                elif name in {
                    "kotlinx.serialization.json.Json.encodeToString",
                    "kotlinx.serialization.json.Json.Default.encodeToString",
                    "kotlinx.serialization.json.Json.encodeToJsonElement",
                } and any(
                    qualified(chain(arguments, position)[0]) in {
                        "com.pennilogic.contracts.money.MoneySerializer",
                        "com.pennilogic.contracts.money.MoneySerializer.INSTANCE",
                    }
                    for position, item in enumerate(arguments) if item.kind == "identifier"
                ) and carries_value(arguments):
                    kind = "rendered"
                index = stop
                if kind is not None:
                    return member_result(expression, index, kind)
            index = 2 if parts[0] == "this" and len(parts) > 1 else 0
            kind = values.get(expression[index].text)
            index += 1
        else:
            return None
        return member_result(expression, index, kind)

    def member_result(expression, index, kind):
        while index + 1 < len(expression) and expression[index].text in {".", "?."}:
            member = expression[index + 1].text
            index += 2
            if index < len(expression) and expression[index].text == "(":
                end = java_group_end(expression, index)
                if end is None:
                    return None
                no_arguments = end == index + 2
                if no_arguments and kind == "money" and member == "getMinorUnits":
                    kind = "raw"
                elif no_arguments and kind is not None and member == "toString":
                    kind = "rendered"
                else:
                    # Unknown calls are opaque; their result is not their receiver or arguments.
                    kind = None
                index = end
            else:
                kind = "raw" if kind == "money" and member in RAW_MEMBERS else None
        return kind if index == len(expression) else None

    def inferred_receiver(expression):
        if not expression:
            return None
        start = 1 if expression[0].text == "new" else 0
        if start >= len(expression) or expression[start].kind != "identifier":
            return None
        parts, end = chain(expression, start)
        if end == len(expression):
            return receiver(parts)
        if expression[end].text == "(" and java_group_end(expression, end) == len(expression):
            name = qualified(parts, static=True)
            if name in LOGGING_TYPES:
                return name
            if name == "org.slf4j.LoggerFactory.getLogger":
                return "org.slf4j.Logger"
        return None

    for _ in range(len(inferred) + 1):
        for token, expression in inferred:
            if token.text in unambiguous_aliases:
                kind = value_kind(expression)
                if kind is not None:
                    values[token.text] = kind
            kind = inferred_receiver(expression)
            if kind is not None and token.text in unambiguous_aliases:
                receivers[token.text] = kind

    for index, token in enumerate(tokens[:-1]):
        if token.kind != "identifier" or tokens[index + 1].text != "(" or index in callable_positions:
            continue
        start = index
        while start >= 2 and tokens[start - 1].text in {".", "?."} and tokens[start - 2].kind == "identifier":
            start -= 2
        parts, _ = chain(tokens, start)
        kind = receiver(parts[:-1])
        if start == index and index >= 2 and tokens[index - 1].text in {".", "?."} and tokens[index - 2].text == ")":
            opening, depth = index - 2, 1
            while opening and depth:
                opening -= 1
                depth += (tokens[opening].text == ")") - (tokens[opening].text == "(")
            beginning = opening - 1
            while beginning >= 2 and tokens[beginning - 1].text == "." and tokens[beginning - 2].kind == "identifier":
                beginning -= 2
            if depth == 0 and beginning >= 0:
                kind = inferred_receiver(tokens[beginning:index - 1])
        standard_print = (
            kotlin and token.text in {"print", "println"} and len(parts) == 1
            and token.text not in declared_names | callables | imports.keys()
        ) or kotlin and qualified(parts, static=True) in {"kotlin.io.print", "kotlin.io.println"}
        if not standard_print and (kind is None or token.text not in LOGGING_TYPES[kind]):
            continue
        end = java_group_end(tokens, index + 1)
        if end is not None and carries_value(tokens[index + 2:end - 1]):
            yield token


def analyze(path, source):
    tokens = tokenize(source)
    chunks = list(statements(tokens))
    aliases = {
        name: {name} for name in UNSAFE_TYPES | INTEGER_TYPES | {"Money"}
        | {kind.rsplit(".", 1)[-1] for kind in LOGGING_TYPES}
    }
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

    java_type_positions = set()
    java_logging_types = {}
    java = Path(path).suffix.lower() == ".java"
    java_types = java_declaration_types(tokens, java_type_positions, java_logging_types) if java else {}
    declarations = {}
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or index in java_type_positions:
            continue
        types = set()
        if index in java_types:
            types = java_types[index]
        elif index + 1 < len(tokens) and tokens[index + 1].text == ":":
            types = declared_type(tokens, index + 2)
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
                types = declared_type(tokens, end + 1)
        elif index and tokens[index - 1].text in aliases:
            types = {tokens[index - 1].text}
        elif index and (
            tokens[index - 1].text in {">", "]"}
            or money_name(token.text) and tokens[index - 1].kind == "identifier"
            and tokens[index - 1].text not in {"return", "class", "object", "interface", "typealias", "new", "throw"}
        ):
            types = java_type_prefix(tokens, index)
        if types:
            declarations[index] = types
        types = expanded(types)
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
    for token in direct_logging(source, tokens, declarations, inferred, wrapped, raw, aliases, not java, java_logging_types):
        find(token, "MG006")
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

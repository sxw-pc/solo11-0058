"""A deliberately small lexer for the LakePrune predicate language."""

from __future__ import annotations

from dataclasses import dataclass

from lakeprune.predicate_ast import PredicateError

@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    position: int

def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "'":
            value, end = _read_string(text, index)
            tokens.append(Token("STRING", value, index))
            index = end
            continue
        if char.isdigit() or (char == "." and index + 1 < length and text[index + 1].isdigit()):
            start = index
            seen_dot = False
            while index < length and (text[index].isdigit() or text[index] == "."):
                if text[index] == ".":
                    if seen_dot:
                        raise PredicateError("E_LEX_NUMBER", "invalid numeric literal", start)
                    seen_dot = True
                index += 1
            raw = text[start:index]
            tokens.append(Token("NUMBER", raw, start))
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < length and (text[index].isalnum() or text[index] == "_"):
                index += 1
            tokens.append(Token("IDENT", text[start:index], start))
            continue
        if char == "(":
            tokens.append(Token("LPAREN", char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token("RPAREN", char, index))
            index += 1
            continue
        if char == ",":
            tokens.append(Token("COMMA", char, index))
            index += 1
            continue
        if char == "-":
            tokens.append(Token("OP", char, index))
            index += 1
            continue
        if char in "<>!=":
            start = index
            two = text[index : index + 2]
            if two in {"<=", ">=", "!="}:
                tokens.append(Token("OP", two, start))
                index += 2
                continue
            if char in "<>=":
                tokens.append(Token("OP", char, start))
                index += 1
                continue
            raise PredicateError("E_LEX_OPERATOR", f"unsupported operator: {two!r}", start)
        raise PredicateError(
            "E_LEX_CHARACTER",
            f"unsupported character at position {index}: {char!r}",
            index,
        )
    tokens.append(Token("EOF", None, length))
    return tokens

def _read_string(text: str, start: int) -> tuple[str, int]:
    index = start + 1
    chunks: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                chunks.append("'")
                index += 2
                continue
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    raise PredicateError("E_LEX_STRING", "unterminated string literal", start)

"""Where a migration file's statements begin and end, without running it.

A scanner, not a parser: it knows only what hides a semicolon from Postgres
(comments, quoted strings and identifiers, dollar-quoted bodies, and the
`BEGIN ATOMIC ... END` body of a SQL-standard function) so it can find the
top-level statements and their first words.
"""

import dataclasses
import re

DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")
WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9$]*")


@dataclasses.dataclass(frozen=True)
class Statement:
    line: int
    words: tuple[str, ...]  # its words outside strings and comments, uppercased


def split(text: str) -> list[Statement]:
    found: list[Statement] = []
    words: list[str] = []
    start = 0
    line = 1
    atomic = 0  # nesting inside BEGIN ATOMIC: CASE ... END pairs, plus one
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    line += text[i] == "\n"
                    i += 1
        elif c in "'\"":
            escapes = c == "'" and i > 0 and text[i - 1] in "eE"
            i += 1
            while i < n:
                if escapes and text[i] == "\\":
                    i += 2
                    continue
                if text[i] == c:
                    if text.startswith(c * 2, i):
                        i += 2
                        continue
                    i += 1
                    break
                line += text[i] == "\n"
                i += 1
            if not words:
                start = line
        elif c == "$" and (match := DOLLAR.match(text, i)):
            close = text.find(match[0], match.end())
            end = n if close == -1 else close + len(match[0])
            line += text.count("\n", i, end)
            i = end
        elif c == ";":
            if atomic:
                i += 1
                continue
            if words:
                found.append(Statement(start, tuple(words)))
            words = []
            i += 1
        elif match := WORD.match(text, i):
            word = match[0].upper()
            if not words:
                start = line
            if words and words[-1] == "BEGIN" and word == "ATOMIC":
                atomic = 1
            elif atomic and word == "CASE":
                atomic += 1
            elif atomic and word == "END":
                atomic -= 1
            words.append(word)
            i = match.end()
        else:
            if not c.isspace() and not words:
                start = line
                words.append(c)
            i += 1
    if words:
        found.append(Statement(start, tuple(words)))
    return found


def transaction_control(statement: Statement) -> bool:
    """BEGIN, COMMIT, ROLLBACK and the rest: statements that would end or
    start the transaction pgforward runs the file in."""
    first, second = (statement.words + ("", ""))[:2]
    if first in ("BEGIN", "COMMIT", "END", "ABORT"):
        return True
    if first == "START" and second == "TRANSACTION":
        return True
    return first == "ROLLBACK" and second != "TO"

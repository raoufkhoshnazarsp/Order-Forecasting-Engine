"""Safe rendering of versioned SQL templates.

Every value interpolated into SQL is validated or escaped here so that no
caller can smuggle arbitrary SQL through a template placeholder.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from src.validation.target import (
    ALLOWED_COLUMNS,
    SQL_KEYWORDS,
    render_vertical_list,
)

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SQLRenderError(ValueError):
    """Raised when a template cannot be rendered safely."""


def quote_literal(value: str) -> str:
    """Escape a string as a single-quoted ClickHouse literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def validate_date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value)
    if not _DATE_RE.match(text):
        raise SQLRenderError(f"Invalid date literal: {text!r}; expected YYYY-MM-DD")
    return text


def validate_column(value: str) -> str:
    if not _IDENTIFIER_RE.match(str(value)):
        raise SQLRenderError(f"Invalid column identifier: {value!r}")
    lowered = str(value).lower()
    if lowered not in ALLOWED_COLUMNS:
        raise SQLRenderError(f"Column {value!r} is not in the approved whitelist")
    return lowered


def render_predicates(predicates: Iterable[str]) -> str:
    """Render a predicate block, allowing only whitelisted SQL tokens.

    Predicates are built by trusted code (TargetDefinition), but each token is
    still checked so a config value can never inject a statement separator.
    """
    lines: list[str] = []
    for predicate in predicates:
        text = " ".join(str(predicate).split())
        if any(token in text for token in (";", "--", "/*", "*/")):
            raise SQLRenderError(f"Destructive token in predicate: {predicate!r}")
        # Blank out quoted literals: their contents are data, not SQL tokens.
        scrubbed = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
        for token in re.findall(r"[A-Za-z_.]+", scrubbed):
            upper = token.upper()
            if upper in SQL_KEYWORDS:
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$", token):
                column = token.split(".", 1)[1].lower()
                if column not in ALLOWED_COLUMNS:
                    raise SQLRenderError(f"Unapproved column in predicate: {token!r}")
                continue
            raise SQLRenderError(f"Unexpected token in predicate: {token!r}")
        lines.append("AND " + text)
    return "\n      ".join(lines)


def render_template(
    template: str,
    *,
    start_date: Any,
    end_date: Any,
    extra: dict[str, str] | None = None,
    verticals: Sequence[str] | None = None,
) -> str:
    values: dict[str, str] = {
        "start_date": validate_date(start_date),
        "end_date": validate_date(end_date),
    }
    if verticals is not None:
        values["vertical_list"] = render_vertical_list(verticals)
    if extra:
        values.update(extra)

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise SQLRenderError(f"No value supplied for placeholder {key!r}")
        return values[key]

    rendered = _PLACEHOLDER.sub(repl, template)
    leftover = _PLACEHOLDER.findall(rendered)
    if leftover:
        raise SQLRenderError(f"Unrendered placeholders: {leftover}")
    return rendered

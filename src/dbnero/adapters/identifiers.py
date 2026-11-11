"""Engine-specific identifier validation and quoting for dynamic SQL.

Only object *names* are ever interpolated into SQL; values always use bound
parameters. Names are validated first, then quoted with the engine's rules.
"""

from __future__ import annotations

from dbnero.core.models import Engine

MAX_IDENTIFIER_LENGTH = {
    Engine.POSTGRESQL: 63,
    Engine.MYSQL: 64,
    Engine.MSSQL: 128,
}


class InvalidIdentifier(ValueError):
    pass


def validate_identifier(name: str, engine: Engine) -> str:
    if not name or not name.strip():
        raise InvalidIdentifier("Identifier must not be empty")
    if "\x00" in name or any(ord(ch) < 32 for ch in name):
        raise InvalidIdentifier("Identifier contains control characters")
    limit = MAX_IDENTIFIER_LENGTH[engine]
    length = len(name.encode("utf-8")) if engine is Engine.POSTGRESQL else len(name)
    if length > limit:
        raise InvalidIdentifier(f"Identifier longer than {limit} characters")
    if engine is Engine.MYSQL and name.endswith(" "):
        raise InvalidIdentifier("MySQL identifiers cannot end with a space")
    return name


def quote_identifier(name: str, engine: Engine) -> str:
    """Validate and quote a single identifier for the given engine."""
    validate_identifier(name, engine)
    if engine is Engine.POSTGRESQL:
        return '"' + name.replace('"', '""') + '"'
    if engine is Engine.MYSQL:
        return "`" + name.replace("`", "``") + "`"
    if engine is Engine.MSSQL:
        return "[" + name.replace("]", "]]") + "]"
    raise AssertionError(f"Unhandled engine: {engine}")


def quote_qualified(parts: list[str], engine: Engine) -> str:
    return ".".join(quote_identifier(p, engine) for p in parts)

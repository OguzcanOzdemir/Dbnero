import pytest

from dbnero.adapters.identifiers import InvalidIdentifier, quote_identifier, quote_qualified
from dbnero.core.models import Engine


@pytest.mark.parametrize(
    ("engine", "name", "expected"),
    [
        (Engine.POSTGRESQL, "crm", '"crm"'),
        (Engine.POSTGRESQL, 'we"ird', '"we""ird"'),
        (Engine.MYSQL, "a`b", "`a``b`"),
        (Engine.MSSQL, "a]b", "[a]]b]"),
        (Engine.MSSQL, "Sales DB", "[Sales DB]"),
    ],
)
def test_quote(engine, name, expected):
    assert quote_identifier(name, engine) == expected


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "a\nb"])
def test_rejects_bad(bad):
    with pytest.raises(InvalidIdentifier):
        quote_identifier(bad, Engine.POSTGRESQL)


def test_length_limits():
    quote_identifier("a" * 63, Engine.POSTGRESQL)
    with pytest.raises(InvalidIdentifier):
        quote_identifier("a" * 64, Engine.POSTGRESQL)
    # PostgreSQL limit is in bytes
    with pytest.raises(InvalidIdentifier):
        quote_identifier("ş" * 32, Engine.POSTGRESQL)
    quote_identifier("a" * 128, Engine.MSSQL)


def test_mysql_trailing_space():
    with pytest.raises(InvalidIdentifier):
        quote_identifier("db ", Engine.MYSQL)


def test_qualified():
    assert quote_qualified(["dbo", "t"], Engine.MSSQL) == "[dbo].[t]"

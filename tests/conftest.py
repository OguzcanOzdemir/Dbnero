from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dbnero.storage.catalog import Catalog


@pytest.fixture
def catalog(tmp_path) -> Iterator[Catalog]:
    cat = Catalog(tmp_path / "catalog.sqlite3")
    yield cat
    cat.close()

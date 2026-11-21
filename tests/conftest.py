"""Shared test setup: one database connection, every test rolled back afterwards.

The tests read the database after a full run:
    python src/load_sources.py --port 5433
    python src/pipeline.py run-year --port 5433
Then:
    python -m pytest -s -v tests --port 5433
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import connect  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--host", default="localhost")
    parser.addoption("--port", default="5432")
    parser.addoption("--user", default="postgres")


@pytest.fixture(scope="session")
def con(request):
    args = SimpleNamespace(host=request.config.getoption("--host"),
                           port=request.config.getoption("--port"),
                           user=request.config.getoption("--user"))
    c = connect(args)
    yield c
    c.close()


@pytest.fixture
def cur(con):
    """A cursor inside a transaction that is always rolled back, so tests never change the data."""
    with con.cursor() as c:
        yield c
    con.rollback()


@pytest.fixture
def one(cur):
    def _one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()[0]
    return _one

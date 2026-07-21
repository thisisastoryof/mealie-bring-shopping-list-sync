"""Shared fixtures and in-memory fakes for the test suite.

The app config reads required settings at import time, so we inject harmless
test values into the environment *before* anything under ``app`` is imported.
No real Mealie/Bring credentials are involved — every external call is served
by the in-memory fakes in :mod:`tests.fakes`.
"""
import os

os.environ.setdefault("MEALIE_BASE_URL", "http://mealie.test")
os.environ.setdefault("MEALIE_API_KEY", "test-key")
os.environ.setdefault("MEALIE_SHOPPING_LIST_ID", "list-1")
os.environ.setdefault("BRING_EMAIL", "test@example.com")
os.environ.setdefault("BRING_PASSWORD", "secret")
os.environ.setdefault("BRING_LIST_NAME", "Shopping")
os.environ.setdefault("DB_PATH", ":memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
import app.models  # noqa: E402,F401  (register tables on the metadata)

from tests.fakes import FakeBring, FakeMealie, make_reconciler, run_cycle  # noqa: E402


@pytest.fixture
def db():
    """A fresh, isolated in-memory SQLite session with the schema created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def mealie():
    return FakeMealie()


@pytest.fixture
def bring():
    return FakeBring()


@pytest.fixture
def reconciler(mealie, bring):
    return make_reconciler(mealie, bring)


@pytest.fixture
def cycle(reconciler, db, mealie, bring):
    """Run one reconcile cycle against the fakes' current snapshots."""

    async def _run(units=None):
        await run_cycle(reconciler, db, mealie, bring, units)

    return _run

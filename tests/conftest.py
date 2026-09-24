import os
import tempfile
from pathlib import Path

import pytest

_tmp = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["BACKUP_DIR"] = str(_tmp / "backups")

from app import recurring  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.seed import seed_categories  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    recurring._last_run = None
    session = SessionLocal()
    seed_categories(session)
    yield session
    session.close()

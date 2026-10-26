import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Production (Docker) uses MariaDB, e.g. mysql+pymysql://budget:pw@db:3306/budget?charset=utf8mb4
# Local development/testing falls back to a SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./budget.db")

_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _kwargs["connect_args"] = {"check_same_thread": False}
else:
    _kwargs["pool_recycle"] = 3600

engine = create_engine(DATABASE_URL, **_kwargs)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_foreign_keys(dbapi_conn, _record):
        # SQLite ignores ON DELETE rules unless asked, per connection; MariaDB always applies them.
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass

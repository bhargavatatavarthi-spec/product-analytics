"""SQLAlchemy engine / session wiring."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATABASE_URL

# check_same_thread=False lets FastAPI's threadpool share the SQLite connection.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_missing_columns(bind: Engine) -> None:
    """Add columns that exist on the models but not yet in the DB.

    ``create_all`` only creates missing *tables*, never new *columns* on a table
    that already exists — so a persisted database (e.g. the SQLite file on a
    deployment's disk) would break the moment the models grow a column. This
    lightweight, dialect-agnostic self-heal runs ``ALTER TABLE ADD COLUMN`` for
    each missing column so an existing deployment survives a schema addition
    without a manual wipe or a full migration tool. Columns are added nullable
    (the compiled type carries no NOT NULL), so pre-existing rows simply get
    NULL and new writes supply the value via the model's Python-side defaults.
    """
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table — create_all already handled it
        present = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            col_type = column.type.compile(dialect=bind.dialect)
            stmt = text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
            with bind.begin() as conn:
                conn.execute(stmt)


def init_db() -> None:
    """Create missing tables, then add any columns new since the DB was created."""
    from . import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)

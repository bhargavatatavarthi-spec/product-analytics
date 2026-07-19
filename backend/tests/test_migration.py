"""The self-healing schema migration in db.init_db / _add_missing_columns."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app import models  # noqa: F401  (register tables on Base)
from app.db import _add_missing_columns


def test_self_heal_adds_missing_column(tmp_path):
    """A DB created before `created_on` existed should gain the column, not break."""
    dbfile = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{dbfile}", future=True)

    # Simulate a legacy `leads` table from before the column was added, with a row.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY, lead_id VARCHAR, current_stage VARCHAR)"
        ))
        conn.execute(text(
            "INSERT INTO leads (lead_id, current_stage) VALUES ('L1', 'Offer Generated')"
        ))

    assert "created_on" not in {c["name"] for c in inspect(engine).get_columns("leads")}

    _add_missing_columns(engine)

    cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    assert "created_on" in cols  # the column that broke cohorts is now present

    # The pre-existing row survives, with NULL for the newly added column.
    with engine.begin() as conn:
        row = conn.execute(text("SELECT lead_id, created_on FROM leads")).one()
    assert row.lead_id == "L1"
    assert row.created_on is None
    engine.dispose()


def test_migration_is_noop_on_current_schema(tmp_path):
    """Running the self-heal against an already-current schema changes nothing."""
    dbfile = tmp_path / "current.db"
    engine = create_engine(f"sqlite:///{dbfile}", future=True)
    models.Base.metadata.create_all(engine)
    before = {c["name"] for c in inspect(engine).get_columns("leads")}
    _add_missing_columns(engine)  # should not raise or duplicate
    after = {c["name"] for c in inspect(engine).get_columns("leads")}
    assert before == after
    engine.dispose()

"""Contract guard: migrations must build the whole schema on a clean DB."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head_on_clean_database(tmp_path):
    """``alembic upgrade head`` on an empty DB creates every table/column."""
    db_path = tmp_path / "clean.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
    }

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "orders",
            "order_items",
            "cart_items",
            "wishlist_items",
            "banners",
            "banner_click_events",
            "subscriptions",
        } <= tables

        order_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(orders)")
        }
        assert {
            "idempotency_key",
            "request_hash",
            "delivery_cost",
            "address_id",
            "payment_method_id",
            "comment",
            "cancel_reason",
            "cancel_retry_count",
            "cancel_next_retry_at",
            "cancel_last_error",
            "paid_at",
        } <= order_columns

        revision = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert revision == "007_add_cancel_retry_fields"


def test_order_primary_keys_are_uuid_strings(tmp_path):
    """Order ids are TEXT/uuid strings, never integer autoincrement."""
    db_path = tmp_path / "types.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        for table in ("orders", "order_items", "cart_items", "wishlist_items"):
            columns = {
                row[1]: row[2]
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            id_column = next(
                name
                for name in columns
                if name.endswith("_id") and name != "user_id"
            )
            assert columns[id_column].upper().startswith("VARCHAR"), (
                table,
                id_column,
                columns[id_column],
            )
            assert "INT" not in columns[id_column].upper()

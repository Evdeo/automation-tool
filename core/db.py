import csv
import json
import numbers
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config


_known_tables = set()


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(config.DB_PATH, timeout=30)


def _to_native(value):
    # Convert numpy scalars / 0-d arrays to native Python so isinstance
    # checks below catch them correctly.
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return value.item()
        except (TypeError, ValueError, AttributeError):
            pass
    return value


def _sqlite_type(value):
    value = _to_native(value)
    if isinstance(value, bool):
        return "INTEGER"
    # numbers.Integral covers int + numpy.int{8,16,32,64}; numbers.Real covers
    # float + numpy.float{16,32,64}. Both pulled in via the .item() path above
    # for numpy scalars, but this is the second line of defence.
    if isinstance(value, numbers.Integral):
        return "INTEGER"
    if isinstance(value, numbers.Real):
        return "REAL"
    return "TEXT"


def _encode(value):
    if isinstance(value, bool):
        return int(value)
    # Numpy: .tolist() returns a list for arrays and a Python scalar for
    # scalars / 0-d arrays.  Only JSON-encode if it's actually a list.
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            converted = value.tolist()
        except (TypeError, ValueError, AttributeError):
            converted = None
        if isinstance(converted, list):
            return json.dumps(converted)
        if converted is not None:
            value = converted  # numpy scalar → native Python; fall through
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    if isinstance(value, set):
        return json.dumps(sorted(value, key=str))
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _ensure_table(conn, table, values):
    if table in _known_tables:
        return
    cols = ["ts TEXT NOT NULL"]
    for i, v in enumerate(values):
        cols.append(f"c{i} {_sqlite_type(v)}")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    _known_tables.add(table)


def log(table, *values):
    conn = _connect()
    try:
        _ensure_table(conn, table, values)
        placeholders = ", ".join(["?"] * (len(values) + 1))
        conn.execute(
            f"INSERT INTO {table} VALUES ({placeholders})",
            (_utc_now(), *(_encode(v) for v in values)),
        )
        conn.commit()
    finally:
        conn.close()


def import_csv(csv_path, table):
    conn = _connect()
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            cols_def = ", ".join(f'"{h}" TEXT' for h in header)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols_def})")
            placeholders = ", ".join(["?"] * len(header))
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                (tuple(row) for row in reader),
            )
            conn.commit()
        _known_tables.add(table)
    finally:
        conn.close()


def list_tables():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()

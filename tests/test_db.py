"""Unit tests for core/db.py — value encoding for log()'s schema-less writes.

Confirms the contract:
- ints / floats / bools land in INTEGER / REAL / INTEGER columns,
- arrays of any flavour (list, tuple, numpy.ndarray, set) land in TEXT
  columns as JSON,
- numpy scalars are coerced to native Python before storage,
- nested structures round-trip cleanly.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import db  # noqa: E402


class TestEncode(unittest.TestCase):
    def test_int_passthrough(self):
        self.assertEqual(db._encode(42), 42)
        self.assertEqual(db._sqlite_type(42), "INTEGER")

    def test_float_passthrough(self):
        self.assertEqual(db._encode(3.14), 3.14)
        self.assertEqual(db._sqlite_type(3.14), "REAL")

    def test_bool_becomes_int(self):
        self.assertEqual(db._encode(True), 1)
        self.assertEqual(db._encode(False), 0)
        self.assertEqual(db._sqlite_type(True), "INTEGER")

    def test_string_passthrough(self):
        self.assertEqual(db._encode("hello"), "hello")
        self.assertEqual(db._sqlite_type("hello"), "TEXT")

    def test_list_becomes_json(self):
        self.assertEqual(db._encode([1, 2, 3]), "[1, 2, 3]")
        self.assertEqual(db._sqlite_type([1, 2, 3]), "TEXT")

    def test_tuple_becomes_json_list(self):
        self.assertEqual(db._encode((1, 2, 3)), "[1, 2, 3]")

    def test_dict_becomes_json(self):
        self.assertEqual(json.loads(db._encode({"x": 1, "y": 2})),
                         {"x": 1, "y": 2})

    def test_set_becomes_sorted_json_list(self):
        # sets are unordered — encoder sorts for stable output
        self.assertEqual(db._encode({3, 1, 2}), "[1, 2, 3]")

    def test_nested(self):
        v = {"size": 10, "values": [1, 2, 3], "nested": {"k": [4, 5]}}
        out = db._encode(v)
        self.assertEqual(json.loads(out), v)


class TestNumpyEncode(unittest.TestCase):
    """Numpy is in requirements.txt so it's always available, but if some
    future repo strips it, these tests just skip rather than fail."""

    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            cls.np = np
        except ImportError:
            raise unittest.SkipTest("numpy not installed")

    def test_numpy_array_becomes_json(self):
        arr = self.np.array([1.1, 2.2, 3.3, 4.4, 5.5])
        out = db._encode(arr)
        self.assertEqual(json.loads(out), [1.1, 2.2, 3.3, 4.4, 5.5])

    def test_numpy_int_array_becomes_json(self):
        arr = self.np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(json.loads(db._encode(arr)), list(range(1, 11)))

    def test_numpy_2d_array_becomes_nested_json(self):
        arr = self.np.array([[1, 2], [3, 4]])
        self.assertEqual(json.loads(db._encode(arr)), [[1, 2], [3, 4]])

    def test_numpy_scalar_becomes_native(self):
        self.assertEqual(db._encode(self.np.int64(42)), 42)
        self.assertEqual(db._encode(self.np.float32(3.5)), 3.5)
        self.assertEqual(db._sqlite_type(self.np.int64(42)), "INTEGER")
        self.assertEqual(db._sqlite_type(self.np.float32(3.5)), "REAL")

    def test_numpy_array_typed_as_text(self):
        arr = self.np.arange(10)
        self.assertEqual(db._sqlite_type(arr), "TEXT")


class TestLogRoundtrip(unittest.TestCase):
    """End-to-end: db.log() → sqlite → SELECT — including arrays."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="db_test_"))
        self._orig_db = config.DB_PATH
        config.DB_PATH = str(self.tmp / "test.db")
        db._known_tables.clear()

    def tearDown(self):
        config.DB_PATH = self._orig_db
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_table(self, table):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            return conn.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            conn.close()

    def _column_types(self, table):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            return [r[2] for r in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def test_log_array_of_ten(self):
        # The exact pattern the user asked about.
        db.log("data", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        rows = self._read_table("data")
        self.assertEqual(len(rows), 1)
        ts, c0 = rows[0]
        self.assertEqual(json.loads(c0), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(self._column_types("data"), ["TEXT", "TEXT"])

    def test_log_mixed_columns(self):
        db.log("measurements", "session_a", [10, 20, 30], 3, 0.95)
        rows = self._read_table("measurements")
        ts, name, arr, count, rate = rows[0]
        self.assertEqual(name, "session_a")
        self.assertEqual(json.loads(arr), [10, 20, 30])
        self.assertEqual(count, 3)
        self.assertAlmostEqual(rate, 0.95)
        self.assertEqual(self._column_types("measurements"),
                         ["TEXT", "TEXT", "TEXT", "INTEGER", "REAL"])

    def test_log_numpy_array(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        db.log("ndarr", np.array([1.5, 2.5, 3.5]))
        rows = self._read_table("ndarr")
        ts, c0 = rows[0]
        self.assertEqual(json.loads(c0), [1.5, 2.5, 3.5])

    def test_log_dict(self):
        db.log("metrics", {"x": 1, "y": 2})
        rows = self._read_table("metrics")
        self.assertEqual(json.loads(rows[0][1]), {"x": 1, "y": 2})

    def test_multiple_rows_same_table(self):
        db.log("series", [1, 2, 3])
        db.log("series", [4, 5, 6])
        db.log("series", [7, 8, 9])
        rows = self._read_table("series")
        self.assertEqual(len(rows), 3)
        self.assertEqual([json.loads(r[1]) for r in rows],
                         [[1, 2, 3], [4, 5, 6], [7, 8, 9]])

    def test_kitchen_sink_in_single_call(self):
        """Documents that a single log call can mix every supported type.
        Each positional arg → its own column; each gets encoded independently."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        db.log(
            "trial",
            "run_alpha",                     # str       → TEXT
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], # list      → TEXT (JSON)
            np.array([0.1, 0.2, 0.3]),       # ndarray   → TEXT (JSON)
            {"k": "v", "n": 42},             # dict      → TEXT (JSON)
            (10, 20, 30),                    # tuple     → TEXT (JSON)
            {7, 1, 5},                       # set       → TEXT (JSON sorted)
            True,                            # bool      → INTEGER (1)
            42,                              # int       → INTEGER
            np.int64(100),                   # np scalar → INTEGER
            3.14,                            # float     → REAL
            np.float32(2.5),                 # np scalar → REAL
            "finished",                      # str       → TEXT
        )
        types = self._column_types("trial")
        self.assertEqual(
            types,
            ["TEXT",                         # ts
             "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT",
             "INTEGER", "INTEGER", "INTEGER",
             "REAL", "REAL",
             "TEXT"],
        )
        row = self._read_table("trial")[0]
        self.assertEqual(row[1], "run_alpha")
        self.assertEqual(json.loads(row[2]), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(json.loads(row[3]), [0.1, 0.2, 0.3])
        self.assertEqual(json.loads(row[4]), {"k": "v", "n": 42})
        self.assertEqual(json.loads(row[5]), [10, 20, 30])
        self.assertEqual(json.loads(row[6]), [1, 5, 7])  # set is sorted
        self.assertEqual(row[7], 1)                       # True → 1
        self.assertEqual(row[8], 42)
        self.assertEqual(row[9], 100)                     # np.int64
        self.assertAlmostEqual(row[10], 3.14)
        self.assertAlmostEqual(row[11], 2.5)
        self.assertEqual(row[12], "finished")


class TestSchemaConstraints(unittest.TestCase):
    """Document the schema-evolution rules: column count is locked by the
    first call; column types are SQLite type-affinity hints (so mismatched
    types in the same column are stored, not rejected)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="db_test_schema_"))
        self._orig_db = config.DB_PATH
        config.DB_PATH = str(self.tmp / "test.db")
        db._known_tables.clear()

    def tearDown(self):
        config.DB_PATH = self._orig_db
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_different_column_count_fails(self):
        db.log("events", "a", "b", "c")
        # Three-column schema is now locked; supplying 2 or 4 must error.
        with self.assertRaises(sqlite3.OperationalError):
            db.log("events", "a", "b")
        with self.assertRaises(sqlite3.OperationalError):
            db.log("events", "a", "b", "c", "d")

    def test_mixed_types_in_same_column_position_allowed(self):
        # SQLite uses type AFFINITY, not strict typing. A column declared
        # INTEGER will happily store a float or a string — the value's
        # actual type wins per-row.
        db.log("counter", 5)         # c0 declared INTEGER
        db.log("counter", 5.7)
        db.log("counter", "oops")
        conn = sqlite3.connect(config.DB_PATH)
        try:
            rows = conn.execute("SELECT c0, typeof(c0) FROM counter").fetchall()
        finally:
            conn.close()
        # Each row keeps its native type
        self.assertEqual(rows[0], (5, "integer"))
        self.assertAlmostEqual(rows[1][0], 5.7)
        self.assertEqual(rows[1][1], "real")
        self.assertEqual(rows[2], ("oops", "text"))

    def test_collection_columns_can_change_shape_per_row(self):
        # A column declared TEXT (because the first call passed a list)
        # still accepts strings, dicts, tuples — anything _encode handles.
        db.log("payload", "id1", [1, 2, 3])
        db.log("payload", "id2", "plain string")
        db.log("payload", "id3", {"x": 1})
        conn = sqlite3.connect(config.DB_PATH)
        try:
            rows = conn.execute("SELECT c0, c1 FROM payload ORDER BY rowid").fetchall()
        finally:
            conn.close()
        self.assertEqual(json.loads(rows[0][1]), [1, 2, 3])
        self.assertEqual(rows[1][1], "plain string")
        self.assertEqual(json.loads(rows[2][1]), {"x": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)

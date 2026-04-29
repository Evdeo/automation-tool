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


if __name__ == "__main__":
    unittest.main(verbosity=2)

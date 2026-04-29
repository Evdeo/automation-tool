import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


st.set_page_config(page_title="Automation Runs", layout="wide")
st.title("Automation Runs")
st.caption(f"DB: {config.DB_PATH}  ·  refresh every {config.DASHBOARD_REFRESH_SEC}s")


def _read_only_conn():
    uri = f"file:{config.DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _tail(conn, table, limit=200):
    return pd.read_sql_query(
        f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT {limit}", conn
    )


try:
    conn = _read_only_conn()
    tables = _tables(conn)
    if not tables:
        st.info("No tables yet. Run a test loop to populate the database.")
    for t in tables:
        st.subheader(t)
        df = _tail(conn, t)
        st.dataframe(df, use_container_width=True, hide_index=True)
    conn.close()
except sqlite3.OperationalError as e:
    st.error(f"Cannot open DB: {e}")

time.sleep(config.DASHBOARD_REFRESH_SEC)
st.rerun()

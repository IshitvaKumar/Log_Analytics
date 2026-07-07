import sqlite3


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:   # ✅ use context manager
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            entity_id    TEXT,
            action       TEXT,
            payload_json TEXT NOT NULL,
            raw_line     TEXT,
            hash         TEXT NOT NULL UNIQUE
        );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_entity ON events(kind, entity_id);")

        conn.commit()

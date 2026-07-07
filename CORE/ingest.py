import json
import hashlib
from typing import Optional, Dict, Any
from .database import init_db, get_conn
from .parser import parse_line

BATCH_SIZE = 1000

def _make_hash(kind: str, timestamp: str, entity_id: Optional[str], raw_line: str) -> str:
    base = f"{kind}|{timestamp}|{entity_id or ''}|{raw_line}"
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()

def _get_entity_id(kind: str, payload: Dict[str, Any]) -> Optional[str]:
    # synced with parser: parser should normalize to payload["entity_id"]
    eid = payload.get("entity_id")
    return str(eid) if eid is not None else None

def _get_action(kind: str, payload: Dict[str, Any]) -> Optional[str]:
    # synced with parser: parser should normalize to payload["action"]
    act = payload.get("action")
    return str(act) if act is not None else None

def ingest_log_file(log_path: str, db_path: str):
    init_db(db_path)

    conn = get_conn(db_path)
    cur = conn.cursor()

    total_lines = 0
    parsed = 0
    inserted = 0
    skipped = 0
    batch = []

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            total_lines += 1
            parsed_obj = parse_line(raw_line)
            if not parsed_obj:
                continue

            kind = parsed_obj.get("kind")
            payload = parsed_obj.get("data")
            if not kind or not isinstance(payload, dict):
                continue

            ts = payload.get("timestamp")
            if not ts:
                continue

            entity_id = _get_entity_id(kind, payload)
            action = _get_action(kind, payload)

            payload_json = json.dumps(payload, ensure_ascii=False)
            h = _make_hash(kind, str(ts), entity_id, raw_line.rstrip("\n"))

            batch.append((kind, str(ts), entity_id, action, payload_json, raw_line.rstrip("\n"), h))
            parsed += 1

            if len(batch) >= BATCH_SIZE:
                i, s = _flush(cur, batch)
                inserted += i
                skipped += s
                conn.commit()
                batch.clear()

    if batch:
        i, s = _flush(cur, batch)
        inserted += i
        skipped += s
        conn.commit()

    conn.close()
    return {
        "lines_scanned": total_lines,
        "events_parsed": parsed,
        "events_inserted": inserted,
        "skipped_duplicates": skipped
    }

def _flush(cur, batch):
    inserted = 0
    skipped = 0
    for row in batch:
        try:
            cur.execute("""
                INSERT INTO events(kind, timestamp, entity_id, action, payload_json, raw_line, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, row)
            inserted += 1
        except Exception:
            skipped += 1
    return inserted, skipped
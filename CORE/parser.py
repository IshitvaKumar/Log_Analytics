# CORE/parser.py

import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
from typing import Optional, Dict, Any, List

# --- TOML loader (py3.11+ has tomllib) ---
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

# -----------------------------
# Shared: dynamic auto-casting
# -----------------------------
def _auto_cast(v: str):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return ""
    try:
        if "." in s:
            return float(s)
        return int(s)
    except Exception:
        return s

# -----------------------------
# PINFO parser (unchanged except we normalize entity_id/action)
# -----------------------------
def parse_pinfo_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if "PSH::Pinfo" not in line:
        return None

    try:
        # Split timestamp
        first_split = line.split("|", 1)
        if len(first_split) < 2:
            return None

        timestamp = first_split[0].strip()
        second_part = first_split[1]

        # Extract prefix id (may not be the true PID in payload)
        if "-" not in second_part:
            return None

        id_part, data_part = second_part.split("-", 1)

        # prefix_pid is just a fallback
        prefix_pid = None
        try:
            prefix_pid = int(id_part.strip())
        except:
            prefix_pid = None

        # Remove marker
        data_part = data_part.replace("PSH::Pinfo,", "", 1)
        tokens = data_part.split(",")

        data: Dict[str, Any] = {"timestamp": timestamp}

        # Extract key-value pairs dynamically
        i = 0
        while i < len(tokens) - 1:
            key = tokens[i].strip()
            value = tokens[i + 1].strip()
            if key:
                data[key] = _auto_cast(value)
            i += 2

        # ✅ TRUE PID: prefer PID field from payload if present, else fallback to prefix
        true_pid = data.get("PID")
        if true_pid is None:
            true_pid = prefix_pid

        if true_pid is None:
            return None

        # Ensure PID is present in payload and normalized in DB
        data["PID"] = int(true_pid) if str(true_pid).isdigit() else true_pid
        data["entity_id"] = str(true_pid)     # ✅ DB entity_id == PID
        data["action"] = None

        return data

    except Exception:
        return None
# -----------------------------
# Generic CM parsing (brace + outside-key patterns)
# -----------------------------
BRACE_KV_RX = re.compile(r"\{\s*([A-Za-z0-9_]+)\s+([^}]*)\}")
OUTSIDE_KEY_BRACE_VALUE_RX = re.compile(r"(?<!\{)\b([A-Za-z0-9_]+)\s*\{([^}]*)\}")

@dataclass(frozen=True)
class CMTraceConfig:
    kind: str
    contains: tuple[str, ...]
    id_key: str = "Id"
    action_key: str = "Action"
    timestamp_source: str = "prefix"  # currently only "prefix"
    timestamp_format: str = "%H:%M:%S:%f"
    capture_source_prefix: bool = True  # capture "3944-VCMI" part if present

@lru_cache(maxsize=1)
def load_cm_trace_configs(config_dir: str = "CORE/config/traces") -> List[CMTraceConfig]:
    """
    Loads all *.toml configs under CORE/config/traces.
    Each file defines one CM trace type.
    """
    configs: List[CMTraceConfig] = []
    p = Path(config_dir)

    if not p.exists():
        return configs

    if tomllib is None:
        raise RuntimeError(
            "tomllib not available. Use Python 3.11+ or switch configs to JSON."
        )

    for fp in sorted(p.glob("*.toml")):
        with fp.open("rb") as f:
            raw = tomllib.load(f)

        kind = str(raw["kind"]).strip()
        contains = tuple(raw["contains"])
        id_key = str(raw.get("id_key", "Id"))
        action_key = str(raw.get("action_key", "Action"))
        timestamp_source = str(raw.get("timestamp_source", "prefix"))
        timestamp_format = str(raw.get("timestamp_format", "%H:%M:%S:%f"))
        capture_source_prefix = bool(raw.get("capture_source_prefix", True))

        configs.append(
            CMTraceConfig(
                kind=kind,
                contains=contains,
                id_key=id_key,
                action_key=action_key,
                timestamp_source=timestamp_source,
                timestamp_format=timestamp_format,
                capture_source_prefix=capture_source_prefix,
            )
        )

    return configs

def _extract_cm_fields(line: str) -> Dict[str, str]:
    """
    Extracts both:
      1) {Key Value}
      2) Key {Value}
    """
    fields: Dict[str, str] = {}

    # 1) inside braces: {Id 123}
    for k, v in BRACE_KV_RX.findall(line):
        fields[k] = v.strip()

    # 2) key outside, value in braces: Action {ADD}
    for k, v in OUTSIDE_KEY_BRACE_VALUE_RX.findall(line):
        # don't overwrite if already captured from braces
        fields.setdefault(k, v.strip())

    return fields

def parse_cm_line(line: str, cfg: CMTraceConfig) -> Optional[Dict[str, Any]]:
    # detection
    if not any(token in line for token in cfg.contains):
        return None

    line = line.rstrip("\n")

    # timestamp (prefix before '|')
    if cfg.timestamp_source == "prefix":
        if "|" not in line:
            return None
        ts_raw = line.split("|", 1)[0].strip()
        try:
            ts_dt = datetime.strptime(ts_raw, cfg.timestamp_format)
            ts = ts_dt.isoformat()
        except Exception:
            # if you prefer: keep raw timestamp instead of failing
            return None
    else:
        return None  # extend later if needed

    # optionally capture "3944-VCMI" part: between '|' and ':'
    source_prefix = None
    remainder = line.split("|", 1)[1]
    if cfg.capture_source_prefix and ":" in remainder:
        left, right = remainder.split(":", 1)
        source_prefix = left.strip()
        msg = right.strip()
    else:
        msg = remainder.strip()

    fields = _extract_cm_fields(msg)

    # must have an id (entity id)
    entity = fields.get(cfg.id_key)
    if not entity:
        return None

    # normalize action
    action = fields.get(cfg.action_key)

    data: Dict[str, Any] = {"timestamp": ts}

    # keep useful metadata
    data["trace"] = cfg.kind
    if source_prefix:
        data["source_prefix"] = source_prefix

    # attach all extracted fields dynamically (auto-cast)
    for k, v in fields.items():
        data[k] = _auto_cast(v)

    # ✅ normalize for ingestion (kind-agnostic)
    data["entity_id"] = str(entity)
    data["action"] = str(action) if action is not None else None

    return data

# ---------------------------------------
# Unified dispatcher (config-driven CM)
# ---------------------------------------
def parse_line(line: str):
    """
    Returns:
      {"kind": "pinfo", "data": {...}}
      {"kind": "<cm kind from config>", "data": {...}}
      None
    """
    row = parse_pinfo_line(line)
    if row:
        return {"kind": "pinfo", "data": row}

    for cfg in load_cm_trace_configs():
        row = parse_cm_line(line, cfg)
        if row:
            return {"kind": cfg.kind, "data": row}

    return None
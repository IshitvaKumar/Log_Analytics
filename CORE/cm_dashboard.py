# CORE/cm_dashboard.py
import streamlit as st
import pandas as pd
import sqlite3
import altair as alt
import json
import os
from collections import Counter
from typing import List, Optional


def _conn(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, check_same_thread=False)


def _read_sql(db_path: str, query: str, params=()) -> pd.DataFrame:
    conn = _conn(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def _pick_field(fields: List[str], keyword_groups: List[List[str]]) -> Optional[str]:
    lower_fields = [(f, f.lower()) for f in fields]
    for group in keyword_groups:
        for f, lf in lower_fields:
            if all(k in lf for k in group):
                return f
    return None


def _get_kind_fields(db_path: str, kind: str, sample_rows: int = 3000) -> List[str]:
    dfp = _read_sql(
        db_path,
        """
        SELECT payload_json
        FROM events
        WHERE kind = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (kind, sample_rows),
    )

    counts = Counter()
    for s in dfp["payload_json"].tolist():
        try:
            d = json.loads(s) if s else {}
            if isinstance(d, dict):
                counts.update(d.keys())
        except Exception:
            continue

    return [k for k, _ in counts.most_common()]


def render(db_path: str):
    st.title("CM Lifecycle Dashboard")

    if not os.path.exists(db_path):
        st.error(f"DB not found: {db_path}")
        return

    # -------------------------
    # Kinds
    # -------------------------
    kinds_df = _read_sql(
        db_path,
        """
        SELECT DISTINCT kind
        FROM events
        WHERE kind != 'pinfo'
        ORDER BY kind
        """
    )
    kinds = kinds_df["kind"].tolist()

    if not kinds:
        st.warning("No CM kinds found. (No rows where kind != 'pinfo').")
        return

    selected_kind = st.selectbox("Select CM trace type", kinds)

    # -------------------------
    # Kind-wide fields (dynamic from DB)
    # -------------------------
    kind_fields = _get_kind_fields(db_path, selected_kind, sample_rows=3000)

    # best-effort “summary fields” (still dynamic)
    status_field = _pick_field(kind_fields, [["status"], ["state"]])
    qty_goal_field = _pick_field(kind_fields, [["qtygoal"], ["qty", "goal"], ["goalqty"]])
    qty_hit_field = _pick_field(kind_fields, [["qtyhit"], ["qty", "hit"], ["filledqty"], ["fill", "qty"]])

    # -------------------------
    # Entities table (last event per entity)
    # -------------------------
    summary_df = _read_sql(
        db_path,
        """
        SELECT
            e.entity_id AS entity_id,
            e.timestamp AS last_time,
            e.action AS last_action,
            e.payload_json AS payload_json
        FROM events e
        JOIN (
            SELECT entity_id, MAX(id) AS max_id
            FROM events
            WHERE kind = ? AND entity_id IS NOT NULL
            GROUP BY entity_id
        ) m
        ON e.id = m.max_id
        ORDER BY e.entity_id DESC
        """,
        (selected_kind,),
    )

    if summary_df.empty:
        st.warning(
            f"No entities found for kind='{selected_kind}'. "
            f"This usually means entity_id is NULL for those events."
        )
        return

    enriched = []
    for _, r in summary_df.iterrows():
        payload = {}
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except Exception:
            payload = {}

        enriched.append(
            {
                "Entity ID": r["entity_id"],
                "Last Action": r["last_action"],
                "Status": payload.get(status_field) if status_field else None,
                "QtyGoal": payload.get(qty_goal_field) if qty_goal_field else None,
            }
        )

    entities_table = pd.DataFrame(enriched)

    st.subheader("Available Entities")

    # TRUE single-select like your CMORDER dashboard
    state_key = f"selected_entity__{selected_kind}"
    if state_key not in st.session_state:
        st.session_state[state_key] = None

    entities_table["Select"] = entities_table["Entity ID"] == st.session_state[state_key]

    edited = st.data_editor(
        entities_table,
        use_container_width=True,
        hide_index=True,
        column_config={"Select": st.column_config.CheckboxColumn("Select")},
        disabled=["Entity ID", "Last Action", "Status", "QtyGoal"],
        key=f"entity_table_{selected_kind}_{st.session_state[state_key]}",
    )

    selected_rows = edited[edited["Select"]]

    if len(selected_rows) == 0:
        if st.session_state[state_key] is not None:
            st.session_state[state_key] = None
            st.rerun()

    elif len(selected_rows) == 1:
        new_sel = selected_rows.iloc[0]["Entity ID"]
        if new_sel != st.session_state[state_key]:
            st.session_state[state_key] = new_sel
            st.rerun()

    else:
        # More than one checked — keep only the one that is NEW (wasn't selected before)
        previously_selected = st.session_state[state_key]
        new_ticks = selected_rows[selected_rows["Entity ID"] != previously_selected]

        if not new_ticks.empty:
            # User just ticked a new row — switch to it
            st.session_state[state_key] = new_ticks.iloc[0]["Entity ID"]
        else:
            # All selected rows were already selected — fall back to first
            st.session_state[state_key] = selected_rows.iloc[0]["Entity ID"]

        st.rerun()
    selected_entity = st.session_state[state_key]
    if not selected_entity:
        st.info("Tick the **Select** checkbox for one entity to see lifecycle events.")
        return

    # -------------------------
    # Lifecycle rows
    # -------------------------
    rows = _read_sql(
        db_path,
        """
        SELECT timestamp, action, payload_json
        FROM events
        WHERE kind = ? AND entity_id = ?
        ORDER BY id ASC
        """,
        (selected_kind, selected_entity),
    )

    if rows.empty:
        st.warning("No events found for this entity.")
        return

    payloads = []
    for s in rows["payload_json"].tolist():
        try:
            payloads.append(json.loads(s) if s else {})
        except Exception:
            payloads.append({})

    df = pd.json_normalize(payloads)

    # ✅ avoid collisions with payload keys (timestamp/action often exist in payload)
    df["event_timestamp"] = pd.to_datetime(rows["timestamp"], errors="coerce")
    df["event_action"] = rows["action"].values

    # Ensure kind-wide fields appear in selector even if missing for this entity
    for f in kind_fields:
        if f not in df.columns:
            df[f] = None

    # -------------------------
    # Field selector (dynamic)
    # -------------------------
    st.subheader("Event Details")

    base_fields = ["event_timestamp", "event_action"]
    all_fields = base_fields + [f for f in kind_fields if f not in base_fields]

    default_fields = base_fields + kind_fields[:6]
    default_fields = [f for f in default_fields if f in all_fields]

    selected_fields = st.multiselect(
        "Select fields to display",
        options=all_fields,
        default=default_fields,
    )

    events_df = df[selected_fields].copy() if selected_fields else df.copy()

    # ✅ Streamlit/pyarrow requires unique column names
    events_df = events_df.loc[:, ~events_df.columns.duplicated()].copy()

    # -------------------------
    # Summary
    # -------------------------
    st.subheader("Entity Summary")
    col1, col2, col3, col4 = st.columns(4)

    final_status = "N/A"
    if status_field and status_field in df.columns:
        s = df[status_field].dropna()
        if not s.empty:
            final_status = s.iloc[-1]

    total_events = len(df)

    fill_percentage = None
    if qty_hit_field and qty_goal_field and qty_hit_field in df.columns and qty_goal_field in df.columns:
        qty_hit = pd.to_numeric(df[qty_hit_field], errors="coerce").fillna(0).sum()
        qty_goal = pd.to_numeric(df[qty_goal_field], errors="coerce").fillna(0).sum()
        fill_percentage = (qty_hit / qty_goal * 100) if qty_goal != 0 else 0

    lifecycle_duration = 0
    if df["event_timestamp"].notna().sum() >= 2:
        lifecycle_duration = (df["event_timestamp"].iloc[-1] - df["event_timestamp"].iloc[0]).total_seconds()

    col1.metric("Final Status", str(final_status))
    col2.metric("Total Events", int(total_events))
    col3.metric("Fill Percentage", f"{fill_percentage:.2f}%" if fill_percentage is not None else "N/A")
    col4.metric("Lifecycle Duration (s)", f"{lifecycle_duration:.0f}")

    st.divider()

    st.subheader("Event Log")
    st.dataframe(events_df, use_container_width=True)

    # -------------------------
    # Chart (dynamic numeric field)
    # -------------------------
    if df["event_timestamp"].notna().any():
        numeric_candidates = []
        for c in all_fields:
            if c in ("event_timestamp", "event_action"):
                continue
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().any():
                numeric_candidates.append(c)

        if numeric_candidates:
            default_metric = qty_hit_field if qty_hit_field in numeric_candidates else numeric_candidates[0]
            metric = st.selectbox(
                "Select metric to chart",
                numeric_candidates,
                index=numeric_candidates.index(default_metric),
            )

            chart_df = df[["event_timestamp", metric]].copy()
            chart_df[metric] = pd.to_numeric(chart_df[metric], errors="coerce").fillna(0)
            chart_df = chart_df.dropna(subset=["event_timestamp"]).sort_values("event_timestamp")

            if chart_df.empty:
                st.info("No valid timestamp rows for charting.")
            else:
                line = alt.Chart(chart_df).mark_line(point=True).encode(
                    x=alt.X("event_timestamp:T", title="Time"),
                    y=alt.Y(f"{metric}:Q", title=metric),
                )
                st.subheader(f"{metric} Progression")
                st.altair_chart(line.interactive(), use_container_width=True)
        else:
            st.info("No numeric fields found for charting.")
    else:
        st.info("No valid timestamps found for charting.")
import streamlit as st
import sqlite3
import pandas as pd
import altair as alt
from collections import Counter
import json
import os


def get_connection(db_path):
    # ✅ do NOT cache this connection (avoids stale reads after processing/reset)
    return sqlite3.connect(db_path, check_same_thread=False)


def run_query(db_path, query, params=()):
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def calculate_mode(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return 0
    return Counter(values).most_common(1)[0][0]


def parse_timestamp(df):
    """
    Accepts:
      - "HH:MM:SS:ffffff"
      - "HH:MM:SS.ffffff"
    Returns df sorted by timestamp with invalid timestamps dropped.
    """
    if "timestamp" in df.columns:
        df = df.copy()
        df["timestamp"] = df["timestamp"].astype(str)

        # convert HH:MM:SS:ffffff -> HH:MM:SS.ffffff
        df["timestamp"] = df["timestamp"].str.replace(
            r"(\d{2}:\d{2}:\d{2}):(\d+)",
            r"\1.\2",
            regex=True
        )

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            format="%H:%M:%S.%f",
            errors="coerce"
        )

        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    return df


def render(db_path):
    st.title("PID-Based Process Monitoring Dashboard")
    st.caption("Enterprise Process Intelligence Monitoring")
    st.divider()

    # quick diagnostics (so we know immediately if DB has data)
    if not os.path.exists(db_path):
        st.error(f"DB not found: {db_path}")
        st.stop()

    diag = run_query(db_path, "SELECT kind, COUNT(DISTINCT entity_id) AS cnt FROM events WHERE entity_id IS NOT NULL GROUP BY kind ORDER BY cnt DESC")
    with st.expander("DB Diagnostics", expanded=False):
        st.write(f"DB Path: `{db_path}`")
        st.dataframe(diag, use_container_width=True)

    # ---- Load PIDs ----
    pids_df = run_query(db_path, """
        SELECT DISTINCT entity_id as PID
        FROM events
        WHERE kind='pinfo' AND entity_id IS NOT NULL
        ORDER BY CAST(entity_id AS INTEGER)
    """)

    if pids_df.empty:
        st.warning("No PINFO PIDs found in DB (kind='pinfo'). Check diagnostics above.")
        st.stop()

    selected_pid = st.selectbox("Select PID", pids_df["PID"])

    # ---- Load rows for selected PID ----
    rows = run_query(db_path, """
        SELECT timestamp, payload_json
        FROM events
        WHERE kind='pinfo' AND entity_id = ?
        ORDER BY id
    """, (selected_pid,))

    if rows.empty:
        st.warning("No records found for this PID.")
        st.stop()

    # ---- Expand JSON ----
    payloads = []
    for x in rows["payload_json"].tolist():
        try:
            payloads.append(json.loads(x))
        except Exception:
            payloads.append({})

    df = pd.json_normalize(payloads)
    df["timestamp"] = rows["timestamp"].values

    # ✅ parse timestamps ONCE for whole dashboard
    df = parse_timestamp(df)
    if df.empty:
        st.warning("All records for this PID have invalid timestamps.")
        st.stop()

    # ---- Metadata ----
    st.divider()
    st.subheader("Process Metadata")

    comp = (
        df["Component"].dropna().iloc[0]
        if "Component" in df.columns and not df["Component"].dropna().empty
        else "N/A"
    )

    appl = (
        df["APPLversion"].dropna().iloc[0]
        if "APPLversion" in df.columns and not df["APPLversion"].dropna().empty
        else "N/A"
    )

    c1, c2 = st.columns(2)
    c1.metric("Component", comp)
    c2.metric("APPL Version", appl)

    # ---- Summary ----
    st.divider()

    def _col_num(col):
        if col not in df.columns:
            return pd.Series([], dtype=float)
        return pd.to_numeric(df[col], errors="coerce")

    cpu_values = df["CPUAvg"].tolist() if "CPUAvg" in df.columns else []
    mode_cpu = calculate_mode(cpu_values)

    peak_cpu = _col_num("CPUAvg").max()
    avg_mem = _col_num("MemSize").mean()
    peak_mem = _col_num("MemSize").max()
    peak_virtual = _col_num("VirtMemSize").max()
    peak_resident = _col_num("ResidentMemSize").max()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mode CPU", round(mode_cpu or 0, 2))
    c2.metric("Peak CPU", round(peak_cpu or 0, 2))
    c3.metric("Avg Memory", round(avg_mem or 0, 2))
    c4.metric("Peak Memory", round(peak_mem or 0, 2))

    # ---- Snapshot Filter ----
    st.divider()
    st.subheader("Filter Snapshot by Timestamp")

    min_time = df["timestamp"].min().to_pydatetime()
    max_time = df["timestamp"].max().to_pydatetime()

    if min_time == max_time:
        st.info("Only one timestamp available; time-range filter disabled.")
        filtered = df.copy()
    else:
        time_range = st.slider(
            "Select Time Range",
            min_value=min_time,
            max_value=max_time,
            value=(min_time, max_time),
            format="HH:mm:ss"
        )
        filtered = df[
            (df["timestamp"] >= pd.Timestamp(time_range[0])) &
            (df["timestamp"] <= pd.Timestamp(time_range[1]))
        ]

    st.write(f"Total Records: {len(filtered)}")
    st.dataframe(filtered, use_container_width=True)
    
    # ---- CPU Chart ----
    st.divider()
    if "CPUAvg" in df.columns:
        cpu_df = df[["timestamp", "CPUAvg"]].copy()
        cpu_df["CPUAvg"] = pd.to_numeric(cpu_df["CPUAvg"], errors="coerce").fillna(0)

        st.subheader("CPU Usage Over Time")
        chart = alt.Chart(cpu_df).mark_line().encode(
            x=alt.X("timestamp:T", title="Time"),
            y=alt.Y("CPUAvg:Q", title="CPUAvg"),
            tooltip=["timestamp:T", "CPUAvg"]
        ).interactive()
        st.altair_chart(chart, use_container_width=True)

    # ---- Memory + Network I/O ----
    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        if "MemSize" in df.columns:
            mem_df = df[["timestamp", "MemSize"]].copy()
            mem_df["MemSize"] = pd.to_numeric(mem_df["MemSize"], errors="coerce").fillna(0)

            st.subheader("Memory Usage")

            st.altair_chart(
                alt.Chart(mem_df).mark_line().encode(
                    x=alt.X("timestamp:T", title="Time"),
                    y=alt.Y("MemSize:Q", title="MemSize"),
                    tooltip=["timestamp:T", "MemSize"]
                ).interactive(),
                use_container_width=True
            )
        else:
            st.info("MemSize field not found in this PID payloads.")

    with col2:
        if "BytesIn" in df.columns and "BytesOut" in df.columns:
            net_df = df[["timestamp", "BytesIn", "BytesOut"]].copy()
            net_df["BytesIn"] = pd.to_numeric(net_df["BytesIn"], errors="coerce").fillna(0)
            net_df["BytesOut"] = pd.to_numeric(net_df["BytesOut"], errors="coerce").fillna(0)

            melted = net_df.melt(
                id_vars=["timestamp"],
                value_vars=["BytesIn", "BytesOut"],
                var_name="Metric",
                value_name="Bytes"
            )

            st.subheader("Network I/O")

            st.altair_chart(
                alt.Chart(melted).mark_line().encode(
                    x=alt.X("timestamp:T", title="Time"),
                    y=alt.Y("Bytes:Q", title="Bytes"),
                    color="Metric:N",
                    tooltip=["timestamp:T", "Metric", "Bytes"]
                ).interactive(),
                use_container_width=True
            )
        else:
            st.info("BytesIn/BytesOut fields not found in this PID payloads.")


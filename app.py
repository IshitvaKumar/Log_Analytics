import streamlit as st
import os
import sqlite3

from CORE.ingest import ingest_log_file
from PINFO.dashboard import render as render_pinfo
from CORE.cm_dashboard import render as render_cm_generic  # ✅ generic CM dashboard

# ✅ absolute path so every module uses same db file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "common.db")

if "processed" not in st.session_state:
    st.session_state["processed"] = False

st.sidebar.title("Navigation")

if st.session_state["processed"]:
    page = st.sidebar.radio(
        "Select Page",
        [
            "Upload & Process",
            "PINFO Dashboard",
            "CM Dashboard",   # ✅ consistent label
        ],
    )
else:
    page = "Upload & Process"
    st.sidebar.info("Upload log file(s) and click Process to enable dashboards.")


def reset_common_db(db_path):
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS events")
        conn.commit()
        conn.close()


# -------------------------
# Upload + Processing Page
# -------------------------
if page == "Upload & Process":
    st.title("Unified Log Processor")

    uploaded_files = st.file_uploader(
        "Upload Log File(s)",
        type=["log", "txt"],
        accept_multiple_files=True
    )

    if uploaded_files:
        total_size = sum(file.size for file in uploaded_files)
        st.write(f"Files selected: {len(uploaded_files)}")
        st.write(f"Total size: {round(total_size / (1024*1024*1024), 5)} GB")

        if total_size > 5 * 1024 * 1024 * 1024:
            st.error("Total upload size cannot exceed 5GB.")
            st.stop()

        if st.button("Process Logs"):
            os.makedirs(os.path.join(BASE_DIR, "temp"), exist_ok=True)
            os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

            log_paths = []
            for file in uploaded_files:
                log_path = os.path.join(BASE_DIR, "temp", file.name)
                with open(log_path, "wb") as f:
                    f.write(file.read())
                log_paths.append(log_path)

            st.info("Processing log file(s)...")

            try:
                reset_common_db(DB_PATH)

                total_lines = 0
                total_parsed = 0
                total_inserted = 0
                total_skipped = 0

                for path in log_paths:
                    res = ingest_log_file(path, DB_PATH)
                    total_lines += res["lines_scanned"]
                    total_parsed += res["events_parsed"]
                    total_inserted += res["events_inserted"]
                    total_skipped += res["skipped_duplicates"]

                st.success("Processing Completed Successfully")
                st.session_state["processed"] = True

                st.write("### Unified DB Stats")
                st.write(f"Lines scanned: {total_lines}")
                st.write(f"Events parsed: {total_parsed}")
                st.write(f"Events inserted: {total_inserted}")
                st.write(f"Duplicates skipped: {total_skipped}")

                st.rerun()

            except Exception as e:
                st.error("Error during processing.")
                st.write(str(e))


# -------------------------
# Dashboard Routing
# -------------------------
elif page == "PINFO Dashboard":
    render_pinfo(DB_PATH)

elif page == "CM Dashboard":
    render_cm_generic(DB_PATH)
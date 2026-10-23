"""PLACEHOLDER dashboard page (TASK-009); TASK-010 replaces this file's contents.

It exists so the in-process server has a real script to serve (smoke test, ADR-002). It
reads the runtime the launcher registered and shows only the current status as plain text.
"""

import streamlit as st

from pc_handoff.runtime.registry import getRuntime

NOT_STARTED_MESSAGE = "PC Handoff is not running here. Start it with: python -m pc_handoff"

st.set_page_config(page_title="PC Handoff")
st.title("PC Handoff")
runtime = getRuntime()
snapshot = None if runtime is None else runtime.stateStore.current()
if snapshot is None:
    st.error(NOT_STARTED_MESSAGE)
else:
    st.text(f"Status: {snapshot.status.value}")
    st.caption("Placeholder page; the full dashboard arrives with TASK-010.")

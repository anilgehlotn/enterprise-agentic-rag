"""Streamlit page: query analytics from GET /analytics (needs the ANALYTICS_API_KEY)."""

import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")))

st.set_page_config(page_title="Query analytics", page_icon="📊", layout="wide")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")


def pct(value) -> str:
    return "–" if value is None else f"{value:.0%}"


def fetch(api_key: str, days: int) -> dict | None:
    """GET /analytics. Shows a readable message for each failure and returns None."""
    try:
        response = requests.get(
            f"{BACKEND_URL}/analytics", params={"days": days}, headers={"X-API-Key": api_key}, timeout=20
        )
    except requests.RequestException as exc:
        st.error(f"Backend unreachable at {BACKEND_URL}: {exc}")
        return None
    messages = {
        401: "Wrong analytics key.",
        404: "Analytics is disabled on the server (ANALYTICS_API_KEY is not set).",
        429: "⏳ Too many requests. Wait a moment.",
        503: "The analytics database is unavailable right now.",
    }
    if response.status_code != 200:
        st.error(messages.get(response.status_code, f"Request failed (HTTP {response.status_code})."))
        return None
    return response.json()


st.title("📊 Query analytics")
st.caption("Every number below comes from a hand-written SQL query (app/analytics/queries.py).")

with st.sidebar:
    api_key = st.text_input("Analytics key", type="password", help="The server's ANALYTICS_API_KEY")
    days = st.slider("Days", min_value=1, max_value=90, value=7)

if not api_key:
    st.info("Enter the analytics key in the sidebar.")
    st.stop()

data = fetch(api_key, days)
if data is None:
    st.stop()

grounded, retrieval, feedback = data["grounded"], data["retrieval"], data["feedback"]
cols = st.columns(5)
cols[0].metric("Queries", data["total_queries"])
cols[1].metric("Grounded (technical)", pct(grounded["grounded_rate"]), help="Verified answers judged supported")
cols[2].metric("Needed a rewrite", pct(retrieval["rewrite_rate"]), help="Technical questions with > 1 retrieval")
cols[3].metric("Satisfaction", pct(feedback["satisfaction_rate"]), help="👍 / (👍 + 👎)")
cols[4].metric("Answers rated", pct(feedback["feedback_rate"]))

st.subheader("Queries per day")
if data["queries_per_day"]:
    st.bar_chart(pd.DataFrame(data["queries_per_day"]).set_index("day")["queries"])
else:
    st.caption("No queries in this window.")

left, right = st.columns(2)
with left:
    st.subheader("Intents")
    st.dataframe(pd.DataFrame(data["intents"]), hide_index=True, width="stretch")
    st.subheader("Feedback")
    st.dataframe(
        pd.DataFrame([{"👍": feedback["thumbs_up"], "👎": feedback["thumbs_down"], **feedback}]).drop(
            columns=["thumbs_up", "thumbs_down"]
        ),
        hide_index=True,
        width="stretch",
    )
with right:
    st.subheader("Latency by entry point (ms)")
    st.dataframe(pd.DataFrame(data["latency"]), hide_index=True, width="stretch")
    st.subheader("Retrieval")
    st.dataframe(pd.DataFrame([retrieval]), hide_index=True, width="stretch")

st.subheader("Most frequent questions (asked at least twice)")
if not data["questions_stored"]:
    st.caption("Question text is not stored (ANALYTICS_STORE_QUESTIONS=false).")
elif data["top_questions"]:
    st.dataframe(pd.DataFrame(data["top_questions"]), hide_index=True, width="stretch")
else:
    st.caption("No question was asked twice in this window.")

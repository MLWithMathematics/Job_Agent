from __future__ import annotations

import os
import sys

# Ensure project root on path when running via streamlit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

<<<<<<< HEAD
=======
import json
>>>>>>> a135004 (Updated..)
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any

import pandas as pd
import plotly.express as px
import streamlit as st

from config import settings
from memory.ledger import update_status_by_id, get_all_applications


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Job Agent Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    .metric-card {
        background: #1e1e2e;
        border-radius: 12px;
        padding: 1rem 1.5rem;
        border: 1px solid #313244;
    }
    .status-applied   { color: #a6e3a1; font-weight: 600; }
    .status-skipped   { color: #f38ba8; }
    .status-failed    { color: #fab387; }
    .status-interview { color: #89dceb; font-weight: 700; }
    .status-pending   { color: #cdd6f4; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_data() -> pd.DataFrame:
    rows = get_all_applications()
    if not rows:
        return pd.DataFrame(
            columns=[
                "id", "job_title", "company", "platform", "apply_url",
                "match_score", "status", "resume_path", "applied_at",
                "outreach_sent", "notes",
            ]
        )
    df = pd.DataFrame(rows)
    df["applied_at"] = pd.to_datetime(df["applied_at"])
    df["outreach_sent"] = df["outreach_sent"].astype(bool)
    return df


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image(
        "https://img.icons8.com/color/96/robot-2.png",
        width=64,
    )
    st.title("Job Agent")
    st.caption("Multi-Agent Application Tracker")
    st.divider()

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
<<<<<<< HEAD
=======
    
    st.subheader("🛠️ Skill Manager")
    skills_path = os.path.join("memory", "skills.json")
    if os.path.exists(skills_path):
        with open(skills_path, "r", encoding="utf-8") as f:
            extra_skills = json.load(f).get("skills", "")
    else:
        extra_skills = ""
        
    new_skills = st.text_area("Add new skills to your resume profile:", value=extra_skills, help="Comma separated list of extra skills to append to your resume context.")
    if st.button("Save Skills"):
        os.makedirs("memory", exist_ok=True)
        with open(skills_path, "w", encoding="utf-8") as f:
            json.dump({"skills": new_skills}, f)
        st.success("Skills saved!")

    st.divider()
>>>>>>> a135004 (Updated..)
    st.caption(f"DB: `{settings.db_path}`")
    st.caption(f"Threshold: **{settings.match_threshold}** / 100")


# ── Load data ─────────────────────────────────────────────────────────────────

df = load_data()


# ── Header ────────────────────────────────────────────────────────────────────

st.title("🤖 Job Application Dashboard")
st.caption(f"Last loaded: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}")
st.divider()


# ── Summary Metrics ───────────────────────────────────────────────────────────

col1, col2, col3, col4, col5 = st.columns(5)

if df.empty:
    for col in [col1, col2, col3, col4, col5]:
        with col:
            st.metric("—", "0")
else:
    total = len(df)
    applied = len(df[df["status"] == "applied"])
    interviews = len(df[df["status"] == "interview"])
    avg_score = int(df["match_score"].mean()) if not df["match_score"].isna().all() else 0
    outreach_count = int(df["outreach_sent"].sum())

    li_count = len(df[df["platform"] == "linkedin"])
    nk_count = len(df[df["platform"] == "naukri"])

    with col1:
        st.metric("Total Processed", total)
    with col2:
        st.metric("Applied", applied, delta=f"{interviews} interviews")
    with col3:
        st.metric("Avg Match Score", f"{avg_score}/100")
    with col4:
        st.metric("LinkedIn / Naukri", f"{li_count} / {nk_count}")
    with col5:
        st.metric("Outreach Sent", outreach_count)

st.divider()


# ── Charts row ────────────────────────────────────────────────────────────────

if not df.empty:
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.subheader("📅 Applications (Last 14 Days)")
        last_14 = datetime.now() - timedelta(days=14)
        recent = df[df["applied_at"] >= last_14].copy()
        if recent.empty:
            st.info("No applications in the last 14 days.")
        else:
            recent["date"] = recent["applied_at"].dt.date
            daily = recent.groupby("date").size().reset_index(name="count")
            fig = px.bar(
                daily,
                x="date",
                y="count",
                labels={"date": "Date", "count": "Applications"},
                color_discrete_sequence=["#89b4fa"],
            )
            fig.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#cdd6f4",
                xaxis=dict(showgrid=False),
                yaxis=dict(showgrid=True, gridcolor="#313244"),
            )
            st.plotly_chart(fig, use_container_width=True)

    with chart_col2:
        st.subheader("📊 Status Breakdown")
        status_counts = df["status"].value_counts().reset_index()
        status_counts.columns = ["status", "count"]
        color_map = {
            "applied": "#a6e3a1",
            "skipped": "#f38ba8",
            "failed": "#fab387",
            "interview": "#89dceb",
            "pending": "#cdd6f4",
        }
        fig2 = px.pie(
            status_counts,
            names="status",
            values="count",
            color="status",
            color_discrete_map=color_map,
            hole=0.45,
        )
        fig2.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#cdd6f4",
            legend=dict(orientation="h", y=-0.1),
        )
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()


# ── Filter & Table ────────────────────────────────────────────────────────────

st.subheader("📋 Application Log")

if df.empty:
    st.info("No applications yet. Run `python main.py` to start the agent.")
else:
    # Filters
    filter_col1, filter_col2, filter_col3 = st.columns(3)

    with filter_col1:
        status_filter = st.multiselect(
            "Status",
            options=df["status"].unique().tolist(),
            default=df["status"].unique().tolist(),
        )

    with filter_col2:
        platform_filter = st.multiselect(
            "Platform",
            options=df["platform"].unique().tolist(),
            default=df["platform"].unique().tolist(),
        )

    with filter_col3:
        search_query = st.text_input("Search company / role", "")

    # Apply filters
    filtered = df[
        (df["status"].isin(status_filter))
        & (df["platform"].isin(platform_filter))
    ]
    if search_query:
        mask = (
            filtered["company"].str.contains(search_query, case=False, na=False)
            | filtered["job_title"].str.contains(search_query, case=False, na=False)
        )
        filtered = filtered[mask]

    st.caption(f"Showing {len(filtered)} of {len(df)} records")

    # Render rows with action buttons
    for _, row in filtered.iterrows():
        with st.expander(
            f"**{row['company']}** — {row['job_title']}  |  "
            f"Score: {row['match_score']}  |  "
            f"[{row['status'].upper()}]  |  "
            f"{row['platform'].capitalize()}",
            expanded=False,
        ):
            detail_col1, detail_col2 = st.columns([2, 1])

            with detail_col1:
                st.markdown(f"**URL:** [{row['apply_url'][:60]}...]({row['apply_url']})")
                st.markdown(f"**Applied:** {row['applied_at'].strftime('%d %b %Y, %H:%M') if pd.notna(row['applied_at']) else '—'}")
                if row["resume_path"]:
                    st.markdown(f"**Resume:** `{row['resume_path']}`")
                if row["notes"]:
                    st.markdown(f"**Notes:** {row['notes']}")
                st.markdown(
                    f"**Outreach:** {'✅ Sent' if row['outreach_sent'] else '❌ Not sent'}"
                )

            with detail_col2:
                st.markdown("**Update Status:**")

                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    if st.button(
                        "🎯 Mark Interview",
                        key=f"interview_{row['id']}",
                        use_container_width=True,
                    ):
                        update_status_by_id(row["id"], "interview")
                        st.cache_data.clear()
                        st.rerun()

                with btn_col2:
                    if st.button(
                        "❌ Mark Rejected",
                        key=f"rejected_{row['id']}",
                        use_container_width=True,
                    ):
                        update_status_by_id(row["id"], "rejected")
                        st.cache_data.clear()
                        st.rerun()

                if row["status"] != "applied":
                    if st.button(
                        "✅ Mark Applied",
                        key=f"applied_{row['id']}",
                        use_container_width=True,
                    ):
                        update_status_by_id(row["id"], "applied")
                        st.cache_data.clear()
                        st.rerun()


# ── Score Distribution ────────────────────────────────────────────────────────

if not df.empty and "match_score" in df.columns:
    st.divider()
    st.subheader("🎯 Match Score Distribution")
    scored = df[df["match_score"] > 0]
    if not scored.empty:
        fig3 = px.histogram(
            scored,
            x="match_score",
            nbins=20,
            color_discrete_sequence=["#cba6f7"],
            labels={"match_score": "Match Score"},
        )
        fig3.add_vline(
            x=settings.match_threshold,
            line_dash="dash",
            line_color="#f38ba8",
            annotation_text=f"Threshold ({settings.match_threshold})",
            annotation_position="top right",
        )
        fig3.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#cdd6f4",
            yaxis=dict(showgrid=True, gridcolor="#313244"),
            xaxis=dict(showgrid=False),
        )
        st.plotly_chart(fig3, use_container_width=True)

from __future__ import annotations

import asyncio
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END

from agents.search_agent import run_search_agent, JobListing
from agents.scorer_agent import run_scorer_agent, ScorerOutput
from agents.tailoring_agent import run_tailoring_agent
from agents.apply_agent import run_apply_agent
from agents.outreach_agent import run_outreach_agent
from resume.resume_editor import extract_bullets_from_docx, extract_full_text_from_docx
from memory.ledger import upsert_application, is_already_applied
from config import settings


# ── Typed state ───────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    job_listings: List[JobListing]
    current_job: Optional[JobListing]
    score_result: Optional[ScorerOutput]
    tailored_resume_path: str
    application_status: str       # 'applied' | 'skipped' | 'failed' | 'pending'
    application_id: int
    resume_text: str
    resume_bullets: List[str]


# ── Nodes ─────────────────────────────────────────────────────────────────────

async def score_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    kind = "Internship" if job.is_internship else "Job"
    print(f"\n[Pipeline] Scoring [{kind}]: {job.company} | {job.job_title}")

    result = await run_scorer_agent(job, state["resume_text"])

    app_id = upsert_application(
        job_title=job.job_title,
        company=job.company,
        platform=job.platform,
        apply_url=job.apply_url,
        match_score=result.score,
        status="skipped" if result.status == "skipped" else "pending",
        notes=result.reasoning,
    )

    return {
        **state,
        "score_result": result,
        "application_id": app_id,
        "application_status": result.status,  # 'approved' or 'skipped'
    }


async def tailor_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    print(f"[Pipeline] Tailoring resume for: {job.company}")

    resume_path = await run_tailoring_agent(
        job=job,
        scorer_output=state["score_result"],
        resume_bullets=state["resume_bullets"],
        resume_text=state["resume_text"],
    )

    upsert_application(
        job_title=job.job_title,
        company=job.company,
        platform=job.platform,
        apply_url=job.apply_url,
        match_score=state["score_result"].score,
        status="pending",
        resume_path=resume_path,
    )

    return {**state, "tailored_resume_path": resume_path}


async def apply_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    print(f"[Pipeline] Applying to: {job.company} | {job.job_title}")

    status = await run_apply_agent(
        job=job,
        tailored_resume_path=state["tailored_resume_path"],
        resume_text=state["resume_text"],
    )
    return {**state, "application_status": status}


async def outreach_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    if job.recruiter_name and state["application_status"] == "applied":
        print(f"[Pipeline] Outreach → {job.recruiter_name} @ {job.company}")
        await run_outreach_agent(
            job=job,
            application_id=state["application_id"],
            resume_text=state["resume_text"],
        )
    return state


async def log_skip_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    sr = state.get("score_result")
    kind = "Internship" if job.is_internship else "Job"
    threshold = (
        settings.internship_match_threshold
        if job.is_internship
        else settings.match_threshold
    )
    print(
        f"[Pipeline] SKIPPED [{kind}]: {job.company} | {job.job_title} "
        f"(score {sr.score if sr else 0} < threshold {threshold})"
    )
    upsert_application(
        job_title=job.job_title,
        company=job.company,
        platform=job.platform,
        apply_url=job.apply_url,
        match_score=sr.score if sr else 0,
        status="skipped",
        notes=sr.reasoning if sr else "",
    )
    return {**state, "application_status": "skipped"}


async def log_result_node(state: AgentState) -> AgentState:
    job = state["current_job"]
    print(
        f"[Pipeline] ✓ {job.company} | {job.job_title} → "
        f"{state['application_status'].upper()}"
    )
    return state


# ── Conditional edges ─────────────────────────────────────────────────────────

def should_apply(state: AgentState) -> str:
    return "tailor" if state["application_status"] == "approved" else "log_skip"


def should_do_outreach(state: AgentState) -> str:
    return "outreach" if state["application_status"] == "applied" else "log_result"


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(AgentState)

    g.add_node("score", score_node)
    g.add_node("tailor", tailor_node)
    g.add_node("apply", apply_node)
    g.add_node("outreach", outreach_node)
    g.add_node("log_skip", log_skip_node)
    g.add_node("log_result", log_result_node)

    g.set_entry_point("score")

    g.add_conditional_edges("score", should_apply, {
        "tailor": "tailor",
        "log_skip": "log_skip",
    })
    g.add_edge("tailor", "apply")
    g.add_conditional_edges("apply", should_do_outreach, {
        "outreach": "outreach",
        "log_result": "log_result",
    })
    g.add_edge("outreach", "log_result")
    g.add_edge("log_skip", END)
    g.add_edge("log_result", END)

    return g.compile()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 62)
    print("  Agentic Job Application System — Starting")
    print(f"  Full-time threshold : {settings.match_threshold}")
    print(f"  Internship threshold: {settings.internship_match_threshold}")
    print("=" * 62)

    resume_docx = settings.base_resume_docx
    if not os.path.exists(resume_docx):
        print(f"\n[ERROR] Resume not found: {resume_docx}")
        print("Place your resume at resume/base_resume.docx and re-run.\n")
        sys.exit(1)

    resume_text = extract_full_text_from_docx(resume_docx)
    resume_bullets = extract_bullets_from_docx(resume_docx)
    print(f"[Main] Resume: {len(resume_text)} chars, {len(resume_bullets)} bullets.")

    print("\n[Main] Running Search Agent (jobs + internships)...")
    job_listings = await run_search_agent()

    if not job_listings:
        print("[Main] No listings found. Exiting.")
        return

    internships = [j for j in job_listings if j.is_internship]
    jobs = [j for j in job_listings if not j.is_internship]
    print(f"[Main] {len(jobs)} full-time jobs + {len(internships)} internships to process.\n")

    app = build_graph()

    for i, job in enumerate(job_listings, 1):
        kind = "🎓 Internship" if job.is_internship else "💼 Job"
        print(f"\n{'─' * 60}")
        print(f"[{i}/{len(job_listings)}] {kind}: {job.company} | {job.job_title}")
        print(f"{'─' * 60}")

        if is_already_applied(job.apply_url):
            print("[Main] Already processed. Skipping.")
            continue

        initial_state: AgentState = {
            "job_listings": job_listings,
            "current_job": job,
            "score_result": None,
            "tailored_resume_path": resume_docx,
            "application_status": "pending",
            "application_id": -1,
            "resume_text": resume_text,
            "resume_bullets": resume_bullets,
        }

        try:
            final_state = await app.ainvoke(initial_state)
            print(f"[Main] → {final_state['application_status'].upper()}")
        except Exception as exc:
            print(f"[Main] Pipeline error: {exc}")
            upsert_application(
                job_title=job.job_title,
                company=job.company,
                platform=job.platform,
                apply_url=job.apply_url,
                status="failed",
                notes=str(exc),
            )

    print("\n" + "=" * 62)
    print("  All done. Run: streamlit run dashboard/app.py")
    print("=" * 62)


if __name__ == "__main__":
    asyncio.run(main())

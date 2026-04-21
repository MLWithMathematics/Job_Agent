from __future__ import annotations

import asyncio
import os
<<<<<<< HEAD
import sys
=======
import signal
import sys
import threading
>>>>>>> a135004 (Updated..)
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END

from agents.search_agent import run_search_agent, JobListing
from agents.scorer_agent import run_scorer_agent, ScorerOutput
from agents.tailoring_agent import run_tailoring_agent
from agents.apply_agent import run_apply_agent
from agents.outreach_agent import run_outreach_agent
<<<<<<< HEAD
from resume.resume_editor import extract_bullets_from_docx, extract_full_text_from_docx
=======
from resume.resume_editor import load_resume
>>>>>>> a135004 (Updated..)
from memory.ledger import upsert_application, is_already_applied
from config import settings


<<<<<<< HEAD
=======
# ── Interrupt controller ──────────────────────────────────────────────────────

class _InterruptController:
    """
    Thread-safe interrupt flag.
    Set by Ctrl+C (SIGINT) or the interactive 'q' command.
    Checked between every job so the agent stops cleanly after the
    current application finishes — never mid-apply.
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._skip = asyncio.Event()   # skip only the current job

    def request_stop(self) -> None:
        print("\n[Interrupt] Stop requested — will finish current job then exit.")
        self._stop.set()

    def request_skip(self) -> None:
        print("\n[Interrupt] Skip requested — moving to next job.")
        self._skip.set()

    @property
    def should_stop(self) -> bool:
        return self._stop.is_set()

    @property
    def should_skip(self) -> bool:
        return self._skip.is_set()

    def clear_skip(self) -> None:
        self._skip.clear()


_ctrl = _InterruptController()


def _setup_signal_handlers() -> None:
    """Ctrl+C → graceful stop (not immediate kill)."""
    def _handler(signum, frame):
        if _ctrl.should_stop:
            # Second Ctrl+C → force exit immediately
            print("\n[Interrupt] Forced exit.")
            sys.exit(1)
        _ctrl.request_stop()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def _start_keyboard_listener() -> None:
    """
    Background thread reading single-key commands from stdin:
      q / Q  → stop after current job
      s / S  → skip current job, continue with next
      p / P  → print current status
    """
    def _listener():
        print("[Control] Keyboard commands: [S]kip job  |  [Q]uit after current  |  [P]rint status")
        while not _ctrl.should_stop:
            try:
                key = input().strip().lower()
                if key == "q":
                    _ctrl.request_stop()
                elif key == "s":
                    _ctrl.request_skip()
                elif key == "p":
                    print("[Status] Agent running. Press Q to quit, S to skip current job.")
            except EOFError:
                break
            except Exception:
                break

    t = threading.Thread(target=_listener, daemon=True)
    t.start()


# ── Resume loader ─────────────────────────────────────────────────────────────

def _resolve_resume_path() -> str:
    """
    Find the base resume file in this priority order:
    1. RESUME_FORMAT=pdf  → base_resume_pdf
    2. RESUME_FORMAT=latex → base_resume_tex
    3. RESUME_FORMAT=docx  → base_resume_docx
    4. Auto-detect: whichever of .pdf / .docx / .tex exists first
    """
    fmt = settings.resume_format.lower()

    candidates = {
        "pdf":   settings.base_resume_pdf,
        "latex": settings.base_resume_tex,
        "docx":  settings.base_resume_docx,
    }

    if fmt in candidates and os.path.exists(candidates[fmt]):
        return candidates[fmt]

    # Auto-detect fallback
    for path in [
        settings.base_resume_pdf,
        settings.base_resume_docx,
        settings.base_resume_tex,
    ]:
        if os.path.exists(path):
            print(f"[Main] Auto-detected resume: {path}")
            return path

    return ""   # caller handles missing file


>>>>>>> a135004 (Updated..)
# ── Typed state ───────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    job_listings: List[JobListing]
    current_job: Optional[JobListing]
    score_result: Optional[ScorerOutput]
    tailored_resume_path: str
<<<<<<< HEAD
    application_status: str       # 'applied' | 'skipped' | 'failed' | 'pending'
=======
    application_status: str
>>>>>>> a135004 (Updated..)
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
<<<<<<< HEAD
        "application_status": result.status,  # 'approved' or 'skipped'
=======
        "application_status": result.status,
>>>>>>> a135004 (Updated..)
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


<<<<<<< HEAD
# ── Graph builder ─────────────────────────────────────────────────────────────
=======
# ── Graph ─────────────────────────────────────────────────────────────────────
>>>>>>> a135004 (Updated..)

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
<<<<<<< HEAD
=======
    _setup_signal_handlers()
    _start_keyboard_listener()

>>>>>>> a135004 (Updated..)
    print("=" * 62)
    print("  Agentic Job Application System — Starting")
    print(f"  Full-time threshold : {settings.match_threshold}")
    print(f"  Internship threshold: {settings.internship_match_threshold}")
<<<<<<< HEAD
    print("=" * 62)

    resume_docx = settings.base_resume_docx
    if not os.path.exists(resume_docx):
        print(f"\n[ERROR] Resume not found: {resume_docx}")
        print("Place your resume at resume/base_resume.docx and re-run.\n")
        sys.exit(1)

    resume_text = extract_full_text_from_docx(resume_docx)
    resume_bullets = extract_bullets_from_docx(resume_docx)
    print(f"[Main] Resume: {len(resume_text)} chars, {len(resume_bullets)} bullets.")

=======
    print("  Commands: [S]kip job  |  [Q]uit after current  |  Ctrl+C")
    print("=" * 62)

    # ── Load resume (auto-detects pdf / docx / tex) ───────────────────
    resume_path = _resolve_resume_path()
    if not resume_path:
        print("\n[ERROR] No resume found. Place your resume at one of:")
        print(f"  • {settings.base_resume_pdf}   (PDF — recommended)")
        print(f"  • {settings.base_resume_docx}  (Word)")
        print(f"  • {settings.base_resume_tex}   (LaTeX)")
        sys.exit(1)

    print(f"[Main] Loading resume: {resume_path}")
    resume_text, resume_bullets = load_resume(resume_path)

    if not resume_text.strip():
        print("[ERROR] Resume appears empty. Check the file and try again.")
        sys.exit(1)

    print(f"[Main] Resume: {len(resume_text)} chars, {len(resume_bullets)} bullets.")

    # ── Search ────────────────────────────────────────────────────────
>>>>>>> a135004 (Updated..)
    print("\n[Main] Running Search Agent (jobs + internships)...")
    job_listings = await run_search_agent()

    if not job_listings:
        print("[Main] No listings found. Exiting.")
        return

    internships = [j for j in job_listings if j.is_internship]
    jobs = [j for j in job_listings if not j.is_internship]
<<<<<<< HEAD
    print(f"[Main] {len(jobs)} full-time jobs + {len(internships)} internships to process.\n")

    app = build_graph()

    for i, job in enumerate(job_listings, 1):
        kind = "🎓 Internship" if job.is_internship else "💼 Job"
=======
    print(f"[Main] {len(jobs)} full-time + {len(internships)} internships to process.\n")

    app = build_graph()
    applied_count = 0
    skipped_count = 0
    failed_count = 0

    for i, job in enumerate(job_listings, 1):
        # ── Check for stop signal (between jobs, never mid-apply) ─────
        if _ctrl.should_stop:
            print(f"\n[Interrupt] Stopping after {i - 1} jobs processed.")
            break

        # Clear any stale skip flag from the previous iteration
        _ctrl.clear_skip()

        kind = "🎓 Intern" if job.is_internship else "💼 Job"
>>>>>>> a135004 (Updated..)
        print(f"\n{'─' * 60}")
        print(f"[{i}/{len(job_listings)}] {kind}: {job.company} | {job.job_title}")
        print(f"{'─' * 60}")

        if is_already_applied(job.apply_url):
            print("[Main] Already processed. Skipping.")
<<<<<<< HEAD
=======
            skipped_count += 1
            continue

        # ── Check per-job skip signal ─────────────────────────────────
        if _ctrl.should_skip:
            print("[Main] Skipped via keyboard command.")
            _ctrl.clear_skip()
            skipped_count += 1
>>>>>>> a135004 (Updated..)
            continue

        initial_state: AgentState = {
            "job_listings": job_listings,
            "current_job": job,
            "score_result": None,
<<<<<<< HEAD
            "tailored_resume_path": resume_docx,
=======
            "tailored_resume_path": resume_path,
>>>>>>> a135004 (Updated..)
            "application_status": "pending",
            "application_id": -1,
            "resume_text": resume_text,
            "resume_bullets": resume_bullets,
        }

        try:
<<<<<<< HEAD
            final_state = await app.ainvoke(initial_state)
            print(f"[Main] → {final_state['application_status'].upper()}")
=======
            # Run pipeline — keyboard commands are checked before/after, not during
            final_state = await app.ainvoke(initial_state)
            status = final_state["application_status"]
            print(f"[Main] → {status.upper()}")

            if status == "applied":
                applied_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                failed_count += 1

        except asyncio.CancelledError:
            print("[Main] Job cancelled by interrupt.")
            break
>>>>>>> a135004 (Updated..)
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
<<<<<<< HEAD

    print("\n" + "=" * 62)
    print("  All done. Run: streamlit run dashboard/app.py")
=======
            failed_count += 1

    print("\n" + "=" * 62)
    print(f"  Session complete:")
    print(f"  ✅ Applied : {applied_count}")
    print(f"  ⏭  Skipped : {skipped_count}")
    print(f"  ❌ Failed  : {failed_count}")
    print("  Run: streamlit run dashboard/app.py")
>>>>>>> a135004 (Updated..)
    print("=" * 62)


if __name__ == "__main__":
    asyncio.run(main())
<<<<<<< HEAD
=======

>>>>>>> a135004 (Updated..)

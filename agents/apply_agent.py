"""
apply_agent.py
──────────────
Applies to jobs using the appropriate platform flow.

Session caching — each platform uses a PERSISTENT Playwright browser context
whose user-data directory is stored on disk (browser/session_store/<platform>/).
This means:

  • Login happens only ONCE ever (on the very first run).
  • Subsequent runs reuse the saved cookies/session without any login step.
  • If LinkedIn or Naukri forcibly expires the session (auto-logout, security
    challenge, etc.) the agent detects the stale session, triggers a fresh
    login, and the new cookies are automatically persisted for next time.

Public API
----------
run_apply_agent(job, tailored_resume_path, resume_text) -> str
cleanup_apply_sessions()   # call once after all jobs in the run are done

Routing logic:
  - LinkedIn Easy Apply     → apply_linkedin_easy_apply()  (automated)
  - Naukri native apply     → apply_naukri()               (automated)
  - External / No Easy Apply→ recorded as 'manual_apply'   (shown in dashboard)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from playwright.async_api import async_playwright

from agents.search_agent import JobListing
from browser.stealth import get_launch_args, get_context_options, STEALTH_INIT_SCRIPT
from browser.session_manager import (
    get_persistent_context,
    is_linkedin_logged_in,
    is_naukri_logged_in,
    SHARED_SESSIONS,
    cleanup_shared_sessions,
)
from browser.linkedin_flow import (
    linkedin_login,
    apply_linkedin_easy_apply,
    solve_captcha_if_present,
)
from browser.naukri_flow import naukri_login, apply_naukri, profile_refresh
from llm_client import dynamic_qa
from memory.ledger import update_status
from config import settings


# Both use *persistent* contexts (cookies saved to disk).


async def _get_or_create_session(platform: str):
    """
    Return (context, page) for *platform* (linkedin or naukri only).

    Uses a persistent context (saved to disk):
      - If a cached in-memory session is alive and healthy → reuse it.
      - Otherwise: launch the persistent context, check if already logged in
        (cookies on disk are valid), and only call the login function if the
        session has expired.
    """
    key = platform  # "linkedin" or "naukri"

    # ── Try to reuse an in-memory session ─────────────────────────────
    if key in SHARED_SESSIONS:
        sess = SHARED_SESSIONS[key]
        try:
            await sess["page"].evaluate("1 + 1")
            return sess["context"], sess["page"]
        except Exception:
            # Page/context died — close gracefully and recreate below
            for target, method in [("context", "close"), ("pw", "stop")]:
                try:
                    await getattr(sess[target], method)()
                except Exception:
                    pass
            del SHARED_SESSIONS[key]

    # ── Spin up / reconnect ───────────────────────────────────────────
    if key == "linkedin":
        print("[Apply] Opening persistent LinkedIn session...")
        pw, context = await get_persistent_context("linkedin")

        already_in = await is_linkedin_logged_in(context)
        if already_in:
            print("[Apply] LinkedIn session restored from disk — skipping login. [OK]")
            page = await context.new_page()
        else:
            print("[Apply] LinkedIn session expired or first run — logging in...")
            page = await linkedin_login(context)
            await solve_captcha_if_present(page)
            print("[Apply] LinkedIn login complete — session saved to disk. [OK]")

    elif key == "naukri":
        print("[Apply] Opening persistent Naukri session...")
        pw, context = await get_persistent_context("naukri")

        already_in = await is_naukri_logged_in(context)
        if already_in:
            print("[Apply] Naukri session restored from disk — skipping login. [OK]")
            page = await context.new_page()
        else:
            print("[Apply] Naukri session expired or first run — logging in...")
            page = await naukri_login(context)
            print("[Apply] Naukri login complete — session saved to disk. [OK]")

    else:
        raise ValueError(f"Unknown platform for session: {key}")

    SHARED_SESSIONS[key] = {
        "pw":      pw,
        "context": context,
        "page":    page,
    }
    return context, page

# Re-export cleanup for external callers if they still use it
cleanup_apply_sessions = cleanup_shared_sessions


# ── Public entry point ────────────────────────────────────────────────────────

async def run_apply_agent(
    job: JobListing,
    tailored_resume_path: str,
    resume_text: str,
) -> str:
    """
    Apply to a single job using the appropriate platform flow.
    Reuses the cached/persistent authenticated session — login only fires when
    the stored session is absent or expired.

    Routing:
      - LinkedIn Easy Apply  → automated via apply_linkedin_easy_apply()
      - Naukri native apply  → automated via apply_naukri()
      - External / No Easy Apply → recorded as 'manual_apply' (user applies
        manually; the dashboard highlights these with a direct link).

    Returns: 'applied' | 'failed' | 'skipped' | 'manual_apply'
    """
    # ── External / non-Easy-Apply jobs → manual apply card ─────────────
    if job.apply_type == "external":
        print(
            f"[Apply] ⚠ External apply (no Easy Apply): {job.company} | {job.job_title}\n"
            f"        URL: {job.apply_url}\n"
            f"        → Marked as MANUAL APPLY in dashboard."
        )
        update_status(
            job.apply_url,
            "manual_apply",
            notes=f"No Easy Apply — apply manually at: {job.apply_url}",
        )
        return "manual_apply"

    async def llm_answer_fn(question: str, res_text: str, job_context: str = "") -> str:
        return await dynamic_qa(question, res_text, job_context)

    # Build job context string for role-aware answers
    job_context = f"Role: {job.job_title} at {job.company}"
    if hasattr(job, "jd_text") and job.jd_text:
        job_context += f"\n{job.jd_text[:1200]}"

    try:
        context, page = await _get_or_create_session(job.platform)

        # ── LinkedIn Easy Apply ───────────────────────────────────────
        if job.platform == "linkedin":
            success = await apply_linkedin_easy_apply(
                page=page,
                apply_url=job.apply_url,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
                job_title=job.job_title,
                jd_text=getattr(job, "jd_text", ""),
            )
            if success:
                update_status(job.apply_url, "applied")
                print(f"[Apply] {job.company} | {job.job_title} -> applied")
                return "applied"
            else:
                # Easy Apply failed = no Easy Apply button / modal didn't open
                # -> mark as manual_apply so user can apply via dashboard
                update_status(
                    job.apply_url,
                    "manual_apply",
                    notes=f"Easy Apply not available or failed — apply manually at: {job.apply_url}",
                )
                print(f"[Apply] {job.company} | {job.job_title} -> manual_apply")
                return "manual_apply"

        # ── Naukri native apply ───────────────────────────────────────
        elif job.platform == "naukri":
            success = await apply_naukri(
                page=page,
                apply_url=job.apply_url,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
            )
            if success:
                await profile_refresh(context)
                update_status(job.apply_url, "applied")
                print(f"[Apply] {job.company} | {job.job_title} -> applied")
                return "applied"
            else:
                update_status(job.apply_url, "failed")
                print(f"[Apply] {job.company} | {job.job_title} -> failed")
                return "failed"

        else:
            print(
                f"[Apply] Unknown platform='{job.platform}' "
                f"apply_type='{job.apply_type}' — skipping."
            )
            return "skipped"

    except Exception as exc:
        print(f"[Apply] Exception for {job.company}: {exc}")
        update_status(job.apply_url, "failed", notes=str(exc))
        return "failed"

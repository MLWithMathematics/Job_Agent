"""
apply_agent.py
──────────────
Applies to jobs using the appropriate platform flow.

Session caching — each platform logs in ONCE per process run, then
reuses the same Playwright browser context for every subsequent job on
that platform.  This eliminates the "new window without login" bug where
a fresh browser was launched (without authentication) for every single job.

Public API
----------
run_apply_agent(job, tailored_resume_path, resume_text) -> str
cleanup_apply_sessions()   # call once after all jobs in the run are done

Routing logic:
  - LinkedIn Easy Apply     → apply_linkedin_easy_apply()  (built-in external fallback)
  - LinkedIn external URL   → apply_linkedin_easy_apply()  (handles popup tab capture)
  - Naukri native apply     → apply_naukri()               (handles external redirect too)
  - True external ATS URL   → apply_external_link()        (opens a fresh tab)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from playwright.async_api import async_playwright

from agents.search_agent import JobListing
from browser.stealth import get_launch_args, get_context_options, STEALTH_INIT_SCRIPT
from browser.linkedin_flow import (
    linkedin_login,
    apply_linkedin_easy_apply,
    solve_captcha_if_present,
)
from browser.naukri_flow import naukri_login, apply_naukri, profile_refresh
from browser.external_flow import apply_external_link
from llm_client import dynamic_qa
from memory.ledger import update_status
from config import settings


# ── Module-level session cache ────────────────────────────────────────────────
# Key: "linkedin" | "naukri" | "external"
# Value: { "pw": Playwright, "browser": Browser, "context": Context, "page": Page }
_SESSIONS: Dict[str, Any] = {}

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
"""


async def _get_or_create_session(platform: str, apply_type: str):
    """
    Return (context, page) — reusing an existing logged-in session when
    possible, or creating + authenticating a fresh one when the cache is
    empty or the existing session has gone stale.

    External ATS jobs share one context (no platform login needed);
    LinkedIn and Naukri each get their own authenticated context.
    """
    key = "external" if apply_type == "external" else platform

    # ── Try to reuse an existing session ─────────────────────────────
    if key in _SESSIONS:
        sess = _SESSIONS[key]
        try:
            await sess["page"].evaluate("1 + 1")
            return sess["context"], sess["page"]
        except Exception:
            for target, method in [("browser", "close"), ("pw", "stop")]:
                try:
                    await getattr(sess[target], method)()
                except Exception:
                    pass
            del _SESSIONS[key]

    # ── Spin up a new browser + authenticate ─────────────────────────
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(**get_launch_args())
    context = await browser.new_context(**get_context_options())
    await context.add_init_script(_STEALTH_SCRIPT)

    if key == "linkedin":
        print("[Apply] Starting LinkedIn session (logging in once for this run)...")
        page = await linkedin_login(context)
        await solve_captcha_if_present(page)

    elif key == "naukri":
        print("[Apply] Starting Naukri session (logging in once for this run)...")
        page = await naukri_login(context)

    else:
        print("[Apply] Starting external session (no platform login needed)...")
        page = await context.new_page()

    _SESSIONS[key] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
    }
    return context, page


async def cleanup_apply_sessions() -> None:
    """
    Close every cached browser session gracefully.
    Call this once after all jobs in the run have been processed.
    """
    for key in list(_SESSIONS.keys()):
        sess = _SESSIONS.pop(key)
        for target, method in [("browser", "close"), ("pw", "stop")]:
            try:
                await getattr(sess[target], method)()
            except Exception:
                pass
    print("[Apply] All browser sessions closed.")


# ── Public entry point ────────────────────────────────────────────────────────

async def run_apply_agent(
    job: JobListing,
    tailored_resume_path: str,
    resume_text: str,
) -> str:
    """
    Apply to a single job using the appropriate platform flow.
    Reuses the cached authenticated session so login only happens once.

    Returns: 'applied' | 'failed' | 'skipped'
    """
    async def llm_answer_fn(question: str, res_text: str) -> str:
        return await dynamic_qa(question, res_text)

    try:
        context, page = await _get_or_create_session(job.platform, job.apply_type)

        # ── LinkedIn: Easy Apply AND LinkedIn-URL external jobs ───────
        # Both cases go through apply_linkedin_easy_apply().
        # That function contains a built-in fallback: when no Easy Apply
        # button is present it looks for the regular Apply button,
        # captures the new-tab popup it opens, and runs the external ATS
        # flow on that tab.
        if job.platform == "linkedin" and (
            job.apply_type == "easy_apply"
            or (job.apply_type == "external" and "linkedin.com" in job.apply_url)
        ):
            success = await apply_linkedin_easy_apply(
                page=page,
                apply_url=job.apply_url,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
            )

        # ── Naukri native apply (also handles external ATS redirects) ─
        elif job.apply_type == "naukri" or (
            job.platform == "naukri" and job.apply_type != "external"
        ):
            success = await apply_naukri(
                page=page,
                apply_url=job.apply_url,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
            )
            if success:
                await profile_refresh(context)

        # ── True external ATS (non-LinkedIn, non-Naukri URL) ──────────
        # apply_url is already the ATS URL (Workday, Greenhouse, Lever …)
        elif job.apply_type == "external":
            ext_page = await context.new_page()
            try:
                success = await apply_external_link(
                    page=ext_page,
                    apply_url=job.apply_url,
                    tailored_resume_path=tailored_resume_path,
                    resume_text=resume_text,
                    llm_answer_fn=llm_answer_fn,
                )
            finally:
                await ext_page.close()

        else:
            print(
                f"[Apply] Unknown apply_type='{job.apply_type}' "
                f"platform='{job.platform}' — skipping."
            )
            return "skipped"

        status = "applied" if success else "failed"
        update_status(job.apply_url, status)
        print(f"[Apply] {job.company} | {job.job_title} → {status}")
        return status

    except Exception as exc:
        print(f"[Apply] Exception for {job.company}: {exc}")
        update_status(job.apply_url, "failed", notes=str(exc))
        return "failed"

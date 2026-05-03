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
from browser.session_manager import (
    get_persistent_context,
    is_linkedin_logged_in,
    is_naukri_logged_in,
)
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
# Value: { "pw": Playwright, "context": Context, "page": Page }
# For "linkedin" and "naukri" these are *persistent* contexts (cookies saved
# to disk).  "external" still uses a regular ephemeral context.
_SESSIONS: Dict[str, Any] = {}

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
"""


async def _get_or_create_session(platform: str, apply_type: str):
    """
    Return (context, page) for *platform*.

    For LinkedIn and Naukri: uses a persistent context (saved to disk).
      - If a cached in-memory session is alive and healthy → reuse it.
      - Otherwise: launch the persistent context, check if already logged in
        (cookies on disk are valid), and only call the login function if the
        session has expired.

    For external ATS: ephemeral context, no authentication needed.
    """
    key = platform if platform in {"linkedin", "naukri"} else "external"

    # ── Try to reuse an in-memory session ─────────────────────────────
    if key in _SESSIONS:
        sess = _SESSIONS[key]
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
            del _SESSIONS[key]

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
        # External ATS — no platform login required
        print("[Apply] Starting external session (no platform login needed)...")
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(**get_launch_args())
        context = await browser.new_context(**get_context_options())
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()
        _SESSIONS[key] = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
        }
        return context, page

    _SESSIONS[key] = {
        "pw":      pw,
        "context": context,
        "page":    page,
    }
    return context, page


async def cleanup_apply_sessions() -> None:
    """
    Close every cached browser session gracefully.

    For persistent sessions (LinkedIn / Naukri) closing the context flushes all
    cookies and storage to disk automatically — they will be reloaded on the
    next run.

    Call this once after all jobs in the run have been processed.
    """
    for key in list(_SESSIONS.keys()):
        sess = _SESSIONS.pop(key)
        # Persistent contexts have no "browser" key — just "context" + "pw"
        for target, method in [
            ("browser", "close"),   # external ephemeral only
            ("context", "close"),   # persistent contexts
            ("pw",      "stop"),
        ]:
            if target in sess:
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
    Reuses the cached/persistent authenticated session — login only fires when
    the stored session is absent or expired.

    Returns: 'applied' | 'failed' | 'skipped'
    """
    async def llm_answer_fn(question: str, res_text: str) -> str:
        return await dynamic_qa(question, res_text)

    try:
        context, page = await _get_or_create_session(job.platform, job.apply_type)

        # LinkedIn-origin jobs stay in the logged-in LinkedIn context.
        if job.platform == "linkedin":
            if job.apply_type == "external" and "linkedin.com" not in job.apply_url:
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
        print(f"[Apply] {job.company} | {job.job_title} -> {status}")
        return status

    except Exception as exc:
        print(f"[Apply] Exception for {job.company}: {exc}")
        update_status(job.apply_url, "failed", notes=str(exc))
        return "failed"

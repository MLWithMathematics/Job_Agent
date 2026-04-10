from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import async_playwright

from agents.search_agent import JobListing
from browser.stealth import get_launch_args, get_context_options
from browser.linkedin_flow import linkedin_login, apply_linkedin_easy_apply, solve_captcha_if_present
from browser.naukri_flow import naukri_login, apply_naukri, profile_refresh
from llm_client import dynamic_qa
from memory.ledger import upsert_application, update_status
from config import settings


async def run_apply_agent(
    job: JobListing,
    tailored_resume_path: str,
    resume_text: str,
) -> str:
    """
    Apply to a job using the appropriate platform flow.
    Returns status: 'applied', 'failed', or 'skipped'.
    """
    async def llm_answer_fn(question: str, res_text: str) -> str:
        return await dynamic_qa(question, res_text)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**get_launch_args())
        context = await browser.new_context(**get_context_options())

        # Stealth JS injection
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            """
        )

        try:
            if job.platform == "linkedin":
                page = await linkedin_login(context)
                await solve_captcha_if_present(page)
                success = await apply_linkedin_easy_apply(
                    page=page,
                    apply_url=job.apply_url,
                    tailored_resume_path=tailored_resume_path,
                    resume_text=resume_text,
                    llm_answer_fn=llm_answer_fn,
                )

            elif job.platform == "naukri":
                page = await naukri_login(context)
                success = await apply_naukri(
                    page=page,
                    apply_url=job.apply_url,
                    tailored_resume_path=tailored_resume_path,
                    resume_text=resume_text,
                    llm_answer_fn=llm_answer_fn,
                )
                if success:
                    # Trigger profile refresh immediately after Naukri apply
                    await profile_refresh(context)

            else:
                print(f"[Apply] Unknown platform: {job.platform}")
                return "failed"

            status = "applied" if success else "failed"
            update_status(job.apply_url, status)
            print(f"[Apply] {job.company} | {job.job_title} → {status}")
            return status

        except Exception as exc:
            print(f"[Apply] Exception for {job.company}: {exc}")
            update_status(job.apply_url, "failed", notes=str(exc))
            return "failed"

        finally:
            await context.close()
            await browser.close()

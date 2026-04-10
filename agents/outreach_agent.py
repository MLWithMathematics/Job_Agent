from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright, Page

from agents.search_agent import JobListing
from browser.stealth import (
    get_launch_args,
    get_context_options,
    random_delay,
    human_type,
    human_click,
    human_scroll,
)
from browser.linkedin_flow import linkedin_login
from llm_client import call_llm
from memory.ledger import log_outreach, mark_outreach_sent
from config import settings


OUTREACH_PROMPT = """\
Write a LinkedIn connection request message (under 300 characters).

Recruiter: {recruiter_name} at {company}
Role applied for: {job_title}
My background: {resume_summary}

Make it genuine, not salesy. Mention ONE specific skill match.
Do NOT use generic phrases like "I came across your profile."
Return ONLY the message text, nothing else.
"""


async def run_outreach_agent(
    job: JobListing,
    application_id: int,
    resume_text: str,
) -> bool:
    """
    Generate and send a LinkedIn connection request to the recruiter.
    Returns True if message was sent.
    """
    if not job.recruiter_name:
        print(f"[Outreach] No recruiter found for {job.company}. Skipping.")
        return False

    # Generate 2-sentence resume summary
    resume_summary = await _generate_resume_summary(resume_text)

    # Generate connection message
    prompt = OUTREACH_PROMPT.format(
        recruiter_name=job.recruiter_name,
        company=job.company,
        job_title=job.job_title,
        resume_summary=resume_summary,
    )
    message = await call_llm(prompt)
    message = message.strip()[:295]  # hard cap at 295 chars (under 300)

    print(f"[Outreach] Generated message for {job.recruiter_name}:\n  {message}")

    # Send via Playwright
    sent = await _send_linkedin_connection(job, message)

    if sent:
        log_outreach(
            application_id=application_id,
            recruiter_name=job.recruiter_name,
            company=job.company,
            message_text=message,
        )
        mark_outreach_sent(job.apply_url)
        print(f"[Outreach] Connection request sent to {job.recruiter_name} at {job.company}.")

    return sent


async def _generate_resume_summary(resume_text: str) -> str:
    """Generate a 2-sentence summary of the resume for use in outreach."""
    prompt = f"""\
Resume text:
{resume_text[:3000]}

Write exactly 2 sentences summarizing this person's background for a LinkedIn bio.
Focus on technical skills and most relevant experience.
"""
    return (await call_llm(prompt)).strip()


async def _send_linkedin_connection(job: JobListing, message: str) -> bool:
    """Use Playwright to find the recruiter on LinkedIn and send a connection request."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**get_launch_args())
        context = await browser.new_context(**get_context_options())

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        try:
            page = await linkedin_login(context)

            # Search for the recruiter
            search_url = (
                f"https://www.linkedin.com/search/results/people/"
                f"?keywords={job.recruiter_name}+{job.company}"
            )
            await page.goto(search_url, wait_until="domcontentloaded")
            await random_delay(2.0, 4.0)

            # Click first search result
            first_result = await page.query_selector(
                ".entity-result__title-text a, .app-aware-link.scale-down"
            )
            if not first_result:
                print(f"[Outreach] Recruiter '{job.recruiter_name}' not found in search.")
                return False

            await first_result.click()
            await page.wait_for_load_state("domcontentloaded")
            await random_delay(2.0, 3.5)

            # Click Connect
            connect_btn = await _find_connect_button(page)
            if connect_btn is None:
                print(f"[Outreach] No Connect button found for {job.recruiter_name}.")
                return False

            await connect_btn.click()
            await random_delay(1.5, 2.5)

            # Click "Add a note"
            add_note_btn = await page.query_selector(
                "button:has-text('Add a note'), button[aria-label*='Add a note']"
            )
            if add_note_btn:
                await add_note_btn.click()
                await random_delay(1.0, 2.0)

                # Type message
                note_textarea = await page.query_selector(
                    "textarea#custom-message, textarea[name='message']"
                )
                if note_textarea:
                    await note_textarea.type(message, delay=80)
                    await random_delay(0.8, 1.5)

            # Send
            send_btn = await page.query_selector(
                "button:has-text('Send'), button[aria-label*='Send invitation']"
            )
            if send_btn:
                await send_btn.click()
                await random_delay(1.5, 3.0)
                return True

            return False

        except Exception as exc:
            print(f"[Outreach] Error: {exc}")
            return False
        finally:
            await context.close()
            await browser.close()


async def _find_connect_button(page: Page):
    """Find the Connect button on a LinkedIn profile page."""
    selectors = [
        "button:has-text('Connect')",
        "button[aria-label*='Connect']",
        ".pvs-profile-actions button:has-text('Connect')",
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                return btn
        except Exception:
            continue

    # Check "More" dropdown
    more_btn = await page.query_selector("button:has-text('More')")
    if more_btn:
        await more_btn.click()
        await random_delay(0.8, 1.5)
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                continue

    return None

"""
naukri_flow.py
──────────────
Naukri login + apply flow + profile refresh.
Uses PopupHandler to sweep aggressively throughout apply flow.
"""
from __future__ import annotations

import asyncio
import os
import random
from typing import Optional

from playwright.async_api import Page, BrowserContext

from browser.stealth import (
    random_delay,
    human_type,
    human_click,
    human_fill,
    human_scroll,
)
from browser.popup_handler import PopupHandler, safe_goto
from memory.form_memory import get_answer, save_answer
from config import settings


async def naukri_login(context: BrowserContext) -> Page:
    """Log into Naukri.com with popup suppression throughout."""
    page = await context.new_page()
    handler = PopupHandler(page)

    await safe_goto(page, "https://www.naukri.com/nlogin/login", handler=handler)
    await random_delay(2.0, 4.0)
    await handler.dismiss_and_escape()

    await human_type(
        page,
        "input[placeholder='Enter your active Email ID / Username']",
        settings.naukri_email,
    )
    await random_delay(0.8, 1.5)
    await human_type(
        page,
        "input[placeholder='Enter your password']",
        settings.naukri_password,
    )
    await random_delay(0.8, 1.5)

    await human_click(page, "button[type='submit']")
    await page.wait_for_load_state("networkidle")
    await random_delay(2.5, 4.5)

    # Dismiss post-login notification nags and push-permission bars aggressively
    await handler.dismiss_and_escape()
    await random_delay(1.0, 2.0)
    await handler.dismiss_all()

    print("[Naukri] Logged in.")
    return page


async def apply_naukri(
    page: Page,
    apply_url: str,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
) -> bool:
    """
    Full Naukri apply flow with background popup sweeper.
    Returns True on success.
    """
    handler = PopupHandler(page)

    try:
        await safe_goto(page, apply_url, handler=handler)
        await random_delay(2.0, 4.0)

        # Background sweep — crucial for Naukri's aggressive login/push nags
        await handler.start_auto_dismiss()

        apply_btn = await _find_apply_button(page)
        if apply_btn is None:
            print(f"[Naukri] No Apply button at {apply_url}")
            await handler.stop_auto_dismiss()
            return False

        await apply_btn.click()
        await random_delay(2.0, 3.5)
        await handler.dismiss_all()

        max_steps = 10
        for step in range(max_steps):
            print(f"[Naukri] Apply step {step + 1}")

            await _handle_resume_upload(page, tailored_resume_path)
            await _fill_naukri_form(page, resume_text, llm_answer_fn)
            await handler.dismiss_all()

            action = await _get_next_action(page)
            if action == "submit":
                await _click_button_by_text(page, ["Apply", "Submit", "Apply Now"])
                await random_delay(2.0, 4.0)
                await handler.dismiss_all()
                print("[Naukri] Application submitted!")
                await handler.stop_auto_dismiss()
                return True
            elif action == "next":
                await _click_button_by_text(page, ["Next", "Save and Continue", "Continue"])
                await random_delay(1.5, 3.0)
                await handler.dismiss_all()
            elif action == "done":
                await handler.stop_auto_dismiss()
                return True
            else:
                # Try any forward CTA
                await _click_button_by_text(page, ["Next", "Continue", "Apply", "Submit"])
                await random_delay(1.5, 3.0)

        await handler.stop_auto_dismiss()
        return False

    except Exception as exc:
        print(f"[Naukri] Error during apply: {exc}")
        try:
            await handler.stop_auto_dismiss()
        except Exception:
            pass
        return False


async def profile_refresh(context: BrowserContext) -> bool:
    """Update Naukri 'Last Active' by toggling a trailing space in the headline."""
    page = await context.new_page()
    handler = PopupHandler(page)

    try:
        await safe_goto(
            page, "https://www.naukri.com/mnjuser/profile", handler=handler
        )
        await random_delay(2.0, 4.0)
        await handler.dismiss_and_escape()

        edit_btn = await page.query_selector(
            "[class*='edit-icon'], [aria-label*='Edit headline'], .edit-headline, "
            "[class*='editIcon']"
        )
        if edit_btn:
            await edit_btn.click()
            await random_delay(1.0, 2.0)
            await handler.dismiss_all()

            headline_input = await page.query_selector(
                "input[placeholder*='headline'], input[name*='headline'], "
                "input[id*='headline'], textarea[name*='headline']"
            )
            if headline_input:
                current_val = await headline_input.input_value()
                new_val = (
                    current_val.rstrip()
                    if current_val.endswith(" ")
                    else current_val + " "
                )
                await headline_input.triple_click()
                await random_delay(0.3, 0.6)
                await headline_input.type(new_val, delay=random.randint(50, 100))
                await random_delay(0.5, 1.0)

                save_btn = await page.query_selector(
                    "button:has-text('Save'), button[type='submit']"
                )
                if save_btn:
                    await save_btn.click()
                    await random_delay(1.5, 3.0)
                    print("[Naukri] Profile refreshed via headline edit.")
                    return True

        return await _refresh_via_resume_upload(page)

    except Exception as exc:
        print(f"[Naukri] Refresh error: {exc}")
        return False
    finally:
        await page.close()


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _refresh_via_resume_upload(page: Page) -> bool:
    resume_path = (
        settings.base_resume_docx
        if settings.resume_format == "docx"
        else settings.base_resume_tex
    )
    if not os.path.exists(resume_path):
        return False

    await page.goto(
        "https://www.naukri.com/mnjuser/profile?src=&profileId=",
        wait_until="domcontentloaded",
    )
    await random_delay(2.0, 3.5)

    upload_input = await page.query_selector("input[type='file']")
    if upload_input:
        await upload_input.set_input_files(resume_path)
        await random_delay(2.0, 4.0)
        save_btn = await page.query_selector(
            "button:has-text('Save'), button:has-text('Upload')"
        )
        if save_btn:
            await save_btn.click()
            await random_delay(1.5, 3.0)
            print("[Naukri] Profile refreshed via resume upload.")
            return True
    return False


async def _find_apply_button(page: Page):
    selectors = [
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        ".apply-button",
        "[class*='apply-btn']",
        "button:has-text('Apply Now')",
        "a.apply-now",
        "[id*='apply']",
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                return btn
        except Exception:
            continue
    return None


async def _handle_resume_upload(page: Page, resume_path: str) -> None:
    if not resume_path or not os.path.exists(resume_path):
        return
    try:
        upload_input = await page.query_selector("input[type='file']")
        if upload_input:
            await upload_input.set_input_files(resume_path)
            await random_delay(1.5, 3.0)
    except Exception as exc:
        print(f"[Naukri] Resume upload warning: {exc}")


async def _fill_naukri_form(page: Page, resume_text: str, llm_answer_fn) -> None:
    inputs = await page.query_selector_all(
        "input[type='text'], input[type='number'], input[type='tel'], textarea"
    )
    for inp in inputs:
        try:
            if not await inp.is_visible():
                continue
            label_text = await _get_field_label(page, inp)
            if not label_text:
                continue
            existing = await inp.input_value()
            if existing.strip():
                continue
            answer = await _resolve_answer(label_text, resume_text, llm_answer_fn)
            if answer:
                await human_fill(inp, answer)
                await random_delay(0.4, 1.0)
        except Exception as exc:
            print(f"[Naukri] Field warning: {exc}")

    selects = await page.query_selector_all("select")
    for sel_el in selects:
        try:
            label_text = await _get_field_label(page, sel_el)
            if not label_text:
                continue
            answer = get_answer(label_text)
            if answer:
                await sel_el.select_option(label=answer)
                await random_delay(0.4, 0.9)
        except Exception:
            pass


async def _resolve_answer(label: str, resume_text: str, llm_answer_fn) -> Optional[str]:
    hard_fields = [
        "phone", "mobile", "notice period", "current ctc", "expected ctc",
        "current salary", "expected salary", "location", "city", "pincode",
    ]
    saved = get_answer(label)
    if saved:
        return saved
    label_lower = label.lower()
    for hard in hard_fields:
        if hard in label_lower:
            print(f"\n[NEW FIELD] '{label}' — enter your answer:")
            answer = input("  >> ").strip()
            if answer:
                save_answer(label, answer)
                return answer
            return None
    answer = await llm_answer_fn(label, resume_text)
    if answer:
        save_answer(label, answer)
    return answer


async def _get_field_label(page: Page, element) -> str:
    try:
        aria = await element.get_attribute("aria-label")
        if aria:
            return aria.strip()
        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()
        placeholder = await element.get_attribute("placeholder")
        if placeholder:
            return placeholder.strip()
        name = await element.get_attribute("name")
        if name:
            return name.replace("-", " ").replace("_", " ").strip()
    except Exception:
        pass
    return ""


async def _get_next_action(page: Page) -> str:
    button_map = {
        "apply": "submit",
        "apply now": "submit",
        "submit": "submit",
        "next": "next",
        "save and continue": "next",
        "continue": "next",
        "done": "done",
    }
    buttons = await page.query_selector_all("button")
    for btn in buttons:
        try:
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if txt in button_map:
                return button_map[txt]
        except Exception:
            continue
    return "unknown"


async def _click_button_by_text(page: Page, texts: list[str]) -> None:
    for text in texts:
        try:
            btn = await page.query_selector(f"button:has-text('{text}')")
            if btn and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue

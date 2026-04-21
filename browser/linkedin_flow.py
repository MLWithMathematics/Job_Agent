"""
linkedin_flow.py
────────────────
LinkedIn login + Easy Apply handler.
Uses PopupHandler to continuously sweep for popups/overlays during the
entire apply flow, not just at the start.
<<<<<<< HEAD
=======

Fixes applied:
  - When no Easy Apply button is found, detect the external Apply button,
    capture the new popup tab it opens, and run the full external ATS flow
    on that tab instead of silently skipping the job.
>>>>>>> a135004 (Updated..)
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
    human_scroll,
    human_fill,
    STEALTH_INIT_SCRIPT,
)
from browser.popup_handler import PopupHandler, safe_goto
from memory.form_memory import get_answer, save_answer
from config import settings


async def linkedin_login(context: BrowserContext) -> Page:
    """Log into LinkedIn, dismiss all nags, return the logged-in page."""
    page = await context.new_page()
    handler = PopupHandler(page)

    await safe_goto(page, "https://www.linkedin.com/login", handler=handler)
    await random_delay(1.5, 3.0)
    await handler.dismiss_all()

<<<<<<< HEAD
    await human_type(page, "#username", settings.linkedin_email)
    await random_delay(0.5, 1.2)
    await human_type(page, "#password", settings.linkedin_password)
    await random_delay(0.5, 1.2)
    await human_click(page, "button[type='submit']")

    await page.wait_for_load_state("networkidle")
    await random_delay(2.5, 4.0)

    # Dismiss post-login popups: notification permission nag, messaging overlays, etc.
=======
    url = page.url.lower()
    if "feed" not in url and "/in/" not in url and not await page.query_selector("#global-nav"):
        try:
            await human_type(page, "#username, #session_key, input[name='session_key']", settings.linkedin_email)
            await random_delay(0.5, 1.2)
            await human_type(page, "#password, #session_password, input[name='session_password']", settings.linkedin_password)
            await random_delay(0.5, 1.2)
            await human_click(page, "button[type='submit']")
        except Exception as e:
            print(f"[Stealth] Warning during login injection: {e}")
    else:
        print("[Stealth] Already authenticated via cached session cookies.")

    print("[Stealth] Verifying LinkedIn login success...")
    timer = 0
    while timer < 300:  # 5 minutes max
        try:
            url = page.url.lower()
            if "feed" in url or "/in/" in url or await page.query_selector("#global-nav, .global-nav"):
                print("[Stealth] Successfully authenticated. Resuming flow...")
                break

            if "checkpoint" in url or "challenge" in url or await page.query_selector(
                "input[name='pin'], #captcha-challenge"
            ):
                if timer % 10 == 0:
                    print("\n🚨 [SECURITY VERIFICATION DETECTED] 🚨")
                    print("Please solve the captcha or enter the OTP in the browser. Waiting...")
            elif "login" in url or url in (
                "https://www.linkedin.com/", "https://linkedin.com/"
            ):
                if timer % 15 == 0:
                    print(f"\n[Stealth] Waiting for login to complete... (URL: {url})")
                    print("If it's stuck or failed, please manually resolve the login.")

            await asyncio.sleep(5)
            timer += 5
        except Exception:
            await asyncio.sleep(5)
            timer += 5

    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await random_delay(2.5, 4.0)

>>>>>>> a135004 (Updated..)
    await handler.dismiss_and_escape()
    await random_delay(1.0, 2.0)
    await handler.dismiss_all()

    print("[LinkedIn] Logged in successfully.")
    return page


async def apply_linkedin_easy_apply(
    page: Page,
    apply_url: str,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
) -> bool:
    """
    Full LinkedIn Easy Apply flow with continuous popup suppression.
<<<<<<< HEAD
=======

    If the job has no Easy Apply button (external apply), this function
    automatically detects the external Apply button, captures the new
    popup tab it opens, and runs the external ATS flow on that tab.

>>>>>>> a135004 (Updated..)
    llm_answer_fn: async callable(question: str, resume_text: str) -> str
    Returns True on success, False on failure.
    """
    handler = PopupHandler(page)

    try:
        await safe_goto(page, apply_url, handler=handler)
        await random_delay(2.0, 3.5)

        # Start background popup sweeper — runs every 2.5 s throughout apply
        await handler.start_auto_dismiss()

        easy_apply_btn = await _find_easy_apply_button(page)
        if easy_apply_btn is None:
<<<<<<< HEAD
            print(f"[LinkedIn] No Easy Apply button at {apply_url}")
            await handler.stop_auto_dismiss()
            return False
=======
            # ── No Easy Apply: fall back to external apply via popup tab ──
            print(f"[LinkedIn] No Easy Apply button at {apply_url} — trying external apply...")
            result = await _handle_external_apply(
                page=page,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
                handler=handler,
            )
            await handler.stop_auto_dismiss()
            return result
>>>>>>> a135004 (Updated..)

        await easy_apply_btn.click()
        await random_delay(2.0, 3.5)
        await handler.dismiss_all()  # sweep immediately after opening modal

        max_steps = 12
        for step in range(max_steps):
            print(f"[LinkedIn] Form step {step + 1}")

            await _handle_resume_upload(page, tailored_resume_path)
            await _fill_form_fields(page, resume_text, llm_answer_fn)
            await handler.dismiss_all()  # sweep before clicking Next

            action = await _get_next_action(page)
            if action == "submit":
                await _click_button_by_text(page, ["Submit application", "Submit"])
                await random_delay(2.0, 4.0)
                await handler.dismiss_all()  # dismiss confirmation popup
                print("[LinkedIn] Application submitted!")
                await handler.stop_auto_dismiss()
                return True
            elif action in ("review", "next"):
                labels = (
                    ["Review", "Review your application"]
                    if action == "review"
                    else ["Next", "Continue"]
                )
                await _click_button_by_text(page, labels)
                await random_delay(1.5, 3.0)
                await handler.dismiss_all()
            elif action == "done":
                await handler.stop_auto_dismiss()
                return True
            else:
                # Unknown state — try Escape to reset and bail
                await handler.press_escape()
                await random_delay(1.0, 2.0)
                await handler.stop_auto_dismiss()
                return False

        await handler.stop_auto_dismiss()
        return False

    except Exception as exc:
        print(f"[LinkedIn] Error during apply: {exc}")
        try:
            await handler.stop_auto_dismiss()
        except Exception:
            pass
        return False


<<<<<<< HEAD
=======
# ── External apply (non-Easy-Apply LinkedIn jobs) ─────────────────────────────

async def _handle_external_apply(
    page: Page,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
    handler: PopupHandler,
) -> bool:
    """
    When a LinkedIn job listing has a regular 'Apply' button (not Easy Apply),
    click it, capture the new browser tab it opens, and run the external ATS
    flow on that tab.

    Returns True on success, False on failure.
    """
    # Lazy import to avoid circular dependency (external_flow does not import
    # linkedin_flow, so this direction is safe)
    from browser.external_flow import apply_external_link  # noqa: PLC0415

    # Selectors for the non-Easy-Apply "Apply" button on LinkedIn job pages
    external_btn_selectors = [
        "button.jobs-apply-button",
        ".jobs-apply-button--top-card",
        "button[aria-label*='Apply']",
        "a[aria-label*='Apply']",
        "button:has-text('Apply')",
    ]

    apply_btn = None
    for sel in external_btn_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                btn_text = (await btn.inner_text()).strip().lower()
                if "easy apply" not in btn_text:
                    apply_btn = btn
                    break
        except Exception:
            continue

    if apply_btn is None:
        print("[LinkedIn] No external Apply button found — skipping job.")
        return False

    print("[LinkedIn] Clicking external Apply button — waiting for new tab...")

    try:
        # LinkedIn external apply buttons open a new browser tab/popup.
        # Use context.expect_page() to capture it before it disappears.
        async with page.context.expect_page(timeout=15_000) as new_page_info:
            await apply_btn.click()

        new_page = await new_page_info.value
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass  # Some ATSs are slow; proceed anyway

        ext_url = new_page.url
        print(f"[LinkedIn] External ATS URL: {ext_url}")

        success = await apply_external_link(
            page=new_page,
            apply_url=ext_url,
            tailored_resume_path=tailored_resume_path,
            resume_text=resume_text,
            llm_answer_fn=llm_answer_fn,
        )

        try:
            await new_page.close()
        except Exception:
            pass

        return success

    except Exception as exc:
        print(f"[LinkedIn] External apply via new tab failed: {exc}")

        # ── Fallback: some ATSs redirect in the same tab instead of a popup ──
        try:
            pre_click_url = page.url
            await apply_btn.click()
            await random_delay(2.0, 3.5)
            post_click_url = page.url

            if post_click_url != pre_click_url and "linkedin.com" not in post_click_url:
                print(f"[LinkedIn] Same-tab redirect to external ATS: {post_click_url}")
                success = await apply_external_link(
                    page=page,
                    apply_url=post_click_url,
                    tailored_resume_path=tailored_resume_path,
                    resume_text=resume_text,
                    llm_answer_fn=llm_answer_fn,
                )
                return success
        except Exception as inner_exc:
            print(f"[LinkedIn] Same-tab fallback also failed: {inner_exc}")

        return False


>>>>>>> a135004 (Updated..)
# ── Internal helpers ──────────────────────────────────────────────────────────

async def _find_easy_apply_button(page: Page):
    selectors = [
        "button.jobs-apply-button",
        "button[aria-label*='Easy Apply']",
        ".jobs-apply-button--top-card",
        "button:has-text('Easy Apply')",
        ".jobs-s-apply button",
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
<<<<<<< HEAD
                return btn
=======
                btn_text = (await btn.inner_text()).strip().lower()
                if "easy apply" in btn_text:
                    return btn
>>>>>>> a135004 (Updated..)
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
            print(f"[LinkedIn] Uploaded resume: {resume_path}")
    except Exception as exc:
        print(f"[LinkedIn] Resume upload warning: {exc}")


async def _fill_form_fields(page: Page, resume_text: str, llm_answer_fn) -> None:
    """Detect and fill all form fields on the current modal page."""
    # --- Text / number / tel / textarea ---
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
            print(f"[LinkedIn] Text field warning: {exc}")

    # --- Select dropdowns ---
    selects = await page.query_selector_all("select")
    for sel_el in selects:
        try:
            if not await sel_el.is_visible():
                continue
            label_text = await _get_field_label(page, sel_el)
            if not label_text:
                continue
            answer = get_answer(label_text)
            if answer:
                await sel_el.select_option(label=answer)
                await random_delay(0.4, 0.9)
        except Exception:
            pass

    # --- Fieldset radios ---
    fieldsets = await page.query_selector_all("fieldset")
    for fs in fieldsets:
        try:
            legend = await fs.query_selector("legend")
            label_text = (await legend.inner_text()).strip() if legend else ""
            radios = await fs.query_selector_all("input[type='radio']")
            if not radios:
                continue
            any_checked = any([await r.is_checked() for r in radios])
            if any_checked:
                continue
            saved = get_answer(label_text)
            clicked = False
            if saved:
                for r in radios:
                    r_label = await _get_field_label(page, r)
                    if r_label and saved.lower() in r_label.lower():
                        await r.click()
                        await random_delay(0.3, 0.7)
                        clicked = True
                        break
            if not clicked:
                # Default: "yes" if present, else first option
                for r in radios:
                    r_label = (await _get_field_label(page, r)).lower()
                    if "yes" in r_label:
                        await r.click()
                        await random_delay(0.3, 0.7)
                        clicked = True
                        break
                if not clicked and radios:
                    await radios[0].click()
                    await random_delay(0.3, 0.7)
        except Exception:
            pass


async def _resolve_answer(
    label: str, resume_text: str, llm_answer_fn
) -> Optional[str]:
    """Memory → hard-field stdin prompt → LLM."""
    hard_fields = [
        "phone", "mobile", "notice period", "current ctc", "expected ctc",
        "current salary", "expected salary", "location", "city", "pincode",
        "date of birth", "nationality",
    ]

    saved = get_answer(label)
    if saved:
        print(f"[FormMemory] '{label}' → '{saved}'")
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

    print(f"[LLM] Dynamic Q: '{label}'")
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
        "submit application": "submit",
        "submit": "submit",
        "review": "review",
        "review your application": "review",
        "next": "next",
        "continue": "next",
        "done": "done",
    }
    buttons = await page.query_selector_all(
        "button[type='button'], button[type='submit']"
    )
    for btn in buttons:
        try:
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if txt in button_map:
                return button_map[txt]
        except Exception:
            continue

    # If modal has closed, we're done
    modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal")
    if not modal:
        return "done"
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


async def solve_captcha_if_present(page: Page) -> bool:
    captcha_selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "#captcha-challenge",
        ".captcha",
    ]
    for sel in captcha_selectors:
        el = await page.query_selector(sel)
        if el:
            print("\n[CAPTCHA] Detected.")
            if settings.twocaptcha_api_key:
                return await _solve_with_2captcha(page)
            else:
                print("[CAPTCHA] Solve it manually in the browser, then press ENTER here.")
                input()
                return True
    return True


async def _solve_with_2captcha(page: Page) -> bool:
    import requests

    try:
        sitekey_el = await page.query_selector(".g-recaptcha, [data-sitekey]")
        if not sitekey_el:
            return False
        sitekey = await sitekey_el.get_attribute("data-sitekey")
        resp = requests.post(
            "https://2captcha.com/in.php",
            data={
                "key": settings.twocaptcha_api_key,
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page.url,
            },
            timeout=30,
        )
        if resp.text.startswith("OK|"):
            captcha_id = resp.text.split("|")[1]
            for _ in range(20):
                await asyncio.sleep(10)
                result = requests.get(
                    f"https://2captcha.com/res.php?key={settings.twocaptcha_api_key}"
                    f"&action=get&id={captcha_id}",
                    timeout=15,
                ).text
                if result.startswith("OK|"):
                    token = result.split("|")[1]
                    await page.evaluate(
                        f"document.getElementById('g-recaptcha-response').innerHTML = '{token}'"
                    )
                    return True
    except Exception as exc:
        print(f"[2captcha] Error: {exc}")
    return False

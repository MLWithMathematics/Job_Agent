"""
naukri_flow.py
──────────────
Naukri login + apply flow + profile refresh.
Uses PopupHandler to sweep aggressively throughout apply flow.

Fixes applied:
  - Success detection after initial Apply click (quick / profile-based apply)
  - Success detection at the top of each form step so the loop exits early
  - Modal-presence check so the loop exits if the form was dismissed
  - _get_next_action() now scopes button search inside the apply modal,
    preventing background "Apply" buttons from being mistaken for next actions
  - External apply detection: when Naukri's Apply button redirects to a
    non-Naukri URL (new tab or same-tab), the external ATS flow is used.
"""
from __future__ import annotations

import asyncio
import os
import re
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


# ── Success detection ─────────────────────────────────────────────────────────

_NAUKRI_SUCCESS_RE = re.compile(
    r"(application.{0,30}(submitted|received|sent|success|complete|saved)|"
    r"thank.{0,10}(you|applying)|"
    r"(successfully|already).{0,20}applied|"
    r"you.{0,10}(have|ve).{0,20}applied|"
    r"congratulations)",
    re.IGNORECASE,
)

# Selectors for the Naukri apply modal / chatbot form
_APPLY_MODAL_SELECTORS = [
    "[class*='apply-form']",
    "[class*='applyForm']",
    "[class*='chatbot']",
    "[class*='apply-modal']",
    ".apply-button-container",
    "[class*='apply-body']",
    "[class*='applyBody']",
    "div[data-modal-id]",
    "[role='dialog']",
]


async def _is_naukri_success(page: Page) -> bool:
    """Return True if the page shows a successful application state."""
    try:
        content = await page.inner_text("body")
        if _NAUKRI_SUCCESS_RE.search(content):
            return True
        for sel in (
            "button:has-text('Applied')",
            "[class*='applied-btn']",
            "[class*='alreadyApplied']",
            "[class*='apply-success']",
            "[class*='successMsg']",
            ".success-wrapper",
        ):
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _get_apply_modal(page: Page):
    """Return the active apply modal element, or None if it is not open."""
    for sel in _APPLY_MODAL_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


# ── Login ─────────────────────────────────────────────────────────────────────

async def naukri_login(context: BrowserContext) -> Page:
    """
    Log into Naukri.com and return the logged-in page.

    If the persistent session is already authenticated this function returns
    immediately without entering credentials.  Login form fill only happens
    when a login wall is detected.
    """
    page = await context.new_page()
    handler = PopupHandler(page)

    # ── Probe: are we already authenticated? ────────────────────────
    already_logged_in = False
    try:
        await safe_goto(page, "https://www.naukri.com/mnjuser/homepage", handler=handler)
        await random_delay(2.0, 3.5)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await handler.dismiss_and_escape()

        url = page.url.lower()
        if "login" in url or "nlogin" in url:
            already_logged_in = False
        elif "mnjuser" in url:
            header = await page.query_selector(
                ".nI-gNb-drawer, .nI-gNb-header, .nI-gNb-user-icon, .user-name"
            )
            already_logged_in = header is not None
        else:
            already_logged_in = False
    except Exception as probe_err:
        print(f"[Naukri] Session probe failed ({probe_err}) -- will do fresh login.")
        already_logged_in = False

    if already_logged_in:
        print("[Naukri] Already authenticated via saved session -- skipping credential entry.")
        await handler.dismiss_all()
        print("[Naukri] Session active. [OK]")
        return page

    # ── Not logged in — fill credentials ─────────────────────────────
    await safe_goto(page, "https://www.naukri.com/nlogin/login", handler=handler)
    await random_delay(2.0, 4.0)
    await handler.dismiss_and_escape()

    await human_type(
        page,
        "input#usernameField, input[placeholder*='Username'], input[placeholder*='Email ID']",
        settings.naukri_email,
    )
    await random_delay(0.8, 1.5)
    await human_type(
        page,
        "input#passwordField, input[type='password']",
        settings.naukri_password,
    )
    await random_delay(0.8, 1.5)

    await human_click(page, "button[type='submit']")

    print("[Naukri] Verifying login success...")
    timer = 0
    while timer < 300:  # 5 minutes max
        try:
            url = page.url.lower()
            if "mnjuser" in url or "homepage" in url or await page.query_selector(
                ".nI-gNb-drawer, .nI-gNb-header, .nI-gNb-user-icon, .user-name"
            ):
                print("[Naukri] Successfully authenticated. Resuming flow...")
                break

            if "challenge" in url or "captcha" in url or await page.query_selector(
                "input[maxlength='6'], .otp-container, iframe[src*='captcha']"
            ):
                if timer % 10 == 0:
                    print("\n!! [SECURITY VERIFICATION DETECTED] !!")
                    print("Please solve the captcha or enter the OTP in the browser. Waiting...")
            else:
                if timer % 15 == 0:
                    print(f"\n[Naukri] Waiting for login to complete... (URL: {url})")
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
    await random_delay(2.5, 4.5)

    await handler.dismiss_and_escape()
    await random_delay(1.0, 2.0)
    await handler.dismiss_all()

    print("[Naukri] Logged in. Session saved to disk. [OK]")
    return page



# ── Apply flow ────────────────────────────────────────────────────────────────

async def apply_naukri(
    page: Page,
    apply_url: str,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
) -> bool:
    """
    Full Naukri apply flow with background popup sweeper.
    Handles: native Naukri apply, quick-apply, and external ATS redirects.
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

        # ── Detect external apply: new-tab popup ─────────────────────────
        # Some Naukri listings open the company ATS in a new browser tab
        try:
            async with page.context.expect_page(timeout=4_000) as new_page_info:
                await apply_btn.click()
            new_page = await new_page_info.value
            # Wait for the new tab to navigate away from about:blank
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            await random_delay(1.5, 3.0)
            ext_url = new_page.url
            if ext_url and ext_url not in ("about:blank", "") and "naukri.com" not in ext_url:
                print(f"[Naukri] External ATS detected (new tab): {ext_url}")
                await handler.stop_auto_dismiss()
                from browser.external_flow import apply_external_link
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
            else:
                # New tab was also Naukri (unlikely) — close it and proceed on original
                try:
                    await new_page.close()
                except Exception:
                    pass
        except Exception:
            # No new tab opened — Apply button worked on same page (normal native flow)
            pass

        await random_delay(2.0, 3.5)
        await handler.dismiss_all()

        # ── Detect external apply: same-tab redirect ──────────────────────
        current_url = page.url
        if "naukri.com" not in current_url:
            print(f"[Naukri] External ATS detected (same tab redirect): {current_url}")
            await handler.stop_auto_dismiss()
            from browser.external_flow import apply_external_link
            success = await apply_external_link(
                page=page,
                apply_url=current_url,
                tailored_resume_path=tailored_resume_path,
                resume_text=resume_text,
                llm_answer_fn=llm_answer_fn,
            )
            return success

        # ── Quick / profile-based apply: done after the first click ──────
        if await _is_naukri_success(page):
            print("[Naukri] Application submitted on first click (quick / profile apply)!")
            await handler.stop_auto_dismiss()
            return True

        # ── Multi-step native form loop ───────────────────────────────────
        max_steps = 10
        for step in range(max_steps):
            print(f"[Naukri] Apply step {step + 1}")

            # 1. Exit early if success indicator appeared
            if await _is_naukri_success(page):
                print("[Naukri] Application submitted (success detected mid-flow)!")
                await handler.stop_auto_dismiss()
                return True

            # 2. Exit if the apply modal/form has been closed
            modal = await _get_apply_modal(page)
            if modal is None:
                if await _is_naukri_success(page):
                    print("[Naukri] Application submitted (modal closed after submit)!")
                    await handler.stop_auto_dismiss()
                    return True
                print("[Naukri] Apply modal closed unexpectedly — stopping loop.")
                await handler.stop_auto_dismiss()
                return False

            # 3. Fill the current form page
            await _handle_resume_upload(page, tailored_resume_path)
            await _fill_naukri_form(page, resume_text, llm_answer_fn)
            await handler.dismiss_all()

            action = await _get_next_action(page)

            if action == "submit":
                # Pause auto-dismiss around the final click
                await handler.stop_auto_dismiss()
                await _click_button_by_text(page, ["Apply", "Submit", "Apply Now"])
                await random_delay(2.0, 4.0)
                await handler.dismiss_all()
                print("[Naukri] Application submitted!")
                return True

            elif action == "next":
                await _click_button_by_text(page, ["Next", "Save and Continue", "Continue"])
                await random_delay(1.5, 3.0)
                await handler.dismiss_all()

            elif action == "done":
                await handler.stop_auto_dismiss()
                return True

            else:
                # Unknown action — try a generic forward CTA
                clicked = await _click_button_by_text(
                    page, ["Next", "Continue", "Apply", "Submit"]
                )
                if not clicked:
                    print(f"[Naukri] No actionable button at step {step + 1} — stopping.")
                    await handler.stop_auto_dismiss()
                    return False
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


# ── Profile refresh ───────────────────────────────────────────────────────────

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
    """
    Fill the current Naukri apply modal / chatbot form.
    Scoped to the modal container so nothing on the job-listing page is touched.
    """
    # Scope queries to the modal to avoid touching page-level inputs
    modal = await _get_apply_modal(page)
    scope = modal or page

    # --- Text / number / tel / email / url / textarea ---
    inputs = await scope.query_selector_all(
        "input[type='text'], input[type='number'], input[type='tel'], "
        "input[type='email'], input[type='url'], textarea"
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
                try:
                    maxlen = await inp.get_attribute("maxlength")
                    cap = int(maxlen) if maxlen and str(maxlen).isdigit() else 500
                except Exception:
                    cap = 500
                await human_fill(inp, answer[:cap])
                await random_delay(0.4, 1.0)
        except Exception as exc:
            print(f"[Naukri] Field warning: {exc}")

    # --- Native select dropdowns ---
    for sel_el in await scope.query_selector_all("select"):
        try:
            if not await sel_el.is_visible():
                continue
            label_text = await _get_field_label(page, sel_el)
            if not label_text:
                continue
            answer = get_answer(label_text) or _settings_sync_answer(label_text)
            if answer:
                try:
                    await sel_el.select_option(label=answer)
                    await random_delay(0.4, 0.9)
                    continue
                except Exception:
                    pass
            options = await sel_el.query_selector_all("option")
            opts = [t for t in [(await o.inner_text()).strip() for o in options]
                    if t and t not in ("Select", "-- Select --", "Choose", "")]
            if opts:
                ans = await llm_answer_fn(f"{label_text} (choose one: {', '.join(opts)})", resume_text)
                if ans:
                    try:
                        await sel_el.select_option(label=ans)
                        save_answer(label_text, ans)
                        await random_delay(0.3, 0.7)
                    except Exception:
                        pass
        except Exception:
            pass

    # --- Radio fieldsets ---
    for fs in await scope.query_selector_all("fieldset"):
        try:
            legend = await fs.query_selector("legend")
            label_text = (await legend.inner_text()).strip() if legend else ""
            radios = await fs.query_selector_all("input[type='radio']")
            if not radios or any([await r.is_checked() for r in radios]):
                continue
            saved = get_answer(label_text) or _settings_sync_answer(label_text)
            clicked = False
            if saved:
                for r in radios:
                    r_label = await _get_field_label(page, r)
                    if r_label and saved.lower() in r_label.lower():
                        await r.click()
                        await random_delay(0.2, 0.5)
                        clicked = True
                        break
            if not clicked:
                for r in radios:
                    r_label = (await _get_field_label(page, r)).lower()
                    if "yes" in r_label:
                        await r.click()
                        await random_delay(0.2, 0.5)
                        clicked = True
                        break
                if not clicked and radios:
                    await radios[0].click()
                    await random_delay(0.2, 0.5)
        except Exception:
            pass

    # --- Standalone checkboxes (terms, consent) ---
    for cb in await scope.query_selector_all("input[type='checkbox']:not([disabled])"):
        try:
            if not await cb.is_visible() or await cb.is_checked():
                continue
            label = (await _get_field_label(page, cb)).lower()
            if any(kw in label for kw in ("agree", "consent", "terms", "privacy", "authoriz")):
                await cb.check()
                await random_delay(0.2, 0.5)
        except Exception:
            pass


def _settings_sync_answer(label: str) -> Optional[str]:
    """Synchronous settings map lookup — used by form filling before LLM."""
    full_name = settings.full_name or " ".join(
        part for part in (settings.first_name, settings.last_name) if part
    ).strip()
    email = settings.email or settings.linkedin_email or settings.naukri_email

    smap = {
        "phone": settings.phone, "mobile": settings.phone,
        "mobile number": settings.phone, "contact number": settings.phone,
        "phone number": settings.phone,
        "city": settings.current_location, "location": settings.current_location,
        "current location": settings.current_location,
        "notice period": settings.notice_period,
        "current ctc": settings.current_ctc, "current salary": settings.current_ctc,
        "expected ctc": settings.expected_ctc, "expected salary": settings.expected_ctc,
        "total experience": settings.total_experience_years,
        "years of experience": settings.total_experience_years,
        "experience": settings.total_experience_years,
        "full name": full_name, "name": full_name,
        "first name": settings.first_name, "last name": settings.last_name,
        "surname": settings.last_name,
        "email": email, "email address": email,
        "email id": email,
        "linkedin": settings.linkedin_url, "linkedin url": settings.linkedin_url,
        "github": settings.github_url, "github url": settings.github_url,
        "portfolio": settings.portfolio_url, "website": settings.portfolio_url,
        "college": settings.college, "university": settings.college,
        "degree": settings.degree, "qualification": settings.degree,
        "graduation year": settings.graduation_year,
        "current company": settings.current_company,
        "current role": settings.current_role,
        "job title": settings.current_role, "designation": settings.current_role,
        "work authorization": settings.work_authorization,
        "eligible to work": settings.work_authorization,
        "visa sponsorship": "No", "require sponsorship": "No",
        "gender": settings.gender, "nationality": settings.nationality,
    }
    label_lower = label.lower()
    for key, value in smap.items():
        if key in label_lower and value:
            return value
    return None


async def _resolve_answer(label: str, resume_text: str, llm_answer_fn) -> Optional[str]:
    """Settings map → memory cache → stdin (sensitive only) → LLM."""
    # 1. Settings (instant)
    answer = _settings_sync_answer(label)
    if answer:
        print(f"[Settings] '{label}' -> '{answer}'")
        save_answer(label, answer)
        return answer

    # 2. Memory cache
    saved = get_answer(label)
    if saved:
        print(f"[FormMemory] '{label}' -> '{saved}'")
        return saved

    # 3. stdin only for genuinely sensitive fields
    sensitive = [
        "date of birth",
        "passport",
        "ssn",
        "aadhaar",
        "pan number",
        "bank account",
        "name",
        "full name",
        "first name",
        "last name",
        "email",
        "email address",
        "email id",
    ]
    label_lower = label.lower()
    for field in sensitive:
        if field in label_lower:
            print(f"\n[Naukri][SENSITIVE FIELD] '{label}' — enter your answer:")
            ans = input("  >> ").strip()
            if ans:
                save_answer(label, ans)
                return ans
            return None

    # 4. LLM
    print(f"[LLM] Dynamic Q: '{label}'")
    answer = await llm_answer_fn(label, resume_text)
    if answer:
        answer = answer.strip()[:300]
        save_answer(label, answer)
    return answer or None


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
    """
    Determine the next action from visible buttons.
    Scoped to the apply modal when one is open so that background
    'Apply' buttons on the job-detail page are not mistaken for
    form-submit actions.
    """
    button_map = {
        "apply": "submit",
        "apply now": "submit",
        "submit": "submit",
        "next": "next",
        "save and continue": "next",
        "continue": "next",
        "done": "done",
    }

    # Prefer to search inside the modal so background buttons are ignored
    modal = await _get_apply_modal(page)
    if modal:
        try:
            buttons = await modal.query_selector_all("button")
        except Exception:
            buttons = await page.query_selector_all("button")
    else:
        # No modal — form must have been dismissed or navigation happened
        return "done"

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


async def _click_button_by_text(page: Page, texts: list[str]) -> bool:
    """Click the first visible button matching any of the given texts.
    Returns True if a button was clicked, False otherwise."""
    for text in texts:
        try:
            btn = await page.query_selector(f"button:has-text('{text}')")
            if btn and await btn.is_visible():
                await btn.click()
                return True
        except Exception:
            continue
    return False

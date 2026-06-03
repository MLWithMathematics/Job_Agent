"""
linkedin_flow.py
────────────────
LinkedIn login + Easy Apply handler.
Uses PopupHandler to continuously sweep for popups/overlays during the
entire apply flow, not just at the start.

Design:
  - Only LinkedIn Easy Apply is automated.  Jobs without an Easy Apply button
    are returned as failed (the caller marks them as 'manual_apply' in the
    dashboard so the user can apply manually).
  - _find_easy_apply_button() retries with scrolling to handle lazy-loading.
  - The form step loop includes retry logic and discard-dialog handling.
  - Login waits in a polling loop for up to 5 min, allowing manual
    captcha / OTP resolution.
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
    human_click_element,
    human_scroll,
    human_fill,
    STEALTH_INIT_SCRIPT,
)
from browser.popup_handler import PopupHandler, safe_goto
from memory.form_memory import get_answer, save_answer
from config import settings


async def linkedin_login(context: BrowserContext) -> Page:
    """
    Log into LinkedIn and return the logged-in page.

    If the persistent session is already authenticated (cookies still valid)
    this function simply returns a page on the feed without typing credentials.
    Login form fill only happens when we detect we are NOT yet authenticated.
    """
    page = await context.new_page()
    handler = PopupHandler(page)

    # ── Probe: are we already authenticated? ────────────────────────
    # Navigate to /feed/ and see if we stay there (vs redirect to login).
    # If this fails entirely (connection error, corrupted session), just
    # fall through to the login form — not a fatal error.
    already_logged_in = False
    try:
        await safe_goto(page, "https://www.linkedin.com/feed/", handler=handler)
        await random_delay(2.0, 3.5)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await handler.dismiss_all()

        url = page.url.lower()
        on_auth_page = any(
            kw in url for kw in ("login", "signup", "checkpoint", "challenge", "authwall")
        )

        from urllib.parse import urlparse
        path = urlparse(page.url).path.lower()
        nav = await page.query_selector("#global-nav, .global-nav")

        already_logged_in = (
            not on_auth_page
            and (path.startswith("/feed") or path.startswith("/in/"))
            and nav is not None
        )
    except Exception as probe_err:
        print(f"[LinkedIn] Session probe failed ({probe_err}) — will do fresh login.")
        already_logged_in = False

    if already_logged_in:
        print("[LinkedIn] Already authenticated via saved session — skipping credential entry.")
        await handler.dismiss_and_escape()
        await random_delay(1.0, 2.0)
        await handler.dismiss_all()
        print("[LinkedIn] Session active. [OK]")
        return page

    # ── Not logged in — fill credentials ─────────────────────────────
    await safe_goto(page, "https://www.linkedin.com/login", handler=handler)
    await random_delay(1.5, 3.0)
    await handler.dismiss_all()

    try:
        await human_type(page, "#username, #session_key, input[name='session_key']", settings.linkedin_email)
        await random_delay(0.5, 1.2)
        await human_type(page, "#password, #session_password, input[name='session_password']", settings.linkedin_password)
        await random_delay(0.5, 1.2)
        await human_click(page, "button[type='submit']")
    except Exception as e:
        print(f"[LinkedIn] Warning during login injection: {e}")

    print("[LinkedIn] Verifying login success...")
    timer = 0
    while timer < 300:  # 5 minutes max
        try:
            url = page.url.lower()
            if "feed" in url or "/in/" in url or await page.query_selector("#global-nav, .global-nav"):
                print("[LinkedIn] Successfully authenticated. Resuming flow...")
                break

            if "checkpoint" in url or "challenge" in url or await page.query_selector(
                "input[name='pin'], #captcha-challenge"
            ):
                if timer % 10 == 0:
                    print("\n!! [SECURITY VERIFICATION DETECTED] !!")
                    print("Please solve the captcha or enter the OTP in the browser. Waiting...")
            elif "login" in url or url in (
                "https://www.linkedin.com/", "https://linkedin.com/"
            ):
                if timer % 15 == 0:
                    print(f"\n[LinkedIn] Waiting for login to complete... (URL: {url})")
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

    await handler.dismiss_and_escape()
    await random_delay(1.0, 2.0)
    await handler.dismiss_all()

    print("[LinkedIn] Logged in successfully. Session saved to disk. [OK]")
    return page



async def apply_linkedin_easy_apply(
    page: Page,
    apply_url: str,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
    job_title: str = "",
    jd_text: str = "",
    cover_letter_path: str = "",
) -> bool:
    """
    Full LinkedIn Easy Apply flow with continuous popup suppression.

    Only automates LinkedIn Easy Apply.  If no Easy Apply button is found,
    returns False so the caller marks it as 'manual_apply'.

    llm_answer_fn: async callable(question: str, resume_text: str, job_context: str) -> str
    Returns True on success, False on failure.
    """
    handler = PopupHandler(page)
    resume_uploaded = False  # track to avoid re-uploading every step

    # Build job context string for role-aware LLM answers
    _job_context = ""
    if job_title or jd_text:
        _job_context = f"Role: {job_title}\n{jd_text[:1200]}" if jd_text else f"Role: {job_title}"

    try:
        await safe_goto(page, apply_url, handler=handler)
        await random_delay(1.0, 2.0)

        # Start background popup sweeper — runs every 3 s throughout apply
        await handler.start_auto_dismiss(interval=3.0)

        # ── Check if already applied ──────────────────────────────────
        try:
            body_text = (await page.inner_text("body")).lower()
            if "applied" in body_text:
                applied_badge = await page.query_selector(
                    ".jobs-s-apply .artdeco-inline-feedback, "
                    "[class*='applied-badge'], [class*='already-applied']"
                )
                if applied_badge and await applied_badge.is_visible():
                    print(f"[LinkedIn] Already applied to this job — skipping.")
                    await handler.stop_auto_dismiss()
                    return False
        except Exception:
            pass

        # ── Retry finding the Easy Apply button ───────────────────────
        easy_apply_btn = None
        for attempt in range(3):
            easy_apply_btn = await _find_easy_apply_button(page)
            if easy_apply_btn is not None:
                break
            if attempt < 2:
                print(f"[LinkedIn] Easy Apply button not found (attempt {attempt+1}) — scrolling & retrying...")
                await page.evaluate("window.scrollTo(0, 0)")
                await random_delay(0.5, 1.0)
                await handler.dismiss_all()
                await page.evaluate("window.scrollBy(0, 400)")
                await random_delay(0.8, 1.5)

        if easy_apply_btn is None:
            print(f"[LinkedIn] No Easy Apply button at {apply_url} — flagging for manual apply.")
            await handler.stop_auto_dismiss()
            return False

        # ── Click Easy Apply and VERIFY the modal actually opened ─────
        await human_click_element(easy_apply_btn, page)
        await random_delay(0.8, 1.5)

        # Wait for the Easy Apply modal specifically (up to 4 seconds)
        modal = None
        for _ in range(4):
            modal = await _find_easy_apply_modal(page)
            if modal:
                break
            await asyncio.sleep(0.8)

        if modal is None:
            # Modal never opened — this is NOT a real Easy Apply job
            print("[LinkedIn] Easy Apply modal did NOT open after clicking — not a real Easy Apply job.")
            await handler.stop_auto_dismiss()
            return False

        print("[LinkedIn] Easy Apply modal opened — starting form fill.")

        max_steps = 15
        last_action = None
        stall_count = 0
        modal_ever_had_fields = False
        cleared_on_stall = False  # True if we already tried clear-and-retry

        for step in range(max_steps):
            print(f"[LinkedIn] Form step {step + 1}/{max_steps}")

            # Only upload resume once
            if not resume_uploaded:
                uploaded = await _handle_resume_upload(page, tailored_resume_path, cover_letter_path)
                if uploaded:
                    resume_uploaded = True

            fields_filled = await _fill_form_fields(page, resume_text, llm_answer_fn, _job_context)
            if fields_filled > 0:
                modal_ever_had_fields = True

            action = await _get_next_action(page, step)

            # Stall detection — with clear-and-retry before bailing
            if action == last_action and action not in ("submit", "done"):
                stall_count += 1
                if stall_count >= 2 and not cleared_on_stall:
                    # First stall recovery: detect validation errors and
                    # clear problematic fields so they get re-filled
                    print("[LinkedIn] Stall detected — checking for validation errors...")
                    errors_cleared = await _clear_errored_fields(page)
                    if errors_cleared > 0:
                        print(f"[LinkedIn] Cleared {errors_cleared} errored field(s) — retrying fill...")
                        stall_count = 0  # Reset counter to give retry a fair chance
                    cleared_on_stall = True
                elif stall_count >= 3:
                    print("[LinkedIn] Stalled — same step repeated 3+ times after retry. Bailing.")
                    await handler.press_escape()
                    await _dismiss_discard_dialog(page)
                    await handler.stop_auto_dismiss()
                    return False
            else:
                stall_count = 0
                cleared_on_stall = False
            last_action = action

            if action == "submit":
                # Scroll modal to bottom so Submit button is in viewport
                await _scroll_modal_to_bottom(page)
                await random_delay(0.3, 0.5)
                await _click_button_by_text(page, ["Submit application", "Submit"])
                await random_delay(1.5, 2.5)

                # Verify submission
                post_modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal")
                page_text = ""
                try:
                    page_text = (await page.inner_text("body")).lower()
                except Exception:
                    pass
                if not post_modal or "submitted" in page_text or "thank" in page_text:
                    print("[LinkedIn] ✓ Application submitted successfully!")
                    await handler.stop_auto_dismiss()
                    return True
                else:
                    print("[LinkedIn] Submit clicked but modal still open — retrying fields...")
                    await _fill_form_fields(page, resume_text, llm_answer_fn, _job_context)
                    await _click_button_by_text(page, ["Submit application", "Submit"])
                    await random_delay(1.5, 2.5)
                    modal_retry = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal")
                    if not modal_retry:
                        print("[LinkedIn] ✓ Application submitted on retry!")
                        await handler.stop_auto_dismiss()
                        return True
                    print("[LinkedIn] Submit still blocked — bailing.")
                    await handler.stop_auto_dismiss()
                    return False

            elif action in ("review", "next"):
                labels = (
                    ["Review", "Review your application"]
                    if action == "review"
                    else ["Next", "Continue"]
                )
                await _click_button_by_text(page, labels)
                await random_delay(0.8, 1.5)

                # ── Validation error detection ────────────────────────
                # After clicking Next, check if LinkedIn showed errors
                # (red text, required-field markers).  If yes, the form
                # stayed on the same step — clear those fields so the
                # next loop iteration can re-fill them.
                has_errors = await _detect_validation_errors(page)
                if has_errors:
                    print("[LinkedIn] Validation error(s) detected after clicking Next — will clear and retry.")
                    await _clear_errored_fields(page)

                await _dismiss_discard_dialog(page)

            elif action == "done":
                # Only trust "done" if we actually interacted with a form
                if step == 0 and not modal_ever_had_fields:
                    print("[LinkedIn] Modal closed instantly on step 1 with no fields — false positive, not applied.")
                    await handler.stop_auto_dismiss()
                    return False
                print("[LinkedIn] ✓ Application completed (modal closed after form interaction).")
                await handler.stop_auto_dismiss()
                return True

            else:
                await handler.press_escape()
                await random_delay(0.3, 0.6)
                await _dismiss_discard_dialog(page)
                await handler.stop_auto_dismiss()
                return False

        print("[LinkedIn] Max steps reached — bailing.")
        await handler.stop_auto_dismiss()
        return False

    except Exception as exc:
        print(f"[LinkedIn] Error during apply: {exc}")
        try:
            await handler.stop_auto_dismiss()
        except Exception:
            pass
        return False


# ── Validation error detection helpers ────────────────────────────────────────

async def _detect_validation_errors(page: Page) -> bool:
    """
    Check if LinkedIn is showing any validation error messages inside the
    Easy Apply modal (red text, required-field markers, error banners).
    Returns True if errors are detected.
    """
    modal = await _find_easy_apply_modal(page)
    scope = modal or page

    error_selectors = [
        ".artdeco-inline-feedback--error",
        ".fb-dash-form-element__error-field",
        "[data-test-form-element-error]",
        ".artdeco-text-input--error",
        ".jobs-easy-apply-form-element__error",
        "[class*='error-message']",
        "[class*='form-error']",
        ".field-error",
    ]
    for sel in error_selectors:
        try:
            el = await scope.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _clear_errored_fields(page: Page) -> int:
    """
    Find form fields with validation errors inside the Easy Apply modal,
    clear their values so they can be re-filled in the next iteration.
    Also removes the bad answer from form memory.
    Returns the number of fields cleared.
    """
    modal = await _find_easy_apply_modal(page)
    scope = modal or page
    cleared = 0

    # Find all inputs that are in an error state
    # LinkedIn marks error fields by adding an error class to the container
    # or to the input itself, or by showing an error element nearby.
    inputs = await scope.query_selector_all(
        "input[type='text'], input[type='number'], input[type='tel'], "
        "input[type='url'], input[type='email'], input[type='date'], textarea"
    )
    for inp in inputs:
        try:
            if not await inp.is_visible():
                continue
            has_error = await _field_has_error(page, inp)
            if not has_error:
                continue
            existing = await inp.input_value()
            if not existing.strip():
                continue  # Empty field with error — nothing to clear
            label = await _get_field_label(page, inp)
            # Clear the field value
            await inp.click()
            await inp.fill("")
            await random_delay(0.1, 0.2)
            # Remove the bad answer from form memory
            if label:
                try:
                    from memory.form_memory import _normalize, _load, _save
                    data = _load()
                    norm = _normalize(label)
                    if norm in data:
                        del data[norm]
                        _save(data)
                except Exception:
                    pass
            cleared += 1
            print(f"[LinkedIn] Cleared errored field: '{label}' (was '{existing[:30]}')")
        except Exception:
            continue
    return cleared


async def _field_has_error(page: Page, element) -> bool:
    """
    Check if a specific form field element has a validation error.
    Looks at the element's own classes, its parent container classes,
    and nearby error message elements.
    """
    try:
        # 1. Check the input element itself for error classes
        classes = (await element.get_attribute("class") or "").lower()
        if "error" in classes or "invalid" in classes:
            return True

        # 2. Check aria-invalid attribute
        aria_invalid = await element.get_attribute("aria-invalid")
        if aria_invalid and aria_invalid.lower() == "true":
            return True

        # 3. Walk up to parent container and check for error indicators
        has_parent_error = await page.evaluate(
            """(el) => {
                let node = el.parentElement;
                for (let i = 0; i < 4; i++) {
                    if (!node) break;
                    const cls = (node.className || '').toLowerCase();
                    if (cls.includes('error') || cls.includes('invalid')) return true;
                    // Check for LinkedIn-specific error elements as siblings
                    const err = node.querySelector(
                        '.artdeco-inline-feedback--error, ' +
                        '.fb-dash-form-element__error-field, ' +
                        '[data-test-form-element-error], ' +
                        '[class*="error-message"], ' +
                        '[class*="form-error"]'
                    );
                    if (err) {
                        const style = window.getComputedStyle(err);
                        if (style.display !== 'none' && style.visibility !== 'hidden') {
                            return true;
                        }
                    }
                    node = node.parentElement;
                }
                return false;
            }""",
            element,
        )
        return bool(has_parent_error)
    except Exception:
        return False


# ── Discard-dialog handler ────────────────────────────────────────────────────

async def _dismiss_discard_dialog(page: Page) -> None:
    """
    LinkedIn shows a "Discard this application?" dialog when Escape is pressed
    or navigation happens mid-apply.  Dismiss it by clicking "Continue applying"
    or the keep / dismiss button so the flow doesn't break.
    """
    try:
        discard_selectors = [
            "button[data-test-dialog-primary-btn]",
            "button:has-text('Discard')",
            "button:has-text('Continue applying')",
            "button:has-text('Keep')",
            "button:has-text('Save')",
        ]
        for sel in discard_selectors:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                text = (await btn.inner_text()).strip().lower()
                # Click "Continue applying" or "Keep" — NOT "Discard"
                if "discard" not in text:
                    await btn.click()
                    await random_delay(0.5, 1.0)
                    return
        # If only Discard is visible, look for the dismiss/X button
        close_btn = await page.query_selector(
            "button[aria-label='Dismiss'], button[aria-label='Close']"
        )
        if close_btn and await close_btn.is_visible():
            await close_btn.click()
            await random_delay(0.5, 1.0)
    except Exception:
        pass


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _find_easy_apply_button(page: Page):
    """
    Find the Easy Apply button — ONLY matches buttons whose text/aria-label
    explicitly contains 'Easy Apply'.  Regular 'Apply' buttons are ignored.
    """
    # Selectors that are likely to match Easy Apply (we validate text below)
    selectors = [
        "button[aria-label*='Easy Apply']",
        "a[aria-label*='Easy Apply']",
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
        "div[role='button']:has-text('Easy Apply')",
        ".jobs-apply-button--top-card button",
        ".jobs-apply-button--top-card a",
        "button.jobs-apply-button",
        ".jobs-s-apply button",
        ".jobs-s-apply a",
    ]
    for sel in selectors:
        try:
            btns = await page.query_selector_all(sel)
            for btn in btns:
                if not await btn.is_visible():
                    continue
                btn_text = (await btn.inner_text()).strip().lower()
                aria_label = (await btn.get_attribute("aria-label") or "").lower()
                combined_text = btn_text + " " + aria_label
                # STRICT: must contain "easy apply" — never match plain "apply"
                if "easy apply" in combined_text:
                    await btn.scroll_into_view_if_needed()
                    return btn
        except Exception:
            continue
    return None


async def _find_easy_apply_modal(page: Page):
    """
    Find the Easy Apply modal specifically — not just any [role='dialog'].
    Checks for LinkedIn's specific Easy Apply modal classes or for a dialog
    that contains Easy Apply content (form fields, Next/Submit buttons).
    """
    # Try LinkedIn-specific selectors first (most reliable)
    for sel in [".jobs-easy-apply-modal", ".jobs-easy-apply-content"]:
        modal = await page.query_selector(sel)
        if modal and await modal.is_visible():
            return modal

    # Try artdeco-modal but verify it has Easy Apply content inside
    modal = await page.query_selector(".artdeco-modal")
    if modal and await modal.is_visible():
        # Verify it's an Easy Apply modal, not a generic dialog
        has_form = await modal.query_selector(
            "input, select, textarea, button:has-text('Next'), "
            "button:has-text('Submit'), button:has-text('Review')"
        )
        if has_form:
            return modal

    # Last resort: [role='dialog'] with form elements inside
    dialog = await page.query_selector("[role='dialog']")
    if dialog and await dialog.is_visible():
        has_apply_content = await dialog.query_selector(
            "button:has-text('Next'), button:has-text('Submit'), "
            "input[type='file'], .jobs-easy-apply"
        )
        if has_apply_content:
            return dialog

    return None


async def _handle_resume_upload(page: Page, resume_path: str, cover_letter_path: str = "") -> bool:
    """Upload resume (and optionally cover letter) if file inputs are visible.
    Returns True if at least one file was uploaded."""
    if not resume_path or not os.path.exists(resume_path):
        return False

    uploaded_any = False
    try:
        upload_inputs = await page.query_selector_all("input[type='file']")
        for i, upload_input in enumerate(upload_inputs):
            try:
                if i == 0:
                    # First file input → resume
                    await upload_input.set_input_files(resume_path)
                    await random_delay(0.5, 1.0)
                    print(f"[LinkedIn] Uploaded resume: {resume_path}")
                    uploaded_any = True
                elif cover_letter_path and os.path.exists(cover_letter_path):
                    # Additional file inputs → cover letter / portfolio
                    label = await _get_field_label(page, upload_input)
                    label_lower = label.lower() if label else ""
                    if any(kw in label_lower for kw in ("cover", "letter", "additional", "document", "portfolio", "supporting")):
                        await upload_input.set_input_files(cover_letter_path)
                        await random_delay(0.5, 1.0)
                        print(f"[LinkedIn] Uploaded additional document: {cover_letter_path}")
                        uploaded_any = True
            except Exception as exc:
                print(f"[LinkedIn] File upload warning (input #{i}): {exc}")
    except Exception as exc:
        print(f"[LinkedIn] Resume upload warning: {exc}")
    return uploaded_any


async def _fill_form_fields(page: Page, resume_text: str, llm_answer_fn, job_context: str = "") -> int:
    """
    Fill all form fields scoped inside the Easy Apply modal.
    Handles: text inputs, native selects, LinkedIn custom dropdowns (typeahead),
    phone country code selectors, radio fieldsets, and consent checkboxes.

    Returns the number of fields successfully filled (0 = nothing to fill or
    all were pre-filled).
    """
    filled_count = 0

    # Scope all queries to the Easy Apply modal specifically
    modal = await _find_easy_apply_modal(page)
    scope = modal or page

    # ── 0. Phone country code selector (do this FIRST) ────────────────
    cc_filled = await _handle_phone_country_code(page, scope)
    if cc_filled:
        filled_count += 1

    # ── 1. Text / number / tel / url / email / textarea ───────────────
    inputs = await scope.query_selector_all(
        "input[type='text'], input[type='number'], input[type='tel'], "
        "input[type='url'], input[type='email'], input[type='date'], textarea"
    )
    for inp in inputs:
        try:
            if not await inp.is_visible():
                continue
            label_text = await _get_field_label(page, inp)
            if not label_text:
                continue

            # Check if the field has a validation error (red border / error
            # message).  If it does, clear it so we can re-fill with a
            # better answer — even if the field is non-empty.
            has_error = await _field_has_error(page, inp)

            existing = await inp.input_value()
            if existing.strip() and not has_error:
                continue  # Already filled and no error — skip

            # If retrying an errored field, invalidate the cached answer
            if has_error and existing.strip():
                print(f"[LinkedIn] Field '{label_text}' has validation error with value '{existing[:30]}' — clearing for retry.")
                await inp.click()
                await inp.fill("")  # Clear the bad value
                await random_delay(0.1, 0.3)
                # Remove the bad answer from form memory so LLM re-generates
                from memory.form_memory import _normalize, _load, _save
                data = _load()
                norm = _normalize(label_text)
                if norm in data:
                    del data[norm]
                    _save(data)

            # Detect field type for answer sanitization
            field_type = await _detect_field_type(label_text, inp)

            answer = await _resolve_answer(label_text, resume_text, llm_answer_fn, job_context)
            if answer:
                # Sanitize based on field type
                answer = _sanitize_answer(answer, field_type)
                try:
                    maxlen = await inp.get_attribute("maxlength")
                    cap = int(maxlen) if maxlen and str(maxlen).isdigit() else 500
                except Exception:
                    cap = 500
                # Use fast fill for known answers, human_fill only for LLM-generated
                capped = answer[:cap]
                await inp.click()
                await inp.fill("")
                # Use type to trigger dropdowns reliably
                await inp.type(capped, delay=random.randint(10, 30))
                await random_delay(0.4, 0.7)

                # Check if a dropdown list appeared (LinkedIn sometimes uses standard text inputs for typeaheads)
                try:
                    listbox_opts = await scope.query_selector_all(
                        "[role='option'], [role='listbox'] li, "
                        ".basic-typeahead__selectable, "
                        "[class*='typeahead'] li, "
                        "[id*='typeahead'] li"
                    )
                    for opt in listbox_opts:
                        if await opt.is_visible():
                            await opt.click()
                            print(f"[LinkedIn] Picked dropdown option for text field '{label_text}'")
                            await random_delay(0.2, 0.5)
                            break
                except Exception:
                    pass

                filled_count += 1
                print(f"[LinkedIn] Filled '{label_text}' -> '{capped[:50]}' (type={field_type})")
                await random_delay(0.2, 0.5)
        except Exception as exc:
            print(f"[LinkedIn] Text field warning: {exc}")

    # ── 2. Native <select> dropdowns ──────────────────────────────────
    for sel_el in await scope.query_selector_all("select"):
        try:
            if not await sel_el.is_visible():
                continue
            # Check if already has a non-default value
            cur_val = await sel_el.input_value()
            if cur_val and cur_val.strip():
                # Check it's not the placeholder
                sel_text = ""
                try:
                    selected_opt = await sel_el.query_selector("option:checked")
                    if selected_opt:
                        sel_text = (await selected_opt.inner_text()).strip().lower()
                except Exception:
                    pass
                if sel_text and sel_text not in ("select", "-- select --", "choose", "select an option", ""):
                    continue

            label_text = await _get_field_label(page, sel_el)
            if not label_text:
                continue

            # Collect available options
            options = await sel_el.query_selector_all("option")
            opts = []
            for o in options:
                t = (await o.inner_text()).strip()
                if t and t.lower() not in ("select", "-- select --", "choose", "select an option", ""):
                    opts.append(t)

            answer = get_answer(label_text) or _settings_sync_answer(label_text)
            if answer:
                # Try exact match first, then partial
                for opt in opts:
                    if answer.lower() == opt.lower() or answer.lower() in opt.lower():
                        try:
                            await sel_el.select_option(label=opt)
                            filled_count += 1
                            print(f"[LinkedIn] Selected '{label_text}' -> '{opt}'")
                            await random_delay(0.4, 0.9)
                            break
                        except Exception:
                            pass
                else:
                    try:
                        await sel_el.select_option(label=answer)
                        filled_count += 1
                        await random_delay(0.4, 0.9)
                    except Exception:
                        pass
                continue

            # LLM fallback
            if opts:
                ans = await llm_answer_fn(
                    f"{label_text} (choose one: {', '.join(opts[:20])})", resume_text, job_context
                )
                if ans:
                    best = _best_option_match(ans, opts)
                    if best:
                        try:
                            await sel_el.select_option(label=best)
                            save_answer(label_text, best)
                            filled_count += 1
                            print(f"[LinkedIn] Selected '{label_text}' -> '{best}'")
                            await random_delay(0.3, 0.7)
                        except Exception:
                            pass
        except Exception:
            pass

    # ── 3. LinkedIn custom dropdowns (typeahead / autocomplete) ───────
    #    These are NOT native <select>. They are <input> fields that open
    #    a listbox when clicked/typed into. LinkedIn uses these for:
    #    - City/Location selection
    #    - Degree type
    #    - School / University
    typeahead_selectors = [
        "input[role='combobox']",
        "input[aria-autocomplete='list']",
        "input[data-test-text-entity-list-filter-input]",
    ]
    for ta_sel in typeahead_selectors:
        try:
            typeaheads = await scope.query_selector_all(ta_sel)
            for ta in typeaheads:
                try:
                    if not await ta.is_visible():
                        continue
                    existing = await ta.input_value()
                    if existing.strip():
                        continue
                    label_text = await _get_field_label(page, ta)
                    if not label_text:
                        continue
                    answer = await _resolve_answer(label_text, resume_text, llm_answer_fn, job_context)
                    if not answer:
                        continue
                    # Type the answer to trigger the dropdown
                    await ta.click()
                    await random_delay(0.3, 0.6)
                    await ta.fill("")
                    await ta.type(answer[:40], delay=random.randint(30, 70))
                    await random_delay(0.5, 1.0)
                    # Pick the first matching option from the listbox
                    listbox_opts = await scope.query_selector_all(
                        "[role='option'], [role='listbox'] li, "
                        ".basic-typeahead__selectable, "
                        "[class*='typeahead'] li, "
                        "[id*='typeahead'] li"
                    )
                    if listbox_opts:
                        await listbox_opts[0].click()
                        filled_count += 1
                        print(f"[LinkedIn] Typeahead '{label_text}' -> '{answer[:40]}'")
                        save_answer(label_text, answer)
                        await random_delay(0.5, 1.0)
                    else:
                        # No dropdown appeared — press Enter to accept typed value
                        await page.keyboard.press("Enter")
                        filled_count += 1
                        await random_delay(0.3, 0.6)
                except Exception:
                    pass
        except Exception:
            pass

    # ── 4. Fieldset radios ────────────────────────────────────────────
    for fs in await scope.query_selector_all("fieldset"):
        try:
            legend = await fs.query_selector("legend")
            label_text = (await legend.inner_text()).strip() if legend else ""
            if not label_text:
                # Try finding a span or label inside fieldset
                span = await fs.query_selector("span, label, .fb-dash-form-element__label")
                if span:
                    label_text = (await span.inner_text()).strip()

            radios = await fs.query_selector_all("input[type='radio']")
            if not radios or any([await r.is_checked() for r in radios]):
                continue

            # Collect radio option labels
            radio_labels = []
            for r in radios:
                rl = await _get_field_label(page, r)
                radio_labels.append(rl)

            saved = get_answer(label_text) or _settings_sync_answer(label_text)
            clicked = False

            if saved:
                for i, r in enumerate(radios):
                    rl = radio_labels[i] if i < len(radio_labels) else ""
                    if rl and saved.lower() in rl.lower():
                        await r.click()
                        filled_count += 1
                        print(f"[LinkedIn] Radio '{label_text}' -> '{rl}'")
                        await random_delay(0.3, 0.7)
                        clicked = True
                        break

            if not clicked and label_text and radio_labels:
                # LLM: ask which option to pick
                opts_str = ", ".join(rl for rl in radio_labels if rl)
                if opts_str:
                    ans = await llm_answer_fn(
                        f"{label_text} (choose one: {opts_str})", resume_text, job_context
                    )
                    if ans:
                        best = _best_option_match(ans, radio_labels)
                        if best:
                            idx = radio_labels.index(best)
                            await radios[idx].click()
                            save_answer(label_text, best)
                            filled_count += 1
                            print(f"[LinkedIn] Radio '{label_text}' -> '{best}'")
                            await random_delay(0.3, 0.7)
                            clicked = True

            if not clicked:
                # Default: pick "Yes" if available, else first option
                for i, r in enumerate(radios):
                    rl = (radio_labels[i] if i < len(radio_labels) else "").lower()
                    if "yes" in rl:
                        await r.click()
                        filled_count += 1
                        await random_delay(0.3, 0.7)
                        clicked = True
                        break
                if not clicked and radios:
                    await radios[0].click()
                    filled_count += 1
                    await random_delay(0.3, 0.7)
        except Exception:
            pass

    # ── 5. Standalone checkboxes (terms / consent / follow / others) ──
    for cb in await scope.query_selector_all("input[type='checkbox']:not([disabled])"):
        try:
            if not await cb.is_visible():
                continue
            label_text = await _get_field_label(page, cb)
            if not label_text:
                continue
            label = label_text.lower()
            
            saved = get_answer(label_text)
            if not saved:
                if any(kw in label for kw in (
                    "agree", "consent", "terms", "privacy", "authoriz",
                    "confirm", "acknowledge", "follow",
                )):
                    saved = "Yes"
                else:
                    ans = await llm_answer_fn(
                        f"Checkbox question: '{label_text}'. Should I check it? (Reply 'Yes' or 'No' only)", resume_text, job_context
                    )
                    saved = "Yes" if (ans and "yes" in ans.lower() and "no" not in ans.lower()) else "No"
                save_answer(label_text, saved)

            is_checked = await cb.is_checked()
            should_be_checked = saved and "yes" in saved.lower()

            if should_be_checked and not is_checked:
                await cb.check()
                filled_count += 1
                await random_delay(0.2, 0.5)
            elif not should_be_checked and is_checked:
                await cb.uncheck()
                filled_count += 1
                await random_delay(0.2, 0.5)
        except Exception:
            pass

    return filled_count


# ── Phone country code handler (Component 3) ─────────────────────────────────

async def _handle_phone_country_code(page: Page, scope) -> bool:
    """
    Select the correct phone country code in LinkedIn's phone field.
    LinkedIn uses a native <select> for the country code.
    Returns True if a selection was made.
    """
    from config import settings as _settings

    target_code = _settings.phone_country_code  # e.g. "India (+91)"
    if not target_code:
        return False

    try:
        # LinkedIn's phone country code selector
        cc_selectors = [
            "select[id*='phoneCountryCode']",
            "select[name*='phoneCountryCode']",
            "select[id*='country-code']",
            "select[aria-label*='country code']",
            "select[aria-label*='Country code']",
            "select[data-test-text-selectable-option]",
        ]
        for sel in cc_selectors:
            cc_select = await scope.query_selector(sel)
            if cc_select and await cc_select.is_visible():
                # Check if already set correctly
                cur = await cc_select.input_value()
                if cur:
                    try:
                        selected_opt = await cc_select.query_selector("option:checked")
                        if selected_opt:
                            cur_text = (await selected_opt.inner_text()).strip()
                            if target_code.lower() in cur_text.lower():
                                return False  # Already correct
                    except Exception:
                        pass

                # Try selecting by label text
                try:
                    await cc_select.select_option(label=target_code)
                    await random_delay(0.3, 0.5)
                    print(f"[LinkedIn] Phone country code -> '{target_code}'")
                    return True
                except Exception:
                    pass

                # Fallback: try partial match on option labels
                options = await cc_select.query_selector_all("option")
                for opt in options:
                    opt_text = (await opt.inner_text()).strip()
                    if target_code.lower() in opt_text.lower():
                        opt_val = await opt.get_attribute("value")
                        if opt_val:
                            await cc_select.select_option(value=opt_val)
                            await random_delay(0.3, 0.5)
                            print(f"[LinkedIn] Phone country code -> '{opt_text}'")
                            return True
    except Exception as exc:
        print(f"[LinkedIn] Phone country code warning: {exc}")
    return False


# ── Field type detection (Component 2) ────────────────────────────────────────

import re as _re
from datetime import date as _date


async def _detect_field_type(label: str, element) -> str:
    """
    Determine the expected answer format based on the HTML input type
    and the label text.
    Returns: 'numeric', 'date', 'url', 'email', 'phone', or 'text'.
    """
    try:
        input_type = (await element.get_attribute("type") or "").lower()
    except Exception:
        input_type = ""

    if input_type == "number":
        return "numeric"
    if input_type == "date":
        return "date"
    if input_type == "url":
        return "url"
    if input_type == "email":
        return "email"
    if input_type == "tel":
        return "phone"

    # Label-based detection for text inputs
    label_lower = label.lower()
    if any(kw in label_lower for kw in (
        "how many years", "years of experience", "years of work",
        "number of", "how many", "total experience",
    )):
        return "numeric"
    if any(kw in label_lower for kw in ("url", "link", "website")):
        return "url"

    return "text"


def _sanitize_answer(answer: str, field_type: str) -> str:
    """
    Post-process an answer based on the detected field type.
    Ensures numeric fields get pure numbers, dates get YYYY-MM-DD, etc.
    """
    if not answer:
        return answer

    if field_type == "numeric":
        # Extract the first number from the answer
        nums = _re.findall(r"\d+(?:\.\d+)?", answer)
        if nums:
            return nums[0]
        # Word-number fallback
        word_map = {
            "zero": "0", "one": "1", "two": "2", "three": "3",
            "four": "4", "five": "5", "six": "6", "seven": "7",
            "eight": "8", "nine": "9", "ten": "10",
            "none": "0", "nil": "0", "no": "0",
        }
        for word, digit in word_map.items():
            if word in answer.lower().split():
                return digit
        return "0"  # safe default for numeric fields

    elif field_type == "date":
        # If already in YYYY-MM-DD format, return as-is
        if _re.match(r"\d{4}-\d{2}-\d{2}$", answer.strip()):
            return answer.strip()
        # Convert common phrases to today's date
        if any(kw in answer.lower() for kw in ("immediate", "now", "asap", "today")):
            return _date.today().isoformat()
        # Try to extract a date-like pattern
        m = _re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", answer)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        # Fallback: return today's date
        return _date.today().isoformat()

    elif field_type == "url":
        answer = answer.strip()
        if answer.lower() in ("n/a", "na", "none", "nil", "-", ""):
            return ""
        if not answer.startswith(("http://", "https://")):
            answer = "https://" + answer
        return answer

    elif field_type == "phone":
        # Strip non-digit/plus characters
        clean = _re.sub(r"[^\d+]", "", answer)
        if clean and not clean.startswith("+"):
            clean = "+91" + clean  # Default to India
        return clean or answer

    return answer



def _best_option_match(answer: str, options: list[str]) -> str | None:
    """Find the best matching option for an LLM answer (case-insensitive)."""
    answer_lower = answer.strip().lower()
    # Exact match
    for opt in options:
        if opt.lower() == answer_lower:
            return opt
    # Substring match
    for opt in options:
        if answer_lower in opt.lower() or opt.lower() in answer_lower:
            return opt
    # Fuzzy: first word match
    first_word = answer_lower.split()[0] if answer_lower else ""
    if first_word and len(first_word) > 2:
        for opt in options:
            if first_word in opt.lower():
                return opt
    return None


def _settings_sync_answer(label: str) -> Optional[str]:
    """Synchronous settings map lookup."""
    from config import settings
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
        # ── Stipend / compensation (common intern questions) ──────────
        "stipend": settings.expected_ctc, "stipend expectation": settings.expected_ctc,
        "expected stipend": settings.expected_ctc, "salary expectation": settings.expected_ctc,
        "compensation": settings.expected_ctc,
        # ── Experience ────────────────────────────────────────────────
        "total experience": settings.total_experience_years,
        "years of experience": settings.total_experience_years,
        "experience": settings.total_experience_years,
        "work experience": settings.total_experience_years,
        "how many years": settings.total_experience_years,
        # ── Start date / availability (common intern questions) ──────
        "start date": "Immediately",
        "earliest date": "Immediately",
        "date you can start": "Immediately",
        "when can you start": "Immediately",
        "when can you join": "Immediately",
        "joining date": "Immediately",
        "available from": "Immediately",
        "availability": "Immediately",
        "earliest start": "Immediately",
        # ── Identity ──────────────────────────────────────────────────
        "full name": full_name, "name": full_name,
        "first name": settings.first_name, "last name": settings.last_name,
        "surname": settings.last_name,
        "email": email, "email address": email,
        "email id": email,
        "linkedin": settings.linkedin_url, "linkedin url": settings.linkedin_url,
        "linkedin profile": settings.linkedin_url,
        "github": settings.github_url, "github url": settings.github_url,
        "portfolio": settings.portfolio_url, "website": settings.portfolio_url,
        "college": settings.college, "university": settings.college,
        "school": settings.college, "institution": settings.college,
        "degree": settings.degree, "qualification": settings.degree,
        "graduation year": settings.graduation_year,
        "year of graduation": settings.graduation_year,
        "current company": settings.current_company,
        "current role": settings.current_role,
        "job title": settings.current_role, "designation": settings.current_role,
        "work authorization": settings.work_authorization,
        "authorized to work": settings.work_authorization,
        "eligible to work": settings.work_authorization,
        "legally authorized": settings.work_authorization,
        "visa sponsorship": "No", "require sponsorship": "No",
        "do you require": "No", "will you require": "No",
        "gender": settings.gender, "nationality": settings.nationality,
        "willing to relocate": "Yes", "open to relocation": "Yes",
        "immediate joiner": "Yes" if settings.notice_period in ("0", "immediate", "Immediate") else "No",
    }
    label_lower = label.lower()
    for key, value in smap.items():
        if key in label_lower and value:
            return value
    return None


async def _resolve_answer(
    label: str, resume_text: str, llm_answer_fn, job_context: str = ""
) -> Optional[str]:
    """Settings map → memory cache → LLM.  No stdin prompts."""
    # 1. Settings map (instant, no API call)
    answer = _settings_sync_answer(label)
    if answer:
        print(f"[Settings] '{label}' -> '{answer}'")
        save_answer(label, answer)
        return answer

    # 2. Memory cache (fuzzy match)
    saved = get_answer(label)
    if saved:
        print(f"[FormMemory] '{label}' -> '{saved}'")
        return saved

    # 3. LLM (fully automatic — no stdin blocking)
    print(f"[LLM] Dynamic Q: '{label}'")
    try:
        answer = await llm_answer_fn(label, resume_text, job_context)
    except Exception as exc:
        print(f"[LLM] Failed for '{label}': {exc}")
        return None
    if answer:
        answer = answer.strip()[:300]
        # Don't save "-" or blank LLM responses
        if answer and answer != "-":
            save_answer(label, answer)
            return answer
    return None


async def _get_field_label(page: Page, element) -> str:
    """
    Extract the label for a form field.  Priority order:
      1. aria-label attribute
      2. aria-labelledby → referenced element text
      3. <label for="id"> lookup
      4. Parent-walking: find enclosing <label> or label-like sibling via JS
      5. placeholder attribute
      6. name attribute (humanised)
    """
    try:
        aria = await element.get_attribute("aria-label")
        if aria and aria.strip():
            return aria.strip()

        # aria-labelledby
        labelledby = await element.get_attribute("aria-labelledby")
        if labelledby:
            parts = []
            for ref_id in labelledby.split():
                ref = await page.query_selector(f"#{ref_id}")
                if ref:
                    t = (await ref.inner_text()).strip()
                    if t:
                        parts.append(t)
            if parts:
                return " ".join(parts)

        # <label for="id">
        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()

        # Walk up DOM to find wrapping label or label-like sibling
        parent_label = await page.evaluate(
            """(el) => {
                let node = el.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (!node) break;
                    if (node.tagName === 'LABEL') return node.innerText.trim();
                    // Check for LinkedIn's label patterns
                    const labelChild = node.querySelector(
                        'label, .fb-dash-form-element__label, ' +
                        '[data-test-form-element-label], ' +
                        '.artdeco-text-input--label, ' +
                        'span.t-14, span.t-bold'
                    );
                    if (labelChild) {
                        const t = labelChild.innerText.trim();
                        if (t.length > 1 && t.length < 120) return t;
                    }
                    // Check preceding siblings
                    for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
                        const tag = sib.tagName;
                        const cls = (sib.className || '').toLowerCase();
                        if (tag === 'LABEL' || cls.includes('label') || cls.includes('legend')) {
                            const t = sib.innerText.trim();
                            if (t.length > 1 && t.length < 120) return t;
                        }
                    }
                    node = node.parentElement;
                }
                return '';
            }""",
            element,
        )
        if parent_label and parent_label.strip():
            return parent_label.strip()

        placeholder = await element.get_attribute("placeholder")
        if placeholder and placeholder.strip():
            return placeholder.strip()

        name = await element.get_attribute("name")
        if name:
            return name.replace("-", " ").replace("_", " ").strip()
    except Exception:
        pass
    return ""


async def _scroll_modal_to_bottom(page: Page) -> None:
    """
    Scroll the Easy Apply modal's scrollable content area to the bottom
    so that the Submit/Review/Next button becomes visible in the viewport.
    LinkedIn's modal has a scrollable inner container — we need to scroll
    THAT element, not the page itself.
    """
    try:
        scrolled = await page.evaluate("""
            () => {
                // Try LinkedIn-specific scrollable containers first
                const selectors = [
                    '.jobs-easy-apply-modal .jobs-easy-apply-content',
                    '.jobs-easy-apply-modal .artdeco-modal__content',
                    '.jobs-easy-apply-modal [class*="body"]',
                    '.artdeco-modal .artdeco-modal__content',
                    '.artdeco-modal [class*="body"]',
                    '[role="dialog"] [class*="content"]',
                    '[role="dialog"] [class*="body"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.scrollHeight > el.clientHeight + 10) {
                        el.scrollTop = el.scrollHeight;
                        return true;
                    }
                }
                // Fallback: scroll any scrollable child inside the modal
                const modal = document.querySelector(
                    '.jobs-easy-apply-modal, .artdeco-modal, [role="dialog"]'
                );
                if (modal) {
                    const children = modal.querySelectorAll('*');
                    for (const child of children) {
                        if (child.scrollHeight > child.clientHeight + 50) {
                            child.scrollTop = child.scrollHeight;
                            return true;
                        }
                    }
                }
                return false;
            }
        """)
        if scrolled:
            print("[LinkedIn] Scrolled modal content to bottom.")
    except Exception as exc:
        print(f"[LinkedIn] Modal scroll warning: {exc}")


async def _get_next_action(page: Page, step: int = 0) -> str:
    """
    Determine next action from visible buttons INSIDE the Easy Apply modal.
    Scoped to modal only — prevents background page 'Apply' buttons from
    being misdetected as submit actions.

    Scrolls the modal content to reveal buttons that may be below the fold.
    """
    button_map = {
        "submit application": "submit",
        "submit": "submit",
        "review": "review",
        "review your application": "review",
        "next": "next",
        "continue": "next",
        "done": "done",
    }

    # Scope button search to the Easy Apply modal specifically
    modal = await _find_easy_apply_modal(page)
    if not modal:
        # Modal is gone — only trust "done" if we progressed past step 0
        return "done"

    try:
        buttons = await modal.query_selector_all(
            "button, a.artdeco-button, div[role='button']"
        )
    except Exception:
        return "unknown"

    # First pass: check already-visible buttons
    for btn in buttons:
        try:
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if not txt:
                txt = (await btn.get_attribute("aria-label") or "").strip().lower()

            for key, action in button_map.items():
                if key == txt or key in txt:
                    return action
        except Exception:
            continue

    # Second pass: scroll modal to bottom and re-check — the Submit button
    # is often below the fold on the final review page
    await _scroll_modal_to_bottom(page)
    await asyncio.sleep(0.3)

    try:
        buttons = await modal.query_selector_all(
            "button, a.artdeco-button, div[role='button']"
        )
    except Exception:
        return "unknown"

    for btn in buttons:
        try:
            # scroll_into_view_if_needed ensures the button is in the viewport
            try:
                await btn.scroll_into_view_if_needed()
            except Exception:
                pass
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if not txt:
                txt = (await btn.get_attribute("aria-label") or "").strip().lower()

            for key, action in button_map.items():
                if key == txt or key in txt:
                    return action
        except Exception:
            continue

    return "unknown"


async def _click_button_by_text(page: Page, texts: list[str]) -> None:
    """Click a button identified by its visible text, scoped to Easy Apply modal.
    Scrolls the button into view before clicking to handle off-screen buttons."""
    # Scope to modal to avoid clicking background buttons
    scope = await _find_easy_apply_modal(page) or page

    for text in texts:
        try:
            selectors = [
                f"button:has-text('{text}')",
                f"a:has-text('{text}')",
                f"div[role='button']:has-text('{text}')",
                f".artdeco-button:has-text('{text}')"
            ]
            for sel in selectors:
                btn = await scope.query_selector(sel)
                if btn:
                    # Scroll the button into view first — it may be below the
                    # fold inside the modal's scrollable content area
                    try:
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
                    if await btn.is_visible():
                        await human_click_element(btn, page)
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

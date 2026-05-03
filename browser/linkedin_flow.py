"""
linkedin_flow.py
────────────────
LinkedIn login + Easy Apply handler.
Uses PopupHandler to continuously sweep for popups/overlays during the
entire apply flow, not just at the start.

Fixes applied:
  - When no Easy Apply button is found, detect the external Apply button,
    capture the new popup tab it opens, and run the full external ATS flow
    on that tab instead of silently skipping the job.
  - _find_easy_apply_button() now validates button text contains "easy apply"
    so a regular "Apply" button is never confused for Easy Apply.
  - Login now waits in a polling loop for up to 5 min, allowing manual
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
) -> bool:
    """
    Full LinkedIn Easy Apply flow with continuous popup suppression.

    If the job has no Easy Apply button (external apply), this function
    automatically detects the external Apply button, captures the new
    popup tab it opens, and runs the external ATS flow on that tab.

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

        await human_click_element(easy_apply_btn, page)
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
    from browser.external_flow import apply_external_link  # noqa: PLC0415

    # Selectors for the non-Easy-Apply "Apply" button on LinkedIn job pages
    external_btn_selectors = [
        "button.jobs-apply-button",
        "a.jobs-apply-button",
        ".jobs-apply-button--top-card",
        ".jobs-s-apply button",
        ".jobs-s-apply a",
        "button[aria-label*='Apply']",
        "a[aria-label*='Apply']",
        "a[href*='externalApply']",
        "a[href*='/jobs/view/externalApply']",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
    ]

    apply_btn = None
    for sel in external_btn_selectors:
        try:
            btns = await page.query_selector_all(sel)
            for btn in btns:
                if await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip().lower()
                    aria_label = (await btn.get_attribute("aria-label") or "").lower()
                    combined_text = btn_text + " " + aria_label
                    if "easy apply" not in combined_text and "apply" in combined_text:
                        apply_btn = btn
                        break
            if apply_btn:
                break
        except Exception:
            continue

    if apply_btn is None:
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await random_delay(0.8, 1.4)
            handle = await page.evaluate_handle(
                """() => {
                    const candidates = [...document.querySelectorAll('button, a')];
                    return candidates.find((el) => {
                        const text = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
                        const rect = el.getBoundingClientRect();
                        return text.includes('apply') &&
                            !text.includes('easy apply') &&
                            rect.width > 0 &&
                            rect.height > 0;
                    }) || null;
                }"""
            )
            apply_btn = handle.as_element() if handle else None
        except Exception:
            apply_btn = None

    if apply_btn is None:
        print("[LinkedIn] No external Apply button found — skipping job.")
        return False

    print("[LinkedIn] Clicking external Apply button — waiting for new tab...")

    # Capture the URL now so the fallback block doesn't need to re-click
    pre_click_url = page.url

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

        # ── Fallback: check if clicking caused a same-tab redirect ──
        # The button was ALREADY clicked inside expect_page above.
        # Do NOT click again — just check if the URL changed.
        try:
            await asyncio.sleep(3)  # give the redirect time to settle
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


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _find_easy_apply_button(page: Page):
    """
    Find the Easy Apply button using a prioritized list of selectors.
    Broadens search to <a> tags and role="button" elements.
    """
    selectors = [
        "button.jobs-apply-button",
        "button[aria-label*='Easy Apply']",
        "a[aria-label*='Easy Apply']",
        ".jobs-apply-button--top-card button",
        ".jobs-apply-button--top-card a",
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
        ".jobs-s-apply button",
        ".jobs-s-apply a",
        "div[role='button']:has-text('Easy Apply')",
    ]
    for sel in selectors:
        try:
            btns = await page.query_selector_all(sel)
            for btn in btns:
                if await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip().lower()
                    aria_label = (await btn.get_attribute("aria-label") or "").lower()
                    combined_text = btn_text + " " + aria_label
                    if "easy apply" in combined_text:
                        # Scroll into view to ensure human_click_element works
                        await btn.scroll_into_view_if_needed()
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
            print(f"[LinkedIn] Uploaded resume: {resume_path}")
    except Exception as exc:
        print(f"[LinkedIn] Resume upload warning: {exc}")


async def _fill_form_fields(page: Page, resume_text: str, llm_answer_fn) -> None:
    """
    Fill all form fields scoped inside the Easy Apply modal.
    The modal is used as the container so nothing outside it is touched.
    """
    # Scope all queries to the Easy Apply modal
    modal = await page.query_selector(
        ".jobs-easy-apply-modal, .artdeco-modal, [role='dialog']"
    )
    scope = modal or page

    # --- Text / number / tel / url / email / textarea ---
    inputs = await scope.query_selector_all(
        "input[type='text'], input[type='number'], input[type='tel'], "
        "input[type='url'], input[type='email'], textarea"
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
            print(f"[LinkedIn] Text field warning: {exc}")

    # --- Select dropdowns ---
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
            # LLM fallback with options
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

    # --- Fieldset radios ---
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
                        await random_delay(0.3, 0.7)
                        clicked = True
                        break
            if not clicked:
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

    # --- Standalone checkboxes (terms / consent) ---
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
    """Synchronous settings map lookup (mirrors external_flow logic)."""
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


async def _resolve_answer(
    label: str, resume_text: str, llm_answer_fn
) -> Optional[str]:
    """Settings map → memory cache → stdin (sensitive only) → LLM."""
    # 1. Settings map (instant, no API call)
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
            print(f"\n[LinkedIn][SENSITIVE FIELD] '{label}' — enter your answer:")
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
    button_map = {
        "submit application": "submit",
        "submit": "submit",
        "review": "review",
        "review your application": "review",
        "next": "next",
        "continue": "next",
        "done": "done",
    }
    # Look for any clickable element that matches our action keywords
    buttons = await page.query_selector_all(
        "button, a.artdeco-button, div[role='button']"
    )
    for btn in buttons:
        try:
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if not txt:
                # Fallback to aria-label
                txt = (await btn.get_attribute("aria-label") or "").strip().lower()

            for key, action in button_map.items():
                if key == txt or key in txt:
                    return action
        except Exception:
            continue

    # If modal has closed, we're done
    modal = await page.query_selector(".jobs-easy-apply-modal, .artdeco-modal")
    if not modal:
        return "done"
    return "unknown"


async def _click_button_by_text(page: Page, texts: list[str]) -> None:
    """Click a button identified by its visible text (case-insensitive)."""
    for text in texts:
        try:
            # Broaden search beyond just 'button' tag
            selectors = [
                f"button:has-text('{text}')",
                f"a:has-text('{text}')",
                f"div[role='button']:has-text('{text}')",
                f".artdeco-button:has-text('{text}')"
            ]
            for sel in selectors:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
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

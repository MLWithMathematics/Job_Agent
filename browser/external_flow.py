"""
external_flow.py
────────────────
Generic external job application handler.

Architecture (v4 — container-scoped):
  1. Navigate to the URL with stealth + PopupHandler.
  2. Identify the APPLICATION FORM CONTAINER — a specific <form> or apply-div
     that wraps the actual job application, not the whole page.  All field
     scanning is scoped inside this container.  This prevents newsletter,
     search, login, and sidebar inputs from being touched.
  3. Score form candidates so the best one wins even on multi-form pages.
  4. Upload resume inside the container.
  5. Fill fields in resolution order:
       settings map → form_memory cache → sensitive stdin → LLM fallback
  6. Click Submit / Next and confirm success-page detection.

Key invariant:
  Every query_selector_all call for form fields passes through
  _scope_query(container, selector) which queries inside the container
  element.  Nothing outside the container is ever touched.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional, Tuple

from playwright.async_api import Page

from browser.stealth import random_delay, human_fill, human_click, STEALTH_INIT_SCRIPT
from browser.popup_handler import PopupHandler, safe_goto
from memory.form_memory import get_answer, save_answer
from config import settings


# ── Success detection ─────────────────────────────────────────────────────────

SUCCESS_PATTERNS = [
    r"application.{0,20}(submitted|received|complete|success)",
    r"thank.{0,10}(you|applying)",
    r"we.{0,15}(received|got).{0,20}application",
    r"you.{0,10}(have|ve).{0,15}applied",
    r"successfully.{0,20}applied",
    r"application.{0,10}(is|has been).{0,10}(sent|submitted)",
]
_SUCCESS_RE = re.compile("|".join(SUCCESS_PATTERNS), re.IGNORECASE)

SUBMIT_BUTTON_TEXTS = [
    "Submit application", "Submit Application", "Submit",
    "Apply", "Apply now", "Apply Now",
    "Send application", "Complete application", "Finish",
]

# Fields prompted via stdin (too sensitive for LLM)
MANUAL_FIELDS = [
    "date of birth", "passport", "ssn", "social security",
    "bank account", "pan number", "aadhaar",
]

# Known ATS hostnames — entire page is the application, no scoping needed
_KNOWN_ATS_HOSTS = [
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
    "icims.com", "taleo.net", "smartrecruiters.com", "jobvite.com",
    "ashbyhq.com", "bamboohr.com", "recruitee.com", "workable.com",
    "rippling.com", "jazz.co", "breezy.hr", "pinpointhq.com",
    "teamtailor.com", "dover.com", "apply.com", "freshteam.com",
    "keka.com", "darwinbox.com", "zohorecruit.com",
    "hirist.com", "unstop.com", "internshala.com", "letsintern.com",
    "springrecruit.com", "ceipal.com", "comeet.co",
    "careers-page.com", "applytojob.com", "hire.li", "recooty.com",
    "recruitcrm.io", "hrcloud.com", "loxo.co",
]

# URL substrings that strongly indicate the page IS an application form
_APPLY_URL_SIGNALS = [
    "/apply", "/application", "apply?", "apply#",
    "/job-application", "/job_application", "jobapplication",
    "/submit", "applyfor", "applyjob",
]

# Class / id substrings on wrapper elements that indicate an apply container
_APPLY_CONTAINER_SIGNALS = [
    "apply", "application", "job-form", "jobform", "candidate",
    "chatbot", "applyform", "applybody", "apply-body",
]

# Class substrings on ancestors that mark an element as site-chrome
_JUNK_CLASS_SIGNALS = [
    "search", "navbar", "nav-", "topbar", "top-bar", "header",
    "footer", "sidebar", "side-bar", "cookie", "newsletter",
    "subscribe", "widget", "sticky", "mega-menu", "login-form",
    "signup", "sign-up", "register", "alert-bar", "toast",
    "notification", "banner", "breadcrumb", "pagination",
]

# Label / placeholder substrings that mark an input as site-chrome
_JUNK_LABEL_SIGNALS = [
    "search", "subscribe", "newsletter", "email updates", "notify me",
    "sign up", "mailing list", "alerts",
]


# ── Settings-backed personal field map ───────────────────────────────────────

def _settings_map() -> dict:
    """Return all pre-known answers keyed by normalised label substring."""
    full_name = settings.full_name or " ".join(
        part for part in (settings.first_name, settings.last_name) if part
    ).strip()
    email = settings.email or settings.linkedin_email or settings.naukri_email

    return {
        # Contact
        "phone":               settings.phone,
        "mobile":              settings.phone,
        "mobile number":       settings.phone,
        "contact number":      settings.phone,
        "phone number":        settings.phone,
        # Location
        "city":                settings.current_location,
        "location":            settings.current_location,
        "current location":    settings.current_location,
        # Work terms
        "notice period":       settings.notice_period,
        "current ctc":         settings.current_ctc,
        "current salary":      settings.current_ctc,
        "expected ctc":        settings.expected_ctc,
        "expected salary":     settings.expected_ctc,
        "total experience":    settings.total_experience_years,
        "years of experience": settings.total_experience_years,
        "experience":          settings.total_experience_years,
        # Identity
        "full name":           full_name,
        "name":                full_name,
        "first name":          settings.first_name,
        "last name":           settings.last_name,
        "surname":             settings.last_name,
        "email":               email,
        "email address":       email,
        "email id":            email,
        # Profiles
        "linkedin":            settings.linkedin_url,
        "linkedin url":        settings.linkedin_url,
        "linkedin profile":    settings.linkedin_url,
        "github":              settings.github_url,
        "github url":          settings.github_url,
        "portfolio":           settings.portfolio_url,
        "website":             settings.portfolio_url,
        "personal website":    settings.portfolio_url,
        # Education
        "college":             settings.college,
        "university":          settings.college,
        "degree":              settings.degree,
        "qualification":       settings.degree,
        "graduation year":     settings.graduation_year,
        "passing year":        settings.graduation_year,
        # Current role
        "current company":     settings.current_company,
        "company name":        settings.current_company,
        "current role":        settings.current_role,
        "current job title":   settings.current_role,
        "job title":           settings.current_role,
        "designation":         settings.current_role,
        # Eligibility
        "work authorization":  settings.work_authorization,
        "eligible to work":    settings.work_authorization,
        "visa sponsorship":    "No",
        "require sponsorship": "No",
        "gender":              settings.gender,
        "nationality":         settings.nationality,
        # Blanks (ATS rarely needs these)
        "zip":                 "",
        "postal":              "",
        "pincode":             "",
    }


# ── Scope helper ──────────────────────────────────────────────────────────────

async def _scope_query(container, selector: str) -> list:
    """
    Query within `container` (which may be a Page or an element handle).
    Returns a list of element handles.
    """
    try:
        return await container.query_selector_all(selector)
    except Exception:
        return []


async def _scope_query_one(container, selector: str):
    """Single-element version of _scope_query."""
    try:
        return await container.query_selector(selector)
    except Exception:
        return None


# ── Application container finder ──────────────────────────────────────────────

async def _score_container(container) -> int:
    """
    Score a DOM element as an application form container.
    Higher = more likely to be the actual job application form.
    """
    score = 0
    try:
        if await _scope_query_one(container, "input[type='file']"):
            score += 4   # resume upload — very strong
        if await _scope_query_one(container, "input[type='email'], input[autocomplete='email']"):
            score += 3   # email is a strong signal
        if await _scope_query_one(container, "input[type='tel'], input[name*='phone'], input[name*='mobile']"):
            score += 2
        if await _scope_query_one(container, "textarea"):
            score += 1
        if await _scope_query_one(container,
                "input[autocomplete='name'], input[name*='name' i], "
                "input[placeholder*='name' i]"):
            score += 1
        if await _scope_query_one(container,
                "button[type='submit'], input[type='submit']"):
            score += 2
        # Apply keyword in class / id of the container itself
        try:
            cls  = (await container.get_attribute("class") or "").lower()
            cid  = (await container.get_attribute("id")    or "").lower()
            if any(kw in cls + cid for kw in _APPLY_CONTAINER_SIGNALS):
                score += 2
        except Exception:
            pass
    except Exception:
        pass
    return score


async def _find_application_container(page: Page) -> Tuple[object, bool]:
    """
    Return (container_element, is_full_page).

    is_full_page=True  → entire <body> is the application (known ATS),
                         field scoping is still done but the container is body.
    is_full_page=False → a specific form/div was isolated.
    Returns (None, False) when no application context can be identified.
    """
    url = page.url.lower()

    # 1. Known ATS host → whole page is safe
    for host in _KNOWN_ATS_HOSTS:
        if host in url:
            body = await page.query_selector("body")
            return body, True

    # 2. URL strongly signals apply page
    for sig in _APPLY_URL_SIGNALS:
        if sig in url:
            body = await page.query_selector("body")
            return body, True

    # 3. Score every <form> on the page — pick highest ≥ 4
    best_form, best_score = None, 0
    forms = await page.query_selector_all("form")
    for form in forms:
        try:
            if not await form.is_visible():
                continue
        except Exception:
            continue
        s = await _score_container(form)
        if s > best_score:
            best_score = s
            best_form = form

    if best_form and best_score >= 4:
        print(f"[External] Application <form> identified (score={best_score})")
        return best_form, False

    # 4. Score known apply-container divs
    candidates = await page.query_selector_all(
        "[class*='apply'], [class*='Apply'], [class*='application'], "
        "[class*='Application'], [id*='apply'], [id*='application'], "
        "[role='dialog'], [role='form'], [class*='modal']:not([class*='cookie']), "
        "[class*='chatbot'], [class*='wizard']"
    )
    best_div, best_div_score = None, 0
    for el in candidates:
        try:
            if not await el.is_visible():
                continue
        except Exception:
            continue
        s = await _score_container(el)
        if s > best_div_score:
            best_div_score = s
            best_div = el

    if best_div and best_div_score >= 3:
        print(f"[External] Application container div identified (score={best_div_score})")
        return best_div, False

    # 5. Accept any form with a decent score (≥ 2) as a last resort
    if best_form and best_score >= 2:
        return best_form, False

    return None, False


# ── Junk-input guard ──────────────────────────────────────────────────────────

async def _is_junk_input(page: Page, element, container=None) -> bool:
    """
    Return True if this element should be skipped.

    If a container was identified, any element OUTSIDE it is automatically junk.
    Otherwise, fall back to class-name and label heuristics.
    """
    try:
        # If we have a container, skip anything outside it
        if container is not None:
            inside = await page.evaluate(
                "([el, cont]) => cont.contains(el)",
                [element, container],
            )
            if not inside:
                return True

        # Check ancestor class names for site-chrome signals
        junk_classes = "|".join(_JUNK_CLASS_SIGNALS)
        in_junk = await page.evaluate(
            f"""(el) => {{
                let node = el.parentElement;
                while (node && node !== document.body) {{
                    const cls = (node.className || '').toLowerCase();
                    const id  = (node.id  || '').toLowerCase();
                    if (/{junk_classes}/.test(cls) || /{junk_classes}/.test(id))
                        return true;
                    node = node.parentElement;
                }}
                return false;
            }}""",
            element,
        )
        if in_junk:
            return True

        # Semantic chrome tags
        for tag_sel in ["footer", "nav", "header", "[role='navigation']", "[role='banner']", "[role='search']"]:
            inside = await page.evaluate(
                """([el, sel]) => {
                    let node = el;
                    while (node && node !== document.body) {
                        if (node.matches && node.matches(sel)) return true;
                        node = node.parentElement;
                    }
                    return false;
                }""",
                [element, tag_sel],
            )
            if inside:
                return True

        # Label / placeholder / type junk signals
        input_type = ((await element.get_attribute("type")) or "").lower()
        if input_type == "search":
            return True

        label = (await _get_field_label(page, element)).lower()
        placeholder = ((await element.get_attribute("placeholder")) or "").lower()
        name_attr = ((await element.get_attribute("name")) or "").lower()
        combined = f"{label} {placeholder} {name_attr}"
        if any(sig in combined for sig in _JUNK_LABEL_SIGNALS):
            return True

    except Exception:
        pass

    return False


# ── Application-page guard ────────────────────────────────────────────────────

async def _is_application_page(page: Page) -> bool:
    """
    Return True only if we can identify an actual application context.
    Uses _find_application_container() as the source of truth.
    """
    container, _ = await _find_application_container(page)
    return container is not None


# ── Public entry point ────────────────────────────────────────────────────────

async def _try_open_application_from_listing_page(page: Page, handler: PopupHandler) -> bool:
    """
    Some external links land on a company jobs/careers listing page. Click one
    likely Apply/View Details control to reach the actual application form.
    """
    selectors = [
        "a:has-text('Apply Now')",
        "button:has-text('Apply Now')",
        "a:has-text('Apply')",
        "button:has-text('Apply')",
        "a:has-text('View Details')",
        "button:has-text('View Details')",
        "a:has-text('Job Details')",
        "button:has-text('Job Details')",
        "a:has-text('Read More')",
        "button:has-text('Read More')",
    ]
    before_url = page.url

    for sel in selectors:
        try:
            controls = await page.query_selector_all(sel)
            for control in controls:
                if not await control.is_visible():
                    continue
                text = (await control.inner_text()).strip()
                if not text:
                    continue

                print(f"[External] Opening likely application control: {text[:80]}")
                try:
                    async with page.context.expect_page(timeout=5_000) as popup_info:
                        await control.click()
                    popup = await popup_info.value
                    try:
                        await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass
                    await page.goto(popup.url, wait_until="domcontentloaded")
                    await popup.close()
                except Exception:
                    await control.click()

                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass
                await random_delay(2.0, 3.0)
                await handler.dismiss_all()

                if page.url != before_url or await _is_application_page(page):
                    return True
        except Exception:
            continue

    return False


async def apply_external_link(
    page: Page,
    apply_url: str,
    tailored_resume_path: str,
    resume_text: str,
    llm_answer_fn,
) -> bool:
    """
    Full external apply flow with continuous popup suppression.

    llm_answer_fn: async callable(question: str, resume_text: str) -> str
    Returns True on success, False on failure.
    """
    handler = PopupHandler(page)

    try:
        print(f"[External] Navigating to {apply_url}")
        await safe_goto(page, apply_url, handler=handler)
        await random_delay(2.5, 4.0)
        await handler.dismiss_all()
        await handler.start_auto_dismiss()

        # Wait for JS-heavy ATSs to settle; retry once if container not yet visible
        for attempt in range(3):
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await random_delay(1.5, 2.5)
            await handler.dismiss_all()

            container, is_full_page = await _find_application_container(page)
            if container is not None:
                break
            if attempt < 2:
                print(f"[External] Attempt {attempt+1}: container not found, waiting 3 s for JS render…")
                await asyncio.sleep(3)

        if container is None:
            opened = await _try_open_application_from_listing_page(page, handler)
            if opened:
                for attempt in range(3):
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    await random_delay(1.5, 2.5)
                    await handler.dismiss_all()
                    container, is_full_page = await _find_application_container(page)
                    if container is not None:
                        break

        if container is None:
            print(
                f"[External] No job-application form found — skipping.\n"
                f"           URL: {page.url}"
            )
            await handler.stop_auto_dismiss()
            return False

        print(f"[External] Container found (full_page={is_full_page}) — starting apply.")

        # Upload resume first — many ATSs pre-parse to populate other fields
        await _handle_resume_upload(page, tailored_resume_path, container)
        await random_delay(1.5, 2.5)

        # Multi-step form loop (up to 12 pages / steps)
        max_steps = 12
        for step in range(max_steps):
            print(f"[External] Form step {step + 1} — {page.url}")

            # Re-find the container each step (SPA navigation changes the DOM)
            container, is_full_page = await _find_application_container(page)
            if container is None:
                # Page may have changed entirely (redirect after step)
                if _is_success_page(await page.content()):
                    print("[External] Success page detected after navigation!")
                    await handler.stop_auto_dismiss()
                    return True
                print("[External] Container lost mid-flow — bailing.")
                await handler.stop_auto_dismiss()
                return False

            await _fill_form_fields(page, resume_text, llm_answer_fn, container)
            await handler.dismiss_all()

            if False and _is_success_page(await page.content()):
                print("[External] Success page detected — application submitted!")
                await handler.stop_auto_dismiss()
                return True

            action = await _get_next_action(page, container)

            if action == "submit":
                await _click_submit(page, container)
                await random_delay(2.5, 4.5)
                await handler.dismiss_all()

                if _is_success_page(await page.content()):
                    print("[External] Application submitted successfully!")
                    await handler.stop_auto_dismiss()
                    return True

                # Some ATSs show a final review page after first Submit
                container, _ = await _find_application_container(page)
                if container:
                    await _handle_resume_upload(page, tailored_resume_path, container)
                    await _fill_form_fields(page, resume_text, llm_answer_fn, container)
                    await _click_submit(page, container)
                    await random_delay(2.0, 3.5)
                    if _is_success_page(await page.content()):
                        print("[External] Application submitted (post-review page)!")
                        await handler.stop_auto_dismiss()
                        return True

                await handler.stop_auto_dismiss()
                return False

            elif action == "next":
                await _click_next(page, container)
                await random_delay(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                await handler.dismiss_all()

            else:
                print(f"[External] Unknown action at step {step + 1} — bailing.")
                await handler.stop_auto_dismiss()
                return False

        await handler.stop_auto_dismiss()
        return False

    except Exception as exc:
        print(f"[External] Exception during apply: {exc}")
        try:
            await handler.stop_auto_dismiss()
        except Exception:
            pass
        return False


# ── Form filling ──────────────────────────────────────────────────────────────

async def _handle_resume_upload(page: Page, resume_path: str, container=None) -> None:
    """Upload resume to a file input inside the container (or page-wide fallback)."""
    if not resume_path or not os.path.exists(resume_path):
        return
    scope = container or page
    try:
        inputs = await _scope_query(scope, "input[type='file']")
        if not inputs:
            # Fallback: some ATSs hide the input outside the scoped container
            inputs = await page.query_selector_all("input[type='file']")
        for inp in inputs:
            try:
                is_vis = await inp.is_visible()
                is_hid = await inp.is_hidden()
                if is_vis or not is_hid:
                    await inp.set_input_files(resume_path)
                    await random_delay(1.5, 3.0)
                    print(f"[External] Uploaded resume: {resume_path}")
                    return
            except Exception:
                continue
    except Exception as exc:
        print(f"[External] Resume upload warning: {exc}")


async def _fill_form_fields(page: Page, resume_text: str, llm_answer_fn, container=None) -> None:
    """
    Fill all form fields INSIDE the identified container.
    Nothing outside the container is ever touched.
    """
    scope = container or page

    # 1. Text / email / tel / url / number / textarea
    text_inputs = await _scope_query(
        scope,
        "input[type='text'], input[type='number'], input[type='email'], "
        "input[type='tel'], input[type='url'], textarea",
    )
    for inp in text_inputs:
        try:
            if not await inp.is_visible():
                continue
            if await _is_junk_input(page, inp, container):
                continue
            label = await _get_field_label(page, inp)
            if not label:
                continue
            existing = await inp.input_value()
            if existing.strip():
                continue
            try:
                maxlen_attr = await inp.get_attribute("maxlength")
                max_chars = int(maxlen_attr) if maxlen_attr and str(maxlen_attr).isdigit() else 500
            except Exception:
                max_chars = 500

            answer = await _resolve_answer(label, resume_text, llm_answer_fn)
            if answer:
                await human_fill(inp, answer[:max_chars])
                await random_delay(0.3, 0.8)
        except Exception as exc:
            print(f"[External] Text field warning: {exc}")

    # 2. Date inputs
    for inp in await _scope_query(scope, "input[type='date']"):
        try:
            if not await inp.is_visible():
                continue
            if (await inp.input_value()).strip():
                continue
            label = await _get_field_label(page, inp)
            saved = get_answer(label) if label else None
            if saved:
                await inp.fill(saved)
                await random_delay(0.3, 0.6)
        except Exception:
            continue

    # 3. Native <select> dropdowns
    for sel_el in await _scope_query(scope, "select"):
        try:
            if not await sel_el.is_visible():
                continue
            label = await _get_field_label(page, sel_el)
            if not label:
                continue
            saved = get_answer(label)
            if saved:
                try:
                    await sel_el.select_option(label=saved)
                    await random_delay(0.3, 0.7)
                    continue
                except Exception:
                    pass
            options = await sel_el.query_selector_all("option")
            option_texts = [
                t for t in [((await o.inner_text()).strip()) for o in options]
                if t and t not in ("Select", "-- Select --", "Choose", "")
            ]
            if option_texts:
                # Check settings map first
                answer = _settings_sync_answer(label)
                if not answer:
                    answer = await llm_answer_fn(
                        f"{label} (choose one: {', '.join(option_texts)})", resume_text
                    )
                if answer:
                    try:
                        await sel_el.select_option(label=answer)
                        save_answer(label, answer)
                        await random_delay(0.3, 0.7)
                    except Exception:
                        pass
        except Exception:
            pass

    # 4. Custom comboboxes (react-select, aria-combobox, etc.)
    await _fill_custom_dropdowns(page, resume_text, llm_answer_fn, scope, container)

    # 5. Radio fieldsets
    for fs in await _scope_query(scope, "fieldset"):
        try:
            legend = await fs.query_selector("legend")
            label = (await legend.inner_text()).strip() if legend else ""
            radios = await fs.query_selector_all("input[type='radio']")
            if not radios or any([await r.is_checked() for r in radios]):
                continue
            saved = get_answer(label) or _settings_sync_answer(label)
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

    # 6. Standalone checkboxes (terms, consent, etc.)
    for cb in await _scope_query(scope, "input[type='checkbox']:not([disabled])"):
        try:
            if not await cb.is_visible() or await cb.is_checked():
                continue
            label = (await _get_field_label(page, cb)).lower()
            if any(kw in label for kw in ("agree", "consent", "terms", "privacy", "authoriz")):
                await cb.check()
                await random_delay(0.2, 0.5)
        except Exception:
            pass

    # 7. Contenteditable divs (rich-text cover letter fields)
    for div in await _scope_query(scope, "[contenteditable='true']:not([aria-hidden='true'])"):
        try:
            if not await div.is_visible():
                continue
            if (await div.inner_text()).strip():
                continue
            label = await _get_field_label(page, div)
            if not label:
                continue
            if await _is_junk_input(page, div, container):
                continue
            answer = await _resolve_answer(label, resume_text, llm_answer_fn)
            if answer:
                await div.click()
                await random_delay(0.2, 0.4)
                await div.type(answer[:1000], delay=40)
                await random_delay(0.3, 0.7)
        except Exception:
            pass


async def _fill_custom_dropdowns(
    page: Page, resume_text: str, llm_answer_fn, scope, container
) -> None:
    """Handle aria-role='combobox', react-select, and similar custom dropdowns."""
    comboboxes = await _scope_query(
        scope,
        "[role='combobox']:not([disabled]), .react-select__control, "
        "[class*='Select__control']",
    )
    for cb in comboboxes:
        try:
            if not await cb.is_visible():
                continue
            if await _is_junk_input(page, cb, container):
                continue
            value_el = await cb.query_selector(
                "[role='option'][aria-selected='true'], "
                ".react-select__single-value, [class*='singleValue']"
            )
            if value_el and (await value_el.inner_text()).strip():
                continue
            label = await _get_field_label(page, cb)
            if not label:
                continue
            await cb.click()
            await random_delay(0.4, 0.8)
            options = await page.query_selector_all(
                "[role='option'], .react-select__option, [class*='Select__option']"
            )
            option_texts = [
                (await o.inner_text()).strip() for o in options
                if (await o.inner_text()).strip()
            ]
            if not option_texts:
                await page.keyboard.press("Escape")
                continue
            saved = get_answer(label) or _settings_sync_answer(label)
            chosen = saved or await llm_answer_fn(
                f"{label} (choose one: {', '.join(option_texts[:15])})", resume_text
            )
            if chosen:
                for opt in options:
                    t = (await opt.inner_text()).strip()
                    if chosen.lower() in t.lower():
                        await opt.click()
                        save_answer(label, t)
                        await random_delay(0.3, 0.6)
                        break
                else:
                    await page.keyboard.press("Escape")
            else:
                await page.keyboard.press("Escape")
        except Exception:
            pass


# ── Field label resolution ────────────────────────────────────────────────────

async def _get_field_label(page: Page, element) -> str:
    """
    Extract the label for a form field (priority order):
      1. aria-label attribute
      2. aria-labelledby → referenced element text
      3. <label for="id"> lookup
      4. Immediately preceding <label> sibling in DOM
      5. placeholder attribute
      6. name attribute (humanised)
    """
    try:
        aria = await element.get_attribute("aria-label")
        if aria and aria.strip():
            return aria.strip()

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

        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()

        # Try to find a preceding sibling <label> or parent label via JS
        parent_label = await page.evaluate(
            """(el) => {
                // Walk up to find a wrapping <label>
                let node = el.parentElement;
                for (let i = 0; i < 4; i++) {
                    if (!node) break;
                    if (node.tagName === 'LABEL') return node.innerText.trim();
                    // Look for a preceding sibling that is a label / div with label-like class
                    for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
                        const tag = sib.tagName;
                        const cls = (sib.className || '').toLowerCase();
                        if (tag === 'LABEL' || cls.includes('label') || cls.includes('legend')) {
                            const t = sib.innerText.trim();
                            if (t.length < 80) return t;
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


# ── Answer resolution ─────────────────────────────────────────────────────────

def _settings_sync_answer(label: str) -> Optional[str]:
    """Synchronous settings lookup (for use in non-async contexts)."""
    label_lower = label.lower()
    for key, value in _settings_map().items():
        if key in label_lower and value:
            return value
    return None


async def _resolve_answer(
    label: str, resume_text: str, llm_answer_fn
) -> Optional[str]:
    """
    Resolution order:
      1. Settings map (instant, no API call)
      2. form_memory cache (fuzzy match)
      3. stdin prompt for sensitive fields
      4. LLM fallback (capped to 300 chars)
    """
    label_lower = label.lower()

    # 1. Settings
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

    # 3. Manual stdin for sensitive fields
    manual_fields = MANUAL_FIELDS + [
        "name",
        "full name",
        "first name",
        "last name",
        "email",
        "email address",
        "email id",
    ]
    for field in manual_fields:
        if field in label_lower:
            print(f"\n[External][SENSITIVE FIELD] '{label}' — enter your answer:")
            answer = input("  >> ").strip()
            if answer:
                save_answer(label, answer)
                return answer
            return None

    # 4. LLM fallback
    print(f"[External][LLM] '{label}'")
    answer = await llm_answer_fn(label, resume_text)
    if answer:
        answer = answer.strip()[:300]
        save_answer(label, answer)
    return answer or None


# ── Navigation helpers ────────────────────────────────────────────────────────

async def _get_next_action(page: Page, container=None) -> str:
    """Return 'submit' | 'next' | 'unknown' based on visible buttons IN container."""
    scope = container or page
    buttons = await _scope_query(
        scope,
        "button[type='submit'], button[type='button'], input[type='submit'], button:not([type])",
    )
    # Also check page-level if container had nothing
    if not buttons:
        buttons = await page.query_selector_all(
            "button[type='submit'], button[type='button'], input[type='submit']"
        )

    submit_kw = {"submit", "apply", "send", "complete", "finish", "apply now"}
    next_kw    = {"next", "continue", "proceed", "save and continue", "save & continue", "next step"}

    visible_texts = []
    for btn in buttons:
        try:
            if await btn.is_visible():
                visible_texts.append((await btn.inner_text()).strip().lower())
        except Exception:
            continue

    for t in visible_texts:
        if any(kw in t for kw in submit_kw):
            return "submit"
    for t in visible_texts:
        if any(kw in t for kw in next_kw):
            return "next"
    return "unknown"


async def _click_submit(page: Page, container=None) -> None:
    """Click the first visible submit-like button (container-scoped, then page-wide)."""
    scope = container or page
    for label in SUBMIT_BUTTON_TEXTS:
        for sel in [f"button:has-text('{label}')", f"input[value='{label}']"]:
            try:
                btn = await _scope_query_one(scope, sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return
            except Exception:
                continue
    # Page-wide fallback
    for sel in ["input[type='submit']", "button[type='submit']"]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue


async def _click_next(page: Page, container=None) -> None:
    """Click the first visible next/continue button (container-scoped)."""
    scope = container or page
    for label in ["Next", "Continue", "Proceed", "Save and Continue", "Save & Continue", "Next Step"]:
        try:
            btn = await _scope_query_one(scope, f"button:has-text('{label}')")
            if btn and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue


def _is_success_page(html: str) -> bool:
    """Return True if page content signals a completed application."""
    visible_text = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible_text = re.sub(r"<[^>]+>", " ", visible_text)
    visible_text = re.sub(r"\s+", " ", visible_text)
    return bool(_SUCCESS_RE.search(visible_text))

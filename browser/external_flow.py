"""
external_flow.py
────────────────
Generic external job application handler.

Used when a LinkedIn or Naukri listing links to a company's own ATS
(Workday, Greenhouse, Lever, iCIMS, Taleo, etc.) rather than using the
platform's native apply flow.

Strategy:
  1. Navigate to the external URL with full stealth + PopupHandler.
  2. Verify the page is actually a job application form (not a listing /
     careers home / generic company page) before touching any inputs.
  3. Upload the tailored resume to any file input found.
  4. Pre-fill personal fields from settings (phone, location, CTC, etc.)
     before touching the LLM — avoids unnecessary API calls and halting
     stdin prompts for fields we already know.
  5. Discover and fill all remaining visible form fields via:
       form_memory cache → manual stdin (sensitive) → LLM fallback
     Inputs inside <footer>, <nav>, <header> or tagged as search /
     newsletter are skipped entirely.
  6. Handle text, textarea, select, radio, checkbox, date, contenteditable
     divs, and custom combobox / react-select style dropdowns.
  7. Click the primary submit button and confirm with a success-page check.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

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
    "Submit application",
    "Submit Application",
    "Submit",
    "Apply",
    "Apply now",
    "Apply Now",
    "Send application",
    "Complete application",
]

# Fields to prompt the user for via stdin rather than the LLM.
MANUAL_FIELDS = [
    "date of birth", "nationality", "passport", "ssn", "social security",
    "bank account", "pan number", "aadhaar",
]

# Labels / placeholders that indicate a non-application input (site chrome)
_JUNK_LABEL_SIGNALS = [
    "search", "subscribe", "newsletter", "email updates", "notify me",
    "sign up", "mailing list", "alerts",
]

# CSS ancestor selectors that mark inputs as site-chrome rather than form fields
_JUNK_ANCESTORS = ["footer", "nav", "header", "[role='navigation']", "[role='banner']"]

# Known ATS URL substrings — pages whose URL matches are almost certainly real
# application forms even if they have few form signals.
_KNOWN_ATS_HOSTS = [
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
    "icims.com", "taleo.net", "smartrecruiters.com", "jobvite.com",
    "ashbyhq.com", "bamboohr.com", "recruitee.com", "workable.com",
    "rippling.com", "jazz.co", "breezy.hr", "pinpointhq.com",
    "teamtailor.com", "dover.com", "apply.com",
]


# ── Settings-backed personal field map ───────────────────────────────────────

def _settings_map() -> dict:
    return {
        "phone":                  settings.phone,
        "mobile":                 settings.phone,
        "contact number":         settings.phone,
        "city":                   settings.current_location,
        "location":               settings.current_location,
        "current location":       settings.current_location,
        "notice period":          settings.notice_period,
        "current ctc":            settings.current_ctc,
        "current salary":         settings.current_ctc,
        "expected ctc":           settings.expected_ctc,
        "expected salary":        settings.expected_ctc,
        "total experience":       settings.total_experience_years,
        "years of experience":    settings.total_experience_years,
        "experience":             settings.total_experience_years,
        "zip":                    "",
        "postal":                 "",
        "pincode":                "",
    }


# ── Application-page guard ────────────────────────────────────────────────────

async def _is_application_page(page: Page) -> bool:
    """
    Return True only if the current page looks like a real job application form.

    Checks (any one is sufficient):
      1. URL matches a known ATS hostname.
      2. A file-upload input exists (resume upload → almost certainly an ATS).
      3. At least 2 of the 4 personal-info signals are present:
           name field, email field, phone/tel field, cover-letter textarea.
      4. Page contains an explicit application-form heading keyword.

    If none match → it's a careers listing page / company homepage / generic
    page and we should bail rather than filling random inputs.
    """
    url = page.url.lower()

    # 1. Known ATS host
    for host in _KNOWN_ATS_HOSTS:
        if host in url:
            return True

    try:
        # 2. File upload input (resume field)
        file_input = await page.query_selector("input[type='file']")
        if file_input:
            return True

        # 3. Personal-info signal count
        signals = 0
        checks = [
            # name field
            "input[autocomplete='name'], input[name*='name'], "
            "input[placeholder*='name' i], input[aria-label*='name' i]",
            # email field
            "input[type='email'], input[autocomplete='email'], "
            "input[name*='email'], input[placeholder*='email' i]",
            # phone / tel field
            "input[type='tel'], input[autocomplete='tel'], "
            "input[name*='phone'], input[placeholder*='phone' i]",
            # cover letter / motivation textarea
            "textarea[name*='cover'], textarea[placeholder*='cover' i], "
            "textarea[aria-label*='cover' i], textarea[name*='letter' i]",
        ]
        for sel in checks:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    signals += 1
            except Exception:
                pass
        if signals >= 2:
            return True

        # 4. Page heading keyword
        heading_sel = "h1, h2, [class*='title'], [class*='heading']"
        headings = await page.query_selector_all(heading_sel)
        for h in headings[:8]:
            try:
                text = (await h.inner_text()).lower()
                if any(kw in text for kw in ("apply", "application", "job application", "submit your")):
                    return True
            except Exception:
                pass

    except Exception:
        pass

    return False


# ── Junk-input guard ──────────────────────────────────────────────────────────

async def _is_junk_input(page: Page, element) -> bool:
    """
    Return True if the input belongs to site chrome (footer, nav, header)
    or is a search / newsletter subscription box rather than an application
    form field.
    """
    try:
        # Check ancestor elements for structural chrome tags
        for ancestor_sel in _JUNK_ANCESTORS:
            is_inside = await page.evaluate(
                """([el, sel]) => {
                    let node = el;
                    while (node && node !== document.body) {
                        if (node.matches && node.matches(sel)) return true;
                        node = node.parentElement;
                    }
                    return false;
                }""",
                [element, ancestor_sel],
            )
            if is_inside:
                return True

        # Check label / placeholder / name for junk signals
        label = (await _get_field_label(page, element)).lower()
        placeholder = ((await element.get_attribute("placeholder")) or "").lower()
        input_type = ((await element.get_attribute("type")) or "").lower()
        input_name = ((await element.get_attribute("name")) or "").lower()

        if input_type == "search":
            return True

        combined = f"{label} {placeholder} {input_name}"
        if any(sig in combined for sig in _JUNK_LABEL_SIGNALS):
            return True

    except Exception:
        pass

    return False


# ── Public entry point ────────────────────────────────────────────────────────

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

        # Wait for the ATS to fully settle (some redirect through SSO / OAuth)
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        await random_delay(1.0, 2.0)
        await handler.dismiss_all()

        # ── Guard: bail if this doesn't look like an application form ──
        if not await _is_application_page(page):
            print(
                f"[External] Page does not look like a job application form — skipping.\n"
                f"           URL: {page.url}"
            )
            await handler.stop_auto_dismiss()
            return False

        # Upload resume first — many ATSs pre-parse it to fill other fields
        await _handle_resume_upload(page, tailored_resume_path)
        await random_delay(1.5, 2.5)

        # Multi-step form loop (up to 12 pages / steps)
        max_steps = 12
        for step in range(max_steps):
            print(f"[External] Form step {step + 1} — {page.url}")

            await _fill_form_fields(page, resume_text, llm_answer_fn)
            await handler.dismiss_all()

            if _is_success_page(await page.content()):
                print("[External] Success page detected — application submitted!")
                await handler.stop_auto_dismiss()
                return True

            action = await _get_next_action(page)

            if action == "submit":
                await _click_submit(page)
                await random_delay(2.5, 4.5)
                await handler.dismiss_all()

                if _is_success_page(await page.content()):
                    print("[External] Application submitted successfully!")
                    await handler.stop_auto_dismiss()
                    return True

                # Some ATSs show a final review page after the first Submit click
                await _handle_resume_upload(page, tailored_resume_path)
                await _fill_form_fields(page, resume_text, llm_answer_fn)
                await _click_submit(page)
                await random_delay(2.0, 3.5)

                if _is_success_page(await page.content()):
                    print("[External] Application submitted (post-review page)!")
                    await handler.stop_auto_dismiss()
                    return True

                await handler.stop_auto_dismiss()
                return False

            elif action == "next":
                await _click_next(page)
                await random_delay(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                await handler.dismiss_all()
                await _handle_resume_upload(page, tailored_resume_path)

            else:
                print(f"[External] Unknown action at step {step + 1}, bailing.")
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

async def _handle_resume_upload(page: Page, resume_path: str) -> None:
    """Upload resume to any visible (or hidden-but-present) file input."""
    if not resume_path or not os.path.exists(resume_path):
        return
    try:
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


async def _fill_personal_fields(page: Page) -> None:
    """
    Pre-fill common personal-info fields directly from settings values.
    Runs before the LLM pass to avoid wasting tokens on data we already have.
    Skips inputs inside site-chrome elements (footer, nav, header) and
    search / newsletter inputs.
    """
    smap = _settings_map()
    text_inputs = await page.query_selector_all(
        "input[type='text'], input[type='number'], input[type='email'], "
        "input[type='tel'], input[type='url'], textarea"
    )
    for inp in text_inputs:
        try:
            if not await inp.is_visible():
                continue
            # Skip site-chrome / junk inputs
            if await _is_junk_input(page, inp):
                continue
            existing = await inp.input_value()
            if existing.strip():
                continue
            label = await _get_field_label(page, inp)
            if not label:
                continue
            label_lower = label.lower()
            for key, value in smap.items():
                if key in label_lower and value:
                    await human_fill(inp, value)
                    save_answer(label, value)
                    await random_delay(0.3, 0.7)
                    break
        except Exception:
            continue


async def _fill_form_fields(page: Page, resume_text: str, llm_answer_fn) -> None:
    """
    Detect and fill every visible form element on the current page.
    Covers: text/number/email/tel/url inputs, textareas, select dropdowns,
    radio fieldsets, individual checkboxes, date pickers, contenteditable
    divs, and custom combobox / react-select components.

    Inputs inside footer/nav/header or flagged as search/newsletter are
    skipped. LLM answers are capped to the field's maxlength attribute (or
    500 chars if absent) to prevent timeout errors from typing huge paragraphs.
    """
    # 1. Personal fields from settings first (no API call needed)
    await _fill_personal_fields(page)

    # 2. Text / number / email / tel / url / textarea
    text_inputs = await page.query_selector_all(
        "input[type='text'], input[type='number'], input[type='email'], "
        "input[type='tel'], input[type='url'], textarea"
    )
    for inp in text_inputs:
        try:
            if not await inp.is_visible():
                continue
            # Skip site-chrome / junk inputs
            if await _is_junk_input(page, inp):
                continue
            label = await _get_field_label(page, inp)
            if not label:
                continue
            existing = await inp.input_value()
            if existing.strip():
                continue

            # Determine the field's maxlength so we can cap the answer
            try:
                maxlen_attr = await inp.get_attribute("maxlength")
                max_chars = int(maxlen_attr) if maxlen_attr and maxlen_attr.isdigit() else 500
            except Exception:
                max_chars = 500

            answer = await _resolve_answer(label, resume_text, llm_answer_fn)
            if answer:
                # Hard-cap to avoid ElementHandle.type() timeout
                answer = answer[:max_chars]
                await human_fill(inp, answer)
                await random_delay(0.3, 0.8)
        except Exception as exc:
            print(f"[External] Text field warning: {exc}")

    # 3. Date inputs
    date_inputs = await page.query_selector_all("input[type='date']")
    for inp in date_inputs:
        try:
            if not await inp.is_visible():
                continue
            existing = await inp.input_value()
            if existing.strip():
                continue
            label = await _get_field_label(page, inp)
            saved = get_answer(label) if label else None
            if saved:
                await inp.fill(saved)
                await random_delay(0.3, 0.6)
        except Exception:
            continue

    # 4. Select dropdowns
    selects = await page.query_selector_all("select")
    for sel_el in selects:
        try:
            if not await sel_el.is_visible():
                continue
            label = await _get_field_label(page, sel_el)
            if not label:
                continue
            saved = get_answer(label)
            if saved:
                await sel_el.select_option(label=saved)
                await random_delay(0.3, 0.7)
                continue
            options = await sel_el.query_selector_all("option")
            option_texts = []
            for opt in options:
                t = (await opt.inner_text()).strip()
                if t and t not in ("Select", "-- Select --", ""):
                    option_texts.append(t)
            if option_texts:
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

    # 5. Custom combobox / react-select / aria-combobox components
    await _fill_custom_dropdowns(page, resume_text, llm_answer_fn)

    # 6. Radio fieldsets
    fieldsets = await page.query_selector_all("fieldset")
    for fs in fieldsets:
        try:
            legend = await fs.query_selector("legend")
            label = (await legend.inner_text()).strip() if legend else ""
            radios = await fs.query_selector_all("input[type='radio']")
            if not radios:
                continue
            if any([await r.is_checked() for r in radios]):
                continue
            saved = get_answer(label)
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

    # 7. Standalone checkboxes — e.g. "I agree to terms"
    checkboxes = await page.query_selector_all(
        "input[type='checkbox']:not([disabled])"
    )
    for cb in checkboxes:
        try:
            if not await cb.is_visible():
                continue
            if await cb.is_checked():
                continue
            label = await _get_field_label(page, cb)
            label_lower = label.lower()
            if any(
                kw in label_lower
                for kw in ("agree", "consent", "terms", "privacy", "authoriz")
            ):
                await cb.check()
                await random_delay(0.2, 0.5)
        except Exception:
            pass

    # 8. Contenteditable divs (rich-text editors in some React ATSs)
    ce_divs = await page.query_selector_all(
        "[contenteditable='true']:not([aria-hidden='true'])"
    )
    for div in ce_divs:
        try:
            if not await div.is_visible():
                continue
            current_text = (await div.inner_text()).strip()
            if current_text:
                continue
            label = await _get_field_label(page, div)
            if not label:
                continue
            # Skip junk contenteditable (site search bars, etc.)
            if await _is_junk_input(page, div):
                continue
            answer = await _resolve_answer(label, resume_text, llm_answer_fn)
            if answer:
                answer = answer[:1000]  # reasonable cap for rich-text fields
                await div.click()
                await random_delay(0.2, 0.4)
                await div.type(answer, delay=40)
                await random_delay(0.3, 0.7)
        except Exception:
            pass


async def _fill_custom_dropdowns(page: Page, resume_text: str, llm_answer_fn) -> None:
    """
    Handle aria-role='combobox', react-select, and similar custom dropdowns
    that don't use native <select> elements.
    """
    comboboxes = await page.query_selector_all(
        "[role='combobox']:not([disabled]), .react-select__control, "
        "[class*='Select__control']"
    )
    for cb in comboboxes:
        try:
            if not await cb.is_visible():
                continue
            if await _is_junk_input(page, cb):
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
            option_texts = []
            for opt in options:
                t = (await opt.inner_text()).strip()
                if t:
                    option_texts.append(t)
            if not option_texts:
                await page.keyboard.press("Escape")
                continue
            saved = get_answer(label)
            chosen = saved
            if not chosen:
                chosen = await llm_answer_fn(
                    f"{label} (choose one: {', '.join(option_texts[:15])})",
                    resume_text,
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
    Best-effort label extraction (in priority order):
      1. aria-label attribute
      2. aria-labelledby → text of the referenced element
      3. <label for="id"> lookup
      4. placeholder attribute
      5. name attribute (humanised)
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

async def _resolve_answer(
    label: str, resume_text: str, llm_answer_fn
) -> Optional[str]:
    """
    Resolution order:
      1. Settings map (phone, location, CTC, notice period …)
      2. form_memory cache
      3. stdin prompt for highly sensitive fields (DOB, passport, etc.)
      4. LLM fallback (answer capped to 300 chars)
    """
    # 1. Settings
    label_lower = label.lower()
    smap = _settings_map()
    for key, value in smap.items():
        if key in label_lower and value:
            print(f"[Settings] '{label}' → '{value}'")
            save_answer(label, value)
            return value

    # 2. Memory cache
    saved = get_answer(label)
    if saved:
        print(f"[FormMemory] '{label}' → '{saved}'")
        return saved

    # 3. Manual stdin for sensitive fields
    for field in MANUAL_FIELDS:
        if field in label_lower:
            print(f"\n[External][SENSITIVE FIELD] '{label}' — enter your answer:")
            answer = input("  >> ").strip()
            if answer:
                save_answer(label, answer)
                return answer
            return None

    # 4. LLM — cap at 300 chars to prevent ElementHandle.type() timeout
    print(f"[External][LLM] '{label}'")
    answer = await llm_answer_fn(label, resume_text)
    if answer:
        answer = answer.strip()[:300]
        save_answer(label, answer)
    return answer or None


# ── Navigation helpers ────────────────────────────────────────────────────────

async def _get_next_action(page: Page) -> str:
    """Return 'submit' | 'next' | 'unknown' based on visible buttons."""
    buttons = await page.query_selector_all(
        "button[type='submit'], button[type='button'], input[type='submit'], "
        "button:not([type])"
    )
    submit_keywords = {"submit", "apply", "send", "complete", "finish"}
    next_keywords = {
        "next", "continue", "proceed", "save and continue",
        "save & continue", "next step",
    }

    visible = []
    for btn in buttons:
        try:
            if await btn.is_visible():
                text = (await btn.inner_text()).strip().lower()
                visible.append(text)
        except Exception:
            continue

    for text in visible:
        if any(kw in text for kw in submit_keywords):
            return "submit"
    for text in visible:
        if any(kw in text for kw in next_keywords):
            return "next"

    return "unknown"


async def _click_submit(page: Page) -> None:
    """Click the first visible submit-like button."""
    for label in SUBMIT_BUTTON_TEXTS:
        try:
            btn = await page.query_selector(
                f"button:has-text('{label}'), input[value='{label}']"
            )
            if btn and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue
    try:
        btn = await page.query_selector("input[type='submit'], button[type='submit']")
        if btn and await btn.is_visible():
            await btn.click()
    except Exception:
        pass


async def _click_next(page: Page) -> None:
    """Click the first visible next/continue button."""
    next_labels = [
        "Next", "Continue", "Proceed",
        "Save and Continue", "Save & Continue", "Next Step",
    ]
    for label in next_labels:
        try:
            btn = await page.query_selector(f"button:has-text('{label}')")
            if btn and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue


def _is_success_page(html: str) -> bool:
    """Return True if the page content signals a completed application."""
    return bool(_SUCCESS_RE.search(html))

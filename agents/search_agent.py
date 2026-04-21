"""
search_agent.py
───────────────
Searches LinkedIn and Naukri for both full-time roles and internships.
Each JobListing carries an `is_internship` flag so the scorer can apply
the correct (lower) threshold, and an `apply_type` field so apply_agent
routes to the correct apply flow.
"""
from __future__ import annotations

import asyncio
import re
import random
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page

from browser.stealth import (
    get_launch_args,
    get_context_options,
    human_scroll_to_bottom,
    random_delay,
    STEALTH_INIT_SCRIPT,
)
from browser.popup_handler import PopupHandler, safe_goto
from browser.linkedin_flow import linkedin_login
from browser.naukri_flow import naukri_login
from config import settings


# ── Internship detection keywords ─────────────────────────────────────────────

INTERNSHIP_TITLE_SIGNALS = [
    "intern", "internship", "trainee", "apprentice", "student", "fresher",
    "graduate trainee", "summer project", "research assistant",
]

INTERNSHIP_JD_SIGNALS = [
    "internship", "intern position", "stipend", "college students",
    "pursuing b.tech", "pursuing m.tech", "pursuing msc", "pursuing bsc",
    "currently enrolled", "final year", "pre-placement offer", "ppo",
    "6 months internship", "3 months internship",
]


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class JobListing:
    job_title: str
    company: str
    location: str
    jd_text: str
    apply_url: str
    platform: str               # 'linkedin' or 'naukri'
    recruiter_name: str = ""
    is_internship: bool = False  # detected automatically
    apply_type: str = "easy_apply"  # 'easy_apply' | 'external' | 'naukri'
    raw_html: str = ""


def _detect_internship(title: str, jd_text: str) -> bool:
    """Return True if the listing looks like an internship."""
    combined = (title + " " + jd_text).lower()
    return any(sig in combined for sig in INTERNSHIP_TITLE_SIGNALS + INTERNSHIP_JD_SIGNALS)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_search_agent() -> List[JobListing]:
    """
    Scrape LinkedIn + Naukri for configured keywords (jobs + internships).
    Returns deduplicated list capped at max_listings_per_run.
    """
    all_listings: List[JobListing] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**get_launch_args())
        context = await browser.new_context(**get_context_options())
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        try:
            # ── LinkedIn ──────────────────────────────────────────────
            if settings.linkedin_email and settings.linkedin_password:
                print("[Search] LinkedIn scrape starting...")
                li_page = await linkedin_login(context)
                handler = PopupHandler(li_page)
                await handler.start_auto_dismiss()

                # Full-time keywords
                for kw in settings.keyword_list:
                    listings = await _scrape_linkedin(li_page, kw, internship=False)
                    all_listings.extend(listings)
                    await random_delay(2.5, 5.0)
                    if len(all_listings) >= settings.max_listings_per_run:
                        break

                # Internship keywords
                for kw in settings.internship_keyword_list:
                    listings = await _scrape_linkedin(li_page, kw, internship=True)
                    all_listings.extend(listings)
                    await random_delay(2.5, 5.0)

                await handler.stop_auto_dismiss()
                await li_page.close()
            else:
                print("[Search] Skipping LinkedIn (no credentials).")

            # ── Naukri ────────────────────────────────────────────────
            if settings.naukri_email and settings.naukri_password:
                print("[Search] Naukri scrape starting...")
                nk_page = await naukri_login(context)
                handler = PopupHandler(nk_page)
                await handler.start_auto_dismiss()

                for kw in settings.keyword_list:
                    listings = await _scrape_naukri(nk_page, kw, internship=False)
                    all_listings.extend(listings)
                    await random_delay(2.5, 5.0)

                for kw in settings.internship_keyword_list:
                    listings = await _scrape_naukri(nk_page, kw, internship=True)
                    all_listings.extend(listings)
                    await random_delay(2.5, 5.0)

                await handler.stop_auto_dismiss()
                await nk_page.close()
            else:
                print("[Search] Skipping Naukri (no credentials).")

        finally:
            await context.close()
            await browser.close()

    # Deduplicate by URL
    seen: set[str] = set()
    unique: List[JobListing] = []
    for lst in all_listings:
        if lst.apply_url not in seen:
            seen.add(lst.apply_url)
            unique.append(lst)

    internships = [j for j in unique if j.is_internship]
    jobs = [j for j in unique if not j.is_internship]
    print(
        f"[Search] Found {len(unique)} unique listings: "
        f"{len(jobs)} jobs + {len(internships)} internships."
    )
    return unique[: settings.max_listings_per_run * 2]   # give scorer more to work with


# ── LinkedIn scrapers ─────────────────────────────────────────────────────────

async def _scrape_linkedin(
    page: Page, keyword: str, internship: bool = False
) -> List[JobListing]:
    listings: List[JobListing] = []
    encoded_kw = quote_plus(keyword)
    encoded_loc = quote_plus(settings.search_location)

    # LinkedIn filter: f_E=1 = internship experience level
    exp_filter = "&f_E=1" if internship else "&f_LF=f_AL"
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={encoded_kw}&location={encoded_loc}{exp_filter}"
    )

    handler = PopupHandler(page)
    await safe_goto(page, url, handler=handler)
    await random_delay(2.0, 3.5)
    await handler.dismiss_all()
    await human_scroll_to_bottom(page, max_scrolls=6)

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    job_cards = soup.find_all(
        "div", {"class": lambda c: c and "job-card-container" in c}
    )
    if not job_cards:
        job_cards = soup.find_all(
            "li", {"class": lambda c: c and "jobs-search-results__list-item" in c}
        )

    print(
        f"[LinkedIn] {'Internship' if internship else 'Job'} "
        f"'{keyword}' → {len(job_cards)} cards"
    )

    for card in job_cards[:12]:
        try:
            listing = await _parse_linkedin_card(page, card, handler)
            if listing:
                if internship:
                    listing.is_internship = True
                listings.append(listing)
            await random_delay(0.4, 1.2)
        except Exception as exc:
            print(f"[LinkedIn] Card parse error: {exc}")

    return listings


async def _parse_linkedin_card(
    page: Page, card, handler: PopupHandler
) -> Optional[JobListing]:
    title_el = card.find(
        "a",
        {"class": lambda c: c and ("job-card-list__title" in c or "base-card__full-link" in c)},
    )
    if not title_el:
        return None

    job_title = title_el.get_text(strip=True)
    apply_url = title_el.get("href", "")
    if apply_url and not apply_url.startswith("http"):
        apply_url = "https://www.linkedin.com" + apply_url
    apply_url = apply_url.split("?")[0]

    company_el = card.find(
        "a", {"class": lambda c: c and "job-card-container__company-name" in c}
    )
    company = company_el.get_text(strip=True) if company_el else ""

    loc_el = card.find(
        "li", {"class": lambda c: c and "job-card-container__metadata-item" in c}
    )
    location = loc_el.get_text(strip=True) if loc_el else ""

    jd_text = ""
    recruiter_name = ""
    apply_type = "easy_apply"
    external_url = ""

    try:
        await safe_goto(page, apply_url, handler=handler)
        await random_delay(1.5, 3.0)
        await handler.dismiss_all()

        jd_html = await page.content()
        jd_soup = BeautifulSoup(jd_html, "lxml")

        jd_el = jd_soup.find(
            "div", {"class": lambda c: c and "description__text" in c}
        )
        if jd_el:
            jd_text = jd_el.get_text(separator="\n", strip=True)

        recruiter_el = jd_soup.find(
            "a", {"class": lambda c: c and "hirer-card__hirer-information" in c}
        )
        if recruiter_el:
            recruiter_name = recruiter_el.get_text(strip=True)

        # ── Detect Easy Apply vs external apply ───────────────────────
        easy_apply_btn = jd_soup.find(
            lambda tag: tag.name == "button"
            and "easy apply" in tag.get_text(strip=True).lower()
        )
        if not easy_apply_btn:
            try:
                live_btn = await page.query_selector(
                    "button.jobs-apply-button, .jobs-apply-button--top-card"
                )
                if live_btn:
                    btn_text = (await live_btn.inner_text()).strip().lower()
                    if "easy apply" not in btn_text:
                        apply_type = "external"
                        href = await live_btn.get_attribute("href")
                        if href and href.startswith("http"):
                            external_url = href
                        else:
                            # Try capturing popup tab
                            try:
                                async with page.context.expect_page(timeout=5_000) as popup_info:
                                    await live_btn.click()
                                popup = await popup_info.value
                                external_url = popup.url
                                await popup.close()
                                # Navigate back to job page
                                await safe_goto(page, apply_url, handler=handler)
                                await random_delay(1.0, 2.0)
                            except Exception:
                                pass
                else:
                    apply_type = "external"
            except Exception:
                apply_type = "easy_apply"  # fallback — try Easy Apply path

        await page.go_back(wait_until="domcontentloaded")
        await random_delay(1.0, 2.5)
        await handler.dismiss_all()
    except Exception:
        pass

    if not apply_url or not job_title:
        return None

    is_intern = _detect_internship(job_title, jd_text)
    final_url = external_url if (apply_type == "external" and external_url) else apply_url

    return JobListing(
        job_title=job_title,
        company=company,
        location=location,
        jd_text=jd_text,
        apply_url=final_url,
        platform="linkedin",
        recruiter_name=recruiter_name,
        is_internship=is_intern,
        apply_type=apply_type,
    )


# ── Naukri scrapers ───────────────────────────────────────────────────────────

# Selectors tried in order; Naukri updates its CSS classes frequently.
_NAUKRI_CARD_SELECTORS = [
    ("div",     lambda c: c and "cust-job-tuple" in c),
    ("article", lambda c: c and "jobTuple" in c),
    ("div",     lambda c: c and "srp-jobtuple-wrapper" in c),
    ("div",     lambda c: c and "job-tuple" in (c or "").lower()),
]

_NAUKRI_SEARCH_BASE = "https://www.naukri.com/jobs"


async def _scrape_naukri(
    page: Page, keyword: str, internship: bool = False
) -> List[JobListing]:
    """
    Scrape Naukri via /jobs?k=KEYWORD&l=LOCATION — the most reliable URL format.
    Internship vs. full-time comes from the keyword text in .env.
    """
    listings: List[JobListing] = []
    location = settings.search_location
    encoded_kw  = quote_plus(keyword)
    encoded_loc = quote_plus(location)

    primary_url = f"{_NAUKRI_SEARCH_BASE}?k={encoded_kw}&l={encoded_loc}"
    fallback_url = f"{_NAUKRI_SEARCH_BASE}?k={encoded_kw}"

    handler = PopupHandler(page)

    await safe_goto(page, primary_url, handler=handler)
    await random_delay(2.0, 4.0)
    await handler.dismiss_and_escape()

    # If Naukri redirected to campus.naukri.com, force back to main site
    if "campus.naukri.com" in page.url:
        print(f"[Naukri] Redirected to campus sub-site — forcing back to main site.")
        await safe_goto(page, primary_url, handler=handler)
        await random_delay(2.0, 3.5)
        await handler.dismiss_and_escape()

    # Campus account detection — strip 'intern' suffix if needed
    in_campus = await _is_campus_mode(page)
    if in_campus and internship:
        clean_kw = re.sub(
            r"\s*(intern(ship)?|trainee)\s*$", "", keyword, flags=re.IGNORECASE
        ).strip()
        if clean_kw and clean_kw != keyword:
            print(f"[Naukri] Campus mode — retrying without 'intern' suffix: '{clean_kw}'")
            alt_primary = f"{_NAUKRI_SEARCH_BASE}?k={quote_plus(clean_kw)}&l={quote_plus(location)}"
            alt_fallback = f"{_NAUKRI_SEARCH_BASE}?k={quote_plus(clean_kw)}"
            await safe_goto(page, alt_primary, handler=handler)
            await random_delay(2.0, 3.5)
            await handler.dismiss_and_escape()
            primary_url = alt_primary
            fallback_url = alt_fallback

    # Wait for React-rendered cards
    card_wait_sel = (
        ".cust-job-tuple, article.jobTuple, "
        ".srp-jobtuple-wrapper, [data-job-id]"
    )
    try:
        await page.wait_for_selector(card_wait_sel, timeout=10_000)
    except Exception:
        print(f"[Naukri] No cards at primary URL for '{keyword}', trying fallback...")
        await safe_goto(page, fallback_url, handler=handler)
        await random_delay(2.0, 4.0)
        await handler.dismiss_and_escape()
        try:
            await page.wait_for_selector(card_wait_sel, timeout=8_000)
        except Exception:
            print(f"[Naukri] Trying searchbar fallback for '{keyword}'...")
            found = await _search_via_naukri_searchbar(page, keyword, location, handler)
            if not found:
                print(f"[Naukri] No cards found for '{keyword}' — skipping.")
                return listings

    await human_scroll_to_bottom(page, max_scrolls=5)
    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    job_cards: list = []
    for tag, cls_fn in _NAUKRI_CARD_SELECTORS:
        job_cards = soup.find_all(tag, {"class": cls_fn})
        if job_cards:
            break
    if not job_cards:
        job_cards = soup.find_all(attrs={"data-job-id": True})

    print(
        f"[Naukri] {'Internship' if internship else 'Job'} "
        f"'{keyword}' → {len(job_cards)} cards"
    )

    for card in job_cards[:12]:
        try:
            listing = _parse_naukri_card(card)
            if listing:
                if internship:
                    listing.is_internship = True
                listings.append(listing)
        except Exception as exc:
            print(f"[Naukri] Card parse error: {exc}")

    return listings


async def _is_campus_mode(page: Page) -> bool:
    """
    Detect if Naukri has switched to campus/fresher mode.
    """
    try:
        campus_el = await page.query_selector(
            ".naukri-campus-logo, [class*='campusLogo'], "
            "a[href*='naukricampus'], .nc-header"
        )
        if campus_el:
            return True
        header_text = await page.evaluate(
            "document.querySelector('header, .nI-gNb-header')?.innerText || ''"
        )
        return "naukri campus" in header_text.lower()
    except Exception:
        return False


async def _search_via_naukri_searchbar(
    page: Page, keyword: str, location: str, handler: PopupHandler
) -> bool:
    """Last-resort fallback: use Naukri's search bar directly."""
    try:
        await safe_goto(page, "https://www.naukri.com/", handler=handler)
        await random_delay(2.0, 3.5)
        await handler.dismiss_and_escape()

        in_campus = await _is_campus_mode(page)
        search_kw = keyword
        if in_campus:
            search_kw = re.sub(
                r"\s*(intern(ship)?|trainee)\s*$", "", keyword, flags=re.IGNORECASE
            ).strip() or keyword
            print(f"[Naukri] Campus mode detected — searching as '{search_kw}'")

        kw_selectors = [
            "input[placeholder*='Skills']",
            "input[placeholder*='Job title']",
            "input[placeholder*='keyword']",
            "input[placeholder*='Search']",
            "input.suggestor-input",
            "input[name='qp']",
        ]
        filled_kw = False
        for sel in kw_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.fill(search_kw)
                    filled_kw = True
                    break
            except Exception:
                continue
        if not filled_kw:
            print("[Naukri] Searchbar: keyword input not found")
            return False
        await random_delay(0.5, 1.0)

        loc_selectors = [
            "input[placeholder*='Location']",
            "input[placeholder*='City']",
            "input[placeholder*='location']",
            "input.loc-input",
            "input[name='loc']",
        ]
        for sel in loc_selectors:
            try:
                loc_el = page.locator(sel).first
                if await loc_el.is_visible():
                    await loc_el.fill(location)
                    break
            except Exception:
                continue
        await random_delay(0.5, 1.0)

        submitted = False
        for btn_sel in [".qsb-search-btn", "button[type='submit']", "button:has-text('Search')"]:
            try:
                btn = page.locator(btn_sel).first
                if await btn.is_visible():
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            await page.keyboard.press("Enter")

        await random_delay(2.5, 4.0)
        await handler.dismiss_and_escape()

        if "campus.naukri.com" in page.url:
            await safe_goto(
                page,
                f"{_NAUKRI_SEARCH_BASE}?k={quote_plus(search_kw)}&l={quote_plus(location)}",
                handler=handler,
            )
            await random_delay(2.0, 3.5)

        try:
            await page.wait_for_selector(
                ".cust-job-tuple, article.jobTuple, .srp-jobtuple-wrapper, [data-job-id]",
                timeout=8_000,
            )
            return True
        except Exception:
            return False
    except Exception as exc:
        print(f"[Naukri] Searchbar fallback error: {exc}")
        return False


def _parse_naukri_card(card) -> Optional[JobListing]:
    """
    Parse a Naukri job card with chained fallback selectors for every field.
    """
    # ── Title + URL ───────────────────────────────────────────────────
    title_el = (
        card.find("a", {"class": lambda c: c and "title" in (c or "")})
        or card.find("a", {"class": lambda c: c and "job-title" in (c or "")})
        or card.find("a", {"class": re.compile(r"title|jobTitle", re.I)})
        or card.find("a", href=re.compile(r"naukri\.com/.+-\d+"))
    )
    if not title_el:
        return None

    job_title = title_el.get_text(strip=True)
    apply_url = title_el.get("href", "")
    if not apply_url:
        return None
    if apply_url.startswith("/"):
        apply_url = "https://www.naukri.com" + apply_url

    # ── Company ───────────────────────────────────────────────────────
    company_el = (
        card.find("a",    {"class": lambda c: c and "comp-name"  in (c or "")})
        or card.find("a",    {"class": lambda c: c and "subTitle"   in (c or "")})
        or card.find("span", {"class": lambda c: c and "comp-name"  in (c or "")})
        or card.find("span", {"class": lambda c: c and "company"    in (c or "").lower()})
    )
    company = company_el.get_text(strip=True) if company_el else ""

    # ── Location ──────────────────────────────────────────────────────
    loc_el = (
        card.find("span", {"class": lambda c: c and "locWdth"   in (c or "")})
        or card.find("li",   {"class": lambda c: c and "location"  in (c or "").lower()})
        or card.find("span", {"class": lambda c: c and "loc"       in (c or "").lower()})
    )
    location = loc_el.get_text(strip=True) if loc_el else ""

    # ── Description ───────────────────────────────────────────────────
    desc_el = (
        card.find("span", {"class": lambda c: c and "job-description" in (c or "")})
        or card.find("span", {"class": lambda c: c and "jd-desc"        in (c or "")})
        or card.find("div",  {"class": lambda c: c and "desc"           in (c or "").lower()})
    )
    jd_text = desc_el.get_text(strip=True) if desc_el else ""

    is_intern = _detect_internship(job_title, jd_text)

    # Determine apply type: if URL doesn't point to naukri.com, treat as external
    naukri_apply_type = "naukri"
    if apply_url and "naukri.com" not in apply_url:
        naukri_apply_type = "external"

    return JobListing(
        job_title=job_title,
        company=company,
        location=location,
        jd_text=jd_text,
        apply_url=apply_url,
        platform="naukri",
        is_internship=is_intern,
        apply_type=naukri_apply_type,
    )

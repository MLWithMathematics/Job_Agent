"""
search_agent.py
───────────────
Searches LinkedIn and Naukri for both full-time roles and internships.
Each JobListing carries an `is_internship` flag so the scorer can apply
the correct (lower) threshold.
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
                # Override is_internship flag if we used the internship search URL
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

        await page.go_back(wait_until="domcontentloaded")
        await random_delay(1.0, 2.5)
        await handler.dismiss_all()
    except Exception:
        pass

    if not apply_url or not job_title:
        return None

    is_intern = _detect_internship(job_title, jd_text)

    return JobListing(
        job_title=job_title,
        company=company,
        location=location,
        jd_text=jd_text,
        apply_url=apply_url,
        platform="linkedin",
        recruiter_name=recruiter_name,
        is_internship=is_intern,
    )


# ── Naukri scrapers ───────────────────────────────────────────────────────────

async def _scrape_naukri(
    page: Page, keyword: str, internship: bool = False
) -> List[JobListing]:
    listings: List[JobListing] = []
    encoded_kw = keyword.lower().replace(" ", "-")
    location_part = settings.search_location.lower().replace(" ", "-")

    if internship:
        # Naukri has a dedicated internship search
        url = f"https://www.naukri.com/{encoded_kw}-internship-jobs-in-{location_part}"
        fallback_url = f"https://www.naukri.com/{encoded_kw}-internship-jobs"
    else:
        url = f"https://www.naukri.com/{encoded_kw}-jobs-in-{location_part}"
        fallback_url = f"https://www.naukri.com/{encoded_kw}-jobs"

    handler = PopupHandler(page)
    await safe_goto(page, url, handler=handler)
    await random_delay(2.0, 4.0)
    await handler.dismiss_and_escape()
    await human_scroll_to_bottom(page, max_scrolls=5)

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    job_cards = soup.find_all("article", {"class": lambda c: c and "jobTuple" in c})
    if not job_cards:
        job_cards = soup.find_all(
            "div", {"class": lambda c: c and "srp-jobtuple-wrapper" in c}
        )
    # Naukri internship page uses different markup
    if not job_cards:
        job_cards = soup.find_all(
            "div", {"class": lambda c: c and "internship" in (c or "").lower()}
        )

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


def _parse_naukri_card(card) -> Optional[JobListing]:
    title_el = card.find("a", {"class": lambda c: c and "title" in (c or "")})
    if not title_el:
        # Fallback: any link with a job-sounding href
        title_el = card.find("a", href=re.compile(r"naukri\.com/.+-\d+"))
    if not title_el:
        return None

    job_title = title_el.get_text(strip=True)
    apply_url = title_el.get("href", "")
    if not apply_url:
        return None

    company_el = card.find("a", {"class": lambda c: c and "subTitle" in (c or "")})
    if not company_el:
        company_el = card.find("span", {"class": lambda c: c and "company" in (c or "").lower()})
    company = company_el.get_text(strip=True) if company_el else ""

    loc_el = card.find("span", {"class": lambda c: c and "loc" in (c or "").lower()})
    location = loc_el.get_text(strip=True) if loc_el else ""

    desc_el = card.find("span", {"class": lambda c: c and "job-description" in (c or "")})
    if not desc_el:
        desc_el = card.find("div", {"class": lambda c: c and "desc" in (c or "").lower()})
    jd_text = desc_el.get_text(strip=True) if desc_el else ""

    is_intern = _detect_internship(job_title, jd_text)

    return JobListing(
        job_title=job_title,
        company=company,
        location=location,
        jd_text=jd_text,
        apply_url=apply_url,
        platform="naukri",
        is_internship=is_intern,
    )

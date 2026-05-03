"""
session_manager.py
──────────────────
Manages *persistent* Playwright browser contexts that survive across process
restarts.  Cookies, localStorage, and session tokens are written to a
user-data directory on disk so the agent never needs to re-login unless:

  • The site has forcibly expired the session (LinkedIn auto-logout etc.)
  • The stored user-data directory is deleted manually.

Public API
----------
get_persistent_context(platform)
    Return (playwright, context) for the given platform ("linkedin" | "naukri").
    Creates the user-data dir if it does not exist.
    The caller is responsible for keeping the Playwright instance alive.

is_linkedin_logged_in(page)   → bool
is_naukri_logged_in(page)     → bool
    Quick checks: navigate to the dashboard URL and see if we land on the
    authenticated feed/homepage rather than a login wall.

SESSION_DIR
    Root directory where user-data dirs are stored.
    Default: <project_root>/browser/session_store/
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Tuple

from playwright.async_api import async_playwright, BrowserContext, Playwright

from browser.stealth import get_persistent_launch_args, get_context_options, STEALTH_INIT_SCRIPT

# ── Storage root ──────────────────────────────────────────────────────────────
# Resolve relative to this file so it works regardless of CWD.
_HERE = Path(__file__).parent
SESSION_DIR = _HERE / "session_store"

PLATFORM_DIRS = {
    "linkedin": SESSION_DIR / "linkedin",
    "naukri":   SESSION_DIR / "naukri",
}

# ── Stealth script injected into every page ───────────────────────────────────
_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
"""


# ── Auth check helpers ────────────────────────────────────────────────────────

async def is_linkedin_logged_in(context: BrowserContext) -> bool:
    """
    Open linkedin.com/feed in a temp page and check for the global nav.
    Returns True ONLY if we are genuinely authenticated.

    Key: we check the URL *path* (not the full URL) because LinkedIn's login
    redirect looks like ``/login?session_redirect=%2Ffeed%2F`` which contains
    'feed' in the query string — a naive ``"feed" in url`` falsely passes.
    """
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        # Wait for any login-wall redirects to finish
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        url = page.url.lower()

        # Reject immediately if we landed on a login / auth page
        if any(kw in url for kw in ("login", "signup", "checkpoint", "challenge", "authwall")):
            print("[SessionManager] LinkedIn check -> landed on login/auth page. NOT logged in.")
            return False

        # Positive: URL path is /feed or /in/ AND the global nav is present
        from urllib.parse import urlparse
        path = urlparse(page.url).path.lower()
        if path.startswith("/feed") or path.startswith("/in/"):
            nav = await page.query_selector("#global-nav, .global-nav")
            if nav:
                return True

        # Final fallback — just check for the nav element
        nav = await page.query_selector("#global-nav, .global-nav")
        return nav is not None
    except Exception:
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def is_naukri_logged_in(context: BrowserContext) -> bool:
    """
    Open naukri.com/mnjuser/homepage and check for authenticated header.
    Returns True ONLY if genuinely logged in (not on a login wall).
    """
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.naukri.com/mnjuser/homepage",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        url = page.url.lower()

        # Reject if redirected to any login page
        if "login" in url or "nlogin" in url:
            print("[SessionManager] Naukri check -> landed on login page. NOT logged in.")
            return False

        # Positive: on mnjuser page AND authenticated header element exists
        if "mnjuser" in url:
            header = await page.query_selector(
                ".nI-gNb-drawer, .nI-gNb-header, .nI-gNb-user-icon, .user-name"
            )
            if header:
                return True

        # Fallback — check header element regardless of URL
        header = await page.query_selector(
            ".nI-gNb-drawer, .nI-gNb-header, .nI-gNb-user-icon, .user-name"
        )
        return header is not None
    except Exception:
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ── Persistent context factory ────────────────────────────────────────────────

async def get_persistent_context(
    platform: str,
) -> Tuple[Playwright, BrowserContext]:
    """
    Launch (or reattach to) a persistent Chromium context for *platform*.

    The user-data directory is created automatically on first use and reused
    on every subsequent run — cookies and session tokens survive restarts.

    Parameters
    ----------
    platform : "linkedin" | "naukri"

    Returns
    -------
    (playwright_instance, context)
        The caller must keep the Playwright instance alive for the duration of
        the session and call pw.stop() when done.
    """
    if platform not in PLATFORM_DIRS:
        raise ValueError(f"Unknown platform '{platform}'. Expected 'linkedin' or 'naukri'.")

    user_data_dir = PLATFORM_DIRS[platform]
    user_data_dir.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()

    launch_args = get_persistent_launch_args()
    ctx_opts    = _persistent_context_options()

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        **launch_args,
        **ctx_opts,
    )

    # Inject stealth overrides into every new page
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    await context.add_init_script(_STEALTH_SCRIPT)

    print(
        f"[SessionManager] Persistent context ready for '{platform}' "
        f"(user-data: {user_data_dir})"
    )
    return pw, context


def _persistent_context_options() -> dict:
    """
    Context options for launch_persistent_context — mirrors get_context_options()
    but excludes keys that are not accepted by launch_persistent_context
    (e.g. 'geolocation' must be omitted when it's None).
    """
    from config import settings

    opts: dict = {
        "viewport": {
            "width": settings.viewport_width,
            "height": settings.viewport_height,
        },
        "user_agent": settings.user_agent,
        "locale":      "en-IN",
        "timezone_id": "Asia/Kolkata",
        "permissions": [],
        "extra_http_headers": {
            "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
        },
        # Keep the browser window open / visible so the user can see what
        # happens if a login challenge appears.
        "no_viewport": False,
    }
    return opts

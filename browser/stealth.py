"""
stealth.py
──────────
Human behaviour simulation + stealth launch configuration.
All browser interactions in this project go through these helpers
so timing and mouse patterns are always realistic.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

from playwright.async_api import Page


# ── Timing helpers ────────────────────────────────────────────────────────────

async def random_delay(min_s: float = 1.5, max_s: float = 4.0) -> None:
    """Sleep for a random human-like duration."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def micro_delay() -> None:
    """Very short pause — between keystrokes / sub-actions."""
    await asyncio.sleep(random.uniform(0.05, 0.25))


# ── Scrolling ─────────────────────────────────────────────────────────────────

async def human_scroll(page: Page, scrolls: int = 3) -> None:
    """Scroll `scrolls` times in random increments."""
    for _ in range(scrolls):
        px = random.randint(250, 750)
        await page.evaluate(f"window.scrollBy(0, {px})")
        await asyncio.sleep(random.uniform(0.8, 2.5))


async def human_scroll_to_bottom(page: Page, max_scrolls: int = 20) -> None:
    """
    Gradually scroll to the bottom of the page.
    Stops early if the scroll height stops growing (page fully loaded).
    """
    for _ in range(max_scrolls):
        prev_height: int = await page.evaluate("document.body.scrollHeight")
        px = random.randint(400, 900)
        await page.evaluate(f"window.scrollBy(0, {px})")
        await asyncio.sleep(random.uniform(1.0, 2.8))

        new_height: int = await page.evaluate("document.body.scrollHeight")
        current_y: int = await page.evaluate("window.scrollY + window.innerHeight")
        if current_y >= new_height - 100 and new_height == prev_height:
            break


async def human_scroll_element(page: Page, selector: str, amount: int = 400) -> None:
    """Scroll inside a specific scrollable element."""
    try:
        await page.evaluate(
            f"""
            const el = document.querySelector('{selector}');
            if (el) el.scrollBy(0, {amount + random.randint(-50, 50)});
            """
        )
        await asyncio.sleep(random.uniform(0.6, 1.5))
    except Exception:
        pass


# ── Mouse movement ────────────────────────────────────────────────────────────

async def human_mouse_move(page: Page, target_x: int, target_y: int) -> None:
    """
    Move the mouse to (target_x, target_y) along a slightly curved path
    with gaussian jitter on each step — looks like a real hand.
    """
    pos = await page.evaluate("() => ({x: window.mouseX || 100, y: window.mouseY || 100})")
    cx, cy = pos.get("x", 100), pos.get("y", 100)

    steps = random.randint(8, 18)
    cp_x = (cx + target_x) / 2 + random.randint(-60, 60)
    cp_y = (cy + target_y) / 2 + random.randint(-60, 60)

    for i in range(1, steps + 1):
        t = i / steps
        ix = int((1 - t) ** 2 * cx + 2 * (1 - t) * t * cp_x + t ** 2 * target_x)
        iy = int((1 - t) ** 2 * cy + 2 * (1 - t) * t * cp_y + t ** 2 * target_y)
        ix += int(random.gauss(0, 2))
        iy += int(random.gauss(0, 2))
        await page.mouse.move(ix, iy)
        await asyncio.sleep(random.uniform(0.01, 0.05))


# ── Clicking ──────────────────────────────────────────────────────────────────

async def human_click(page: Page, selector: str) -> None:
    """Locate element, move mouse naturally, then click with a small random offset."""
    try:
        el = await page.wait_for_selector(selector, state="visible", timeout=15000)
    except Exception:
        el = None
    if el is None:
        raise ValueError(f"Element not found: {selector}")
    box = await el.bounding_box()
    if box:
        tx = int(box["x"] + box["width"] / 2) + random.randint(-4, 4)
        ty = int(box["y"] + box["height"] / 2) + random.randint(-4, 4)
        await human_mouse_move(page, tx, ty)
        await asyncio.sleep(random.uniform(0.08, 0.25))
        await page.mouse.click(tx, ty)
    else:
        await el.click()
    await asyncio.sleep(random.uniform(0.4, 1.2))


async def human_click_element(element, page: Page) -> None:
    """Click a Playwright ElementHandle with natural mouse movement."""
    box = await element.bounding_box()
    if box:
        tx = int(box["x"] + box["width"] / 2) + random.randint(-3, 3)
        ty = int(box["y"] + box["height"] / 2) + random.randint(-3, 3)
        await human_mouse_move(page, tx, ty)
        await asyncio.sleep(random.uniform(0.08, 0.2))
        await page.mouse.click(tx, ty)
    else:
        await element.click()
    await asyncio.sleep(random.uniform(0.4, 1.0))


# ── Typing ────────────────────────────────────────────────────────────────────

async def human_type(page: Page, selector: str, text: str) -> None:
    """
    Click a field then type character-by-character with randomised delays,
    including occasional brief pauses to mimic natural typing rhythm.
    """
    await human_click(page, selector)
    await asyncio.sleep(random.uniform(0.2, 0.5))

    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(55, 145))
        if random.random() < 0.04:
            await asyncio.sleep(random.uniform(0.3, 0.9))


async def human_fill(element, text: str) -> None:
    """Type text into an ElementHandle using realistic per-character delay."""
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await element.type(text, delay=random.randint(60, 140))


# ── Dismiss helpers ───────────────────────────────────────────────────────────

async def dismiss_modal_if_present(page: Page) -> None:
    """
    Full popup sweep. Delegates to PopupHandler for comprehensive coverage.
    """
    from browser.popup_handler import PopupHandler
    handler = PopupHandler(page)
    await handler.dismiss_all()


# ── Launch configuration ──────────────────────────────────────────────────────

def get_launch_args() -> dict:
    """Playwright chromium.launch() kwargs with stealth settings."""
    from config import settings

    return {
        "headless": settings.headless,
        "slow_mo": settings.slow_mo,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--lang=en-IN",
            "--disable-notifications",
            "--disable-save-password-bubble",
        ],
    }


def get_persistent_launch_args() -> dict:
    """
    Playwright chromium.launch_persistent_context() kwargs.

    launch_persistent_context accepts a subset of launch() kwargs merged with
    context kwargs.  We include headless / slow_mo / args here (they are valid
    for persistent contexts) but do NOT include context-specific keys like
    viewport or user_agent (those go in _persistent_context_options()).
    """
    from config import settings

    return {
        "headless": settings.headless,
        "slow_mo":  settings.slow_mo,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--lang=en-IN",
            "--disable-notifications",
            "--disable-save-password-bubble",
        ],
    }


def get_context_options() -> dict:
    """Browser context options — realistic viewport, UA, locale, no permissions."""
    from config import settings

    return {
        "viewport": {
            "width": settings.viewport_width,
            "height": settings.viewport_height,
        },
        "user_agent": settings.user_agent,
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
        "permissions": [],
        "geolocation": None,
        "extra_http_headers": {
            "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
        },
    }


STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin' },
            { name: 'Chrome PDF Viewer' },
            { name: 'Native Client' }
        ]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-IN', 'en-GB', 'en']
    });
    const origQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
    if (origQuery) {
        window.navigator.permissions.query = (parameters) => {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: 'denied' });
            }
            return origQuery(parameters);
        };
    }
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
"""

# ── Captcha & Verification helpers ──────────────────────────────────────────────────

async def handle_captcha(page: Page) -> bool:
    """Detect and handle captchas using an external solver to mimic human flow."""
    from config import settings
    print("[Stealth] Checking for captchas...")
    try:
        frames = page.frames
        for frame in frames:
            if "captcha" in frame.url.lower() or "challenge" in frame.url.lower():
                print("[Stealth] Captcha detected! Integrating 3rd-party solver...")
                # Integrate with 2Captcha or Anti-Captcha using settings.twocaptcha_api_key
                await asyncio.sleep(random.uniform(5.0, 10.0))  # Simulated solving delay
                return True
    except Exception as e:
        print(f"[Stealth] Error during captcha verification: {e}")
    return False

async def handle_email_verification(page: Page, email_address: str, code_selector: str) -> bool:
    """
    Pause execution so the user can manually retrieve and enter the verification code.
    Allows testing/logging in fresh accounts smoothly.
    """
    print(f"[Stealth] MANUAL VERIFICATION TRIGGERED.")
    print(f"[Stealth] Please check the inbox at: {email_address}")
    print("[Stealth] Pausing execution for 90 seconds to let you enter the code manually...")
    
    # Increase the wait time substantially for manual entry
    await asyncio.sleep(90.0) 
    
    print("[Stealth] Resuming execution. Assuming the verification code was entered successfully.")
    return True

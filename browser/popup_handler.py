"""
popup_handler.py
─────────────────
Proactive + reactive dismissal of every kind of popup, overlay, cookie
banner, notification prompt, sign-in nag, chat widget, etc. that LinkedIn,
Naukri, and general websites throw at the browser.

Usage:
    from browser.popup_handler import PopupHandler
    handler = PopupHandler(page)
    await handler.dismiss_all()          # one-shot sweep
    await handler.start_auto_dismiss()   # background loop (every 2 s)
    await handler.stop_auto_dismiss()
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional

from playwright.async_api import Page, Dialog


# ── Selector banks ────────────────────────────────────────────────────────────

# Generic close / dismiss buttons (ordered from most specific → least)
GENERIC_CLOSE_SELECTORS = [
    # aria patterns
    "button[aria-label='Dismiss']",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "button[aria-label='Close dialog']",
    "button[aria-label='Dismiss dialog']",
    "button[aria-label='Cancel']",
    # data-test ids
    "[data-test='modal-close-btn']",
    "[data-test='close-btn']",
    "[data-testid='close-button']",
    "[data-testid='modal-close']",
    "[data-testid='dismiss-btn']",
    # common class patterns
    ".modal-close",
    ".close-button",
    ".close-btn",
    ".btn-close",
    ".dismiss-btn",
    ".overlay-close",
    ".popup-close",
    # SVG / icon close buttons
    "button svg[aria-label='Close']",
    # text-based (last resort)
    "button:has-text('Close')",
    "button:has-text('Dismiss')",
    "button:has-text('No thanks')",
    "button:has-text('Not now')",
    "button:has-text('Maybe later')",
    "button:has-text('Skip')",
]

# LinkedIn-specific selectors
LINKEDIN_POPUP_SELECTORS = [
    # Sign-in / join nag overlay
    ".authentication-outlet button[aria-label='Dismiss']",
    ".join-modal__dismiss",
    "button.artdeco-modal__dismiss",
<<<<<<< HEAD
    # Message / InMail prompts
    ".msg-overlay-bubble-header__control--close-btn",
    ".msg-overlay-list-bubble--is-minimized button",
    # "Follow company" / "Add connections" cards
=======
    # ── Message overlays ──────────────────────────────────────────────────────
    # ONLY close OPEN (expanded) message bubbles — never click minimised ones
    # (clicking a minimised bubble opens it, which is the reported bug)
    ".msg-overlay-bubble-header__control--close-btn",          # X on open chat
    "button[aria-label='Close your conversation']",
    "button[aria-label='Dismiss messaging overlay']",
    # ── "Follow company" / ad banners ────────────────────────────────────────
>>>>>>> a135004 (Updated..)
    ".ad-banner-container button[aria-label='Dismiss']",
    # Premium upsell
    ".premium-custom-cta-dismiss",
    "[data-test='premium-upsell-modal'] button[aria-label='Dismiss']",
    # Cookie consent
    "button#CybotCookiebotDialogBodyButtonDecline",
    ".cookie-consent-banner button:has-text('Reject')",
    ".cookie-consent-banner button:has-text('Accept')",
    # "Open in App" banner
    "button[aria-label='Dismiss app store banner']",
    ".app-aware-link--dismiss",
    # Profile strength / notification nudge
    ".ql-nudge-dismiss",
    ".onboarding-nudge__dismiss",
    # Easy Apply "You've already applied" overlay
    ".artdeco-modal__actionbar button:has-text('Done')",
    # Random welcome/intro dialogs
    ".artdeco-modal__dismiss",
    "button.artdeco-button--circle.artdeco-modal__dismiss",
]

<<<<<<< HEAD
=======
# Selectors that must NEVER be clicked (would open something instead of closing)
LINKEDIN_NEVER_CLICK = [
    ".msg-overlay-list-bubble--is-minimized",   # minimised chat — clicking opens it
    ".msg-overlay-list-bubble-item",             # individual chat tab in tray
]

>>>>>>> a135004 (Updated..)
# Naukri-specific selectors
NAUKRI_POPUP_SELECTORS = [
    # Login nudge
    ".loginModal .modal-close",
    "#loginModal .crossIcon",
    ".naukri-login-layer .close",
    # Chat / helpdesk widget
    "#zopimchat-box .zopim-widget-close",
    ".chatbot-close-btn",
    "#freshWidget .freshwidget-close",
    # Notification permission bar
    ".nI-pushNotification-closeBtn",
    ".pus-notification-close",
    # Cookie consent
    ".cookie-msg-close",
    ".cookies-accept-btn",   # just accept to continue
    # App download interstitial
    ".app-download-overlay .close",
    ".appBanner-close",
    # Profile completion nudge
    ".nudge-close",
    ".profileCompletionNudge .closeBtn",
    # "Apply saved" confirmation modal
    ".applySaveModal .modalClose",
    ".saveApplyModal button:has-text('Cancel')",
    # Subscription upsell
    ".subscriptionModal .close-icon",
    # Resume score pop-up
    ".resumeScoreModal .close",
    # Alert strip
    ".naukri-alert .close-alert",
]

# Cookie / GDPR banners (universal)
COOKIE_SELECTORS = [
    "#onetrust-reject-all-handler",
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept all')",
    "button:has-text('Reject all')",
    "button:has-text('Decline')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
    "[id*='cookie'] button",
    "[class*='cookie'] button",
    "[class*='consent'] button",
]

# Notification permission browser bar — handled via dialog API, not selectors
# Overlay / backdrop selectors (click outside to close)
OVERLAY_SELECTORS = [
    ".artdeco-modal__overlay",
    ".modal-backdrop",
    ".overlay-backdrop",
    "[data-test='modal-overlay']",
]


class PopupHandler:
    """
    Attaches to a Playwright Page and provides multi-strategy popup dismissal.
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._attach_dialog_handler()

    # ── Public API ────────────────────────────────────────────────────────────

    async def dismiss_all(self) -> int:
        """
        Run a single sweep of all dismissal strategies.
        Returns the number of popups successfully dismissed.
        """
        count = 0
<<<<<<< HEAD
=======
        count += await self._close_linkedin_messages()        # handle chats first
>>>>>>> a135004 (Updated..)
        count += await self._dismiss_by_selectors(LINKEDIN_POPUP_SELECTORS)
        count += await self._dismiss_by_selectors(NAUKRI_POPUP_SELECTORS)
        count += await self._dismiss_by_selectors(GENERIC_CLOSE_SELECTORS)
        count += await self._dismiss_by_selectors(COOKIE_SELECTORS)
        count += await self._dismiss_overlays()
        count += await self._dismiss_notification_permission()
        return count

    async def start_auto_dismiss(self, interval: float = 2.5) -> None:
        """Start a background coroutine that sweeps for popups every `interval` seconds."""
        self._running = True
        self._task = asyncio.create_task(self._auto_loop(interval))

    async def stop_auto_dismiss(self) -> None:
        """Stop the background sweep loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ── Internal strategies ───────────────────────────────────────────────────

    async def _auto_loop(self, interval: float) -> None:
        while self._running:
            try:
                await self.dismiss_all()
            except Exception:
                pass
            await asyncio.sleep(interval + random.uniform(-0.3, 0.5))

    def _attach_dialog_handler(self) -> None:
        """
        Auto-dismiss browser-native dialogs (alert/confirm/prompt)
        and notification permission requests.
        """
        async def handle_dialog(dialog: Dialog) -> None:
            try:
                await dialog.dismiss()
            except Exception:
                pass

        self.page.on("dialog", handle_dialog)

<<<<<<< HEAD
=======
    async def _close_linkedin_messages(self) -> int:
        """
        Close any EXPANDED LinkedIn message overlay bubbles without opening
        minimised ones.  The minimised tray items (.msg-overlay-list-bubble--is-minimized)
        are intentionally left alone — clicking them would open a new chat window,
        which is exactly the bug this fixes.
        """
        closed = 0
        try:
            # Only target expanded (non-minimised) message windows
            open_chats = await self.page.query_selector_all(
                ".msg-overlay-bubble-header"
            )
            for chat in open_chats:
                try:
                    # Confirm this chat is actually expanded (has a body visible)
                    parent = await chat.evaluate_handle(
                        "el => el.closest('.msg-overlay-list-bubble')"
                    )
                    is_minimised = await parent.evaluate(
                        "el => el && el.classList.contains('msg-overlay-list-bubble--is-minimized')"
                    )
                    if is_minimised:
                        continue  # leave minimised chats alone

                    close_btn = await chat.query_selector(
                        ".msg-overlay-bubble-header__control--close-btn, "
                        "button[aria-label='Close your conversation']"
                    )
                    if close_btn and await close_btn.is_visible():
                        await close_btn.click(timeout=1500)
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        closed += 1
                except Exception:
                    pass
        except Exception:
            pass
        return closed

>>>>>>> a135004 (Updated..)
    async def _dismiss_by_selectors(self, selectors: list[str]) -> int:
        """Click all visible, enabled elements matching any selector in the list."""
        dismissed = 0
        for selector in selectors:
            try:
                elements = await self.page.query_selector_all(selector)
                for el in elements:
                    try:
                        if await el.is_visible() and await el.is_enabled():
                            await el.click(timeout=1500)
                            await asyncio.sleep(random.uniform(0.3, 0.7))
                            dismissed += 1
                    except Exception:
                        pass
            except Exception:
                pass
        return dismissed

    async def _dismiss_overlays(self) -> int:
        """Click outside modal overlays / backdrops to close them."""
        dismissed = 0
        for selector in OVERLAY_SELECTORS:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    # Click the very edge (top-left corner of viewport) to close
                    await self.page.mouse.click(10, 10)
                    await asyncio.sleep(random.uniform(0.4, 0.8))
                    dismissed += 1
            except Exception:
                pass
        return dismissed

    async def _dismiss_notification_permission(self) -> int:
        """
        Handle browser notification permission requests via CDP.
        Sets permission to 'denied' so the prompt never appears.
        """
        try:
            context = self.page.context
            await context.grant_permissions([], origin=self.page.url)
        except Exception:
            pass
        return 0

    async def press_escape(self) -> None:
        """Press Escape — closes most modals as a last resort."""
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.3, 0.6))
        except Exception:
            pass

    async def dismiss_and_escape(self) -> None:
        """Full sweep + Escape key — use when entering a high-risk page."""
        await self.dismiss_all()
        await asyncio.sleep(0.5)
        await self.press_escape()
        await asyncio.sleep(0.3)
        await self.dismiss_all()  # second sweep after Escape


# ── Convenience function ──────────────────────────────────────────────────────

async def safe_goto(page: Page, url: str, handler: Optional[PopupHandler] = None) -> None:
    """
    Navigate to a URL and immediately dismiss any popups that appear.
    """
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.5, 3.0))

    h = handler or PopupHandler(page)
    await h.dismiss_and_escape()

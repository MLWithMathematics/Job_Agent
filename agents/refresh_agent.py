from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright

from browser.stealth import get_launch_args, get_context_options
from browser.naukri_flow import naukri_login, profile_refresh
from memory.ledger import log_refresh
from config import settings


async def do_refresh() -> None:
    """Perform a single Naukri profile refresh cycle."""
    print(f"[RefreshAgent] Starting refresh at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**get_launch_args())
        context = await browser.new_context(**get_context_options())
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        try:
            # Login first
            page = await naukri_login(context)
            await page.close()

            # Refresh profile
            success = await profile_refresh(context)
            log_refresh(success=success, notes="Scheduled refresh")
            print(f"[RefreshAgent] Refresh {'succeeded' if success else 'failed'}.")

        except Exception as exc:
            print(f"[RefreshAgent] Error: {exc}")
            log_refresh(success=False, notes=str(exc))
        finally:
            await context.close()
            await browser.close()


def start_scheduler() -> None:
    """Start the APScheduler to run refresh every N hours."""
    scheduler = AsyncIOScheduler()
    interval_hours = settings.naukri_refresh_interval_hours

    scheduler.add_job(
        do_refresh,
        trigger="interval",
        hours=interval_hours,
        id="naukri_refresh",
        replace_existing=True,
        next_run_time=datetime.now(),  # Run immediately on start
    )

    scheduler.start()
    print(
        f"[RefreshAgent] Scheduler started. "
        f"Refreshing every {interval_hours} hour(s). Press Ctrl+C to stop."
    )

    loop = asyncio.get_event_loop()
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        print("[RefreshAgent] Scheduler stopped.")
        scheduler.shutdown()


if __name__ == "__main__":
    # Add project root to path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    start_scheduler()

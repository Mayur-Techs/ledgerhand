"""Browser driver — manages the Playwright browser instance.

Why: centralise all browser lifecycle (launch, close, tracing, screenshot)
so the rest of the code never touches Playwright directly.
"""
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from pathlib import Path
from typing import Optional
from .guards import PathGuard

class Driver:
    def __init__(self, headless: bool = False, trace_dir: Optional[Path] = None,
                 allowed_paths: tuple[str, ...] = (),
                 allowed_origin: str = ""):
        """Launch Chromium. headless=False for the demo video.

        allowed_origin: the exact ERP origin, e.g. 'http://localhost:8001'.
        PathGuard will reject requests to any other host.
        """
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._trace_dir = trace_dir

        self.guard = PathGuard(tuple(allowed_paths), allowed_origin=allowed_origin)
        self._attach_guards(self._page, self.guard)

    def page(self) -> Page:
        """Return the current page."""
        return self._page
        
    def screenshot(self, path: Path) -> None:
        """Save screenshot to path."""
        self._page.screenshot(path=str(path))
        
    def start_trace(self) -> None:
        """Start Playwright tracing (screenshots+snapshots)."""
        self._context.tracing.start(screenshots=True, snapshots=True, sources=True)
        
    def stop_trace(self, dest: Path) -> None:
        """Stop tracing and save to dest (a .zip file)."""
        self._context.tracing.stop(path=str(dest))
        
    def close(self) -> None:
        """Close browser and stop playwright."""
        self._context.close()
        self._browser.close()
        self._pw.stop()
        
    def _attach_guards(self, page: Page, guard: PathGuard) -> None:
        """Attach route handler that blocks disallowed paths (I8)."""
        def route_handler(route):
            url = route.request.url
            if not guard.is_allowed(url):
                guard.blocked_count += 1
                route.abort()
            else:
                route.continue_()
        page.route("**/*", route_handler)
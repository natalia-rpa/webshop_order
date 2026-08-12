"""Playwright automation client for Hiab webshop batch order."""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from config_loader import load_config
from logging_setup import get_logger

# Import newly separated modules
from webshop.profil_handler import ProfileHandler
import webshop.login_handler as login_handler

logger = get_logger()   
PhaseCallback = Callable[[str, str], None]


class WebshopClient:
    """
    Orchestrates the webshop business logic.
    Delegates browser lifecycle to ProfileHandler and logins to login_handler.
    """

    def __init__(self, config=None, on_phase: Optional[PhaseCallback] = None):
        self.config = config or load_config()
        self.on_phase = on_phase or (lambda phase, detail: None)
        
        self.profile_handler = ProfileHandler(self.config)
        self.page: Optional[Page] = None
        self.base_url = self.config.get("webshop", "base_url")
        self.nav_timeout = self.config.getint("webshop", "navigation_timeout_ms", fallback=90000)
        self.timeout = self.config.getint("webshop", "default_timeout_ms", fallback=60000)

    def _phase(self, detail: str) -> None:
        """Helper to log and emit phase updates."""
        self.on_phase("PROCESSING", detail)
        logger.info(detail)

    def _wait_loaded(self, page: Optional[Page] = None, settle_s: float = 1.0) -> None:
        """Wait for document load state with a short sleep."""
        target = page or self.page
        assert target is not None
        try:
            target.wait_for_load_state("domcontentloaded", timeout=self.nav_timeout)
        except PlaywrightTimeout:
            pass
        try:
            target.wait_for_load_state("networkidle", timeout=min(self.nav_timeout, 30000))
        except PlaywrightTimeout:
            pass
        time.sleep(settle_s)

    def _safe_goto(self, url: str) -> None:
        """Navigate without failing on navigation race conditions."""
        assert self.page is not None
        current = (self.page.url or "").rstrip("/").lower()
        target = url.rstrip("/").lower()
        if current == target:
            self._wait_loaded(self.page, settle_s=0.5)
            return
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        except Exception:
            pass
        self._wait_loaded(self.page, settle_s=1.0)

    def start(self) -> None:
        """Starts browser via manager and navigates to the shop."""
        self.page = self.profile_handler.start()
        logger.info("Navigating to %s", self.base_url)
        self._safe_goto(self.base_url)

    def stop(self, *, keep_browser: bool = False) -> None:
        """Stops the browser session."""
        self.profile_handler.stop(keep_browser=keep_browser)
        self.page = None

    def __enter__(self) -> "WebshopClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def ensure_signed_in_session(self, wait_ms: int = 10_000) -> None:
        """Delegates auth check to standalone login_handler."""
        assert self.page is not None
        self._phase("Checking active session")
        self._safe_goto(self.base_url)
        login_handler.dismiss_cookie_banner(self.page)
        
        if login_handler.is_signed_in(self.page):
            logger.info("Active signed-in webshop session confirmed.")
            return

        logger.warning("Session expired. Trying automatic SSO login...")
        login_handler.login(self.page, self.config)
        
        if not login_handler.is_signed_in(self.page):
            raise RuntimeError("Failed to restore session automatically. Run: python main.py --login")

    def wait_until_impersonator_ready(self, timeout_ms: int = 600_000) -> None:
        """Blocks until the Find/Add user control is visible."""
        assert self.page is not None
        toggle = self.page.locator(login_handler.impersonator_toggle_selector()).first
        try:
            if toggle.count() > 0 and toggle.is_visible():
                return
        except Exception:
            pass
        logger.info("Waiting for manual login to finish...")
        toggle.wait_for(state="visible", timeout=timeout_ms)

    def _open_impersonator_toggle(self) -> None:
        assert self.page is not None
        selector = login_handler.impersonator_toggle_selector()

        for attempt in range(1, 4):
            login_handler.dismiss_cookie_banner(self.page)
            # Stop existing impersonation
            for exit_selector in ('[data-testid="impersonator-signout"] a', 'button:has-text("Stop impersonating")'):
                loc = self.page.locator(exit_selector)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    self._wait_loaded(self.page, settle_s=1.5)
            
            toggle = self.page.locator(selector).first
            try:
                toggle.wait_for(state="attached", timeout=12_000)
                toggle.click(force=True, timeout=5000)
                return
            except Exception:
                self._safe_goto(self.base_url)
        raise TimeoutError("Impersonator control not available after retries.")

    def run_batch_order(
        self,
        client_number: str,
        client_name: str,
        client_mail: str,
        batch_csvs: Sequence[str | Path],
        *,
        require_existing_session: bool = False,
    ) -> None:
        """Main flow: check session, impersonate client, create cart, and upload batches."""
        if not self.page:
            self.start()
        assert self.page is not None

        paths: List[Path] = [Path(p).resolve() for p in batch_csvs]

        if require_existing_session:
            self.ensure_signed_in_session()
        else:
            self._safe_goto(self.base_url)
            login_handler.dismiss_cookie_banner(self.page)
            if not login_handler.is_signed_in(self.page):
                logger.warning("Session is over — trying Login with SSO.")
                login_handler.restore_session_via_sso(self.page, self.profile_handler.context, self.config)

        self._impersonate_user(client_number, client_name)

        email_prefix = (client_mail or "").strip()[:8]
        cart_name = f"{datetime.datetime.now().strftime('%d%m%Y_%H%M')}_{email_prefix}"
        self._create_saved_cart(cart_name=cart_name)

        self._open_batch_order()

        total = len(paths)
        for index, csv_path in enumerate(paths, start=1):
            self._phase(f"Batch upload {index}/{total}: {csv_path.name}")
            if index > 1:
                self._ensure_batch_order_page()
            self._upload_and_add_to_cart(csv_path, batch_index=index, batch_total=total)

        logger.info("Batch order flow completed for client %s.", client_number)

    def _impersonate_user(self, client_number: str, client_name: str) -> None:
        assert self.page is not None
        self._phase(f"Finding user {client_number} {client_name}".strip())
        self._safe_goto(self.base_url)
        self._open_impersonator_toggle()
        time.sleep(0.3)

        search = self.page.locator('input.c-impersonator__input-search, input[id^="downshift-"]').first
        search.wait_for(state="visible")

        queries = [f"{client_number} {client_name}".strip()]
        if client_number:
            queries.append(client_number.strip())

        options = self.page.locator('[role="option"], .c-impersonator__menu-item')
        number_l = (client_number or "").casefold()
        name_l = (client_name or "").casefold()

        for query in queries:
            search.click()
            search.fill("")
            search.type(query, delay=40)

            try:
                options.first.wait_for(state="visible", timeout=12000)
            except PlaywrightTimeout:
                continue

            for index in range(options.count()):
                option = options.nth(index)
                text_l = (option.inner_text(timeout=2000) or "").casefold()
                if ((not number_l) or (number_l in text_l)) and ((not name_l) or (name_l in text_l)):
                    option.click()
                    self._wait_loaded(self.page, settle_s=2.0)
                    return

        raise TimeoutError(f"No impersonator option matched client_number={client_number}")

    def _create_saved_cart(self, cart_name: str) -> None:
        assert self.page is not None
        self._phase(f"Creating new cart: {cart_name}")
        time.sleep(2.0)
        
        try:
            cart_icon = self.page.locator('button.wishlist-toggle:visible, button.right-off-canvas-toggle[title*="Saved carts"]:visible').first
            cart_icon.wait_for(state="visible", timeout=5000)
            cart_icon.click(force=True)
            time.sleep(2.0)
        except Exception:
            pass

        self.page.get_by_role("button", name="Create new cart").first.click()
        name_input = self.page.get_by_role("textbox", name="Cart name").first
        name_input.wait_for(state="visible", timeout=5000)
        name_input.click()
        name_input.fill(cart_name)

        self.page.get_by_role("textbox", name="Cart description").first.fill("Hiabdeals generated by bot")
        self.page.get_by_role("button", name="Save", exact=True).first.click()
        self._wait_loaded(self.page, settle_s=1.5)

        close_btn = self.page.get_by_role("button", name="Close saved cart").first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click()
            time.sleep(0.5)

    def _open_batch_order(self) -> None:
        assert self.page is not None
        self._phase("Opening Batch Order")
        base = self.base_url.rstrip("/")
        batch_url = f"{base}/shop-by/batch/" if base.endswith("/en") else f"{base}/en/shop-by/batch/"
        self._safe_goto(batch_url)
        self._wait_loaded(self.page, settle_s=2.0)

    def _ensure_batch_order_page(self) -> None:
        assert self.page is not None
        if "/shop-by/batch" not in (self.page.url or ""):
            self._open_batch_order()

    def _upload_and_add_to_cart(self, csv_path: Path, batch_index: int = 1, batch_total: int = 1) -> None:
        assert self.page is not None
        self._phase(f"Uploading batch {batch_index}/{batch_total}: {csv_path.name}")
        
        file_input = self.page.locator('input[type="file"][name="file"], input.upload, input[type="file"]').first
        file_input.wait_for(state="attached", timeout=self.timeout)
        file_input.set_input_files(str(csv_path))
        
        try:
            file_input.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
        except Exception:
            pass
        time.sleep(1.5)

        self._phase(f"Adding batch {batch_index}/{batch_total} to cart")
        add_btn = self.page.locator("#batch-saved-card-add, button.batch-saved-card-add, button:has-text('add to saved cart')").first
        add_btn.wait_for(state="visible")
        
        deadline = time.time() + 90
        while time.time() < deadline:
            disabled = add_btn.get_attribute("disabled")
            if disabled is None and add_btn.is_enabled():
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(f"Add to cart stayed disabled after upload of {csv_path.name}.")

        add_btn.click()
        self._wait_loaded(self.page, settle_s=2.0)
"""Playwright automation for Hiab webshop batch order."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from config_loader import load_config, webshop_password
from logging_setup import get_logger

logger = get_logger()

PhaseCallback = Callable[[str, str], None]


class WebshopBot:
    """
    Browser flow:
    1. Open webshop -> Sign in
    2. Username / password -> Log in
    3. Passkey popup (Continue for hiabdeals@hiab.com)
    4. Focus back on web -> Verify (#savebtn) if shown
    5. Microsoft auth if still required
    6. Find user (impersonate)
    7. Batch Order uploads

    Session reuse: Playwright storage_state (cookies + localStorage JSON).
    """

    def __init__(
        self,
        config=None,
        on_phase: Optional[PhaseCallback] = None,
    ):
        self.config = config or load_config()
        self.on_phase = on_phase or (lambda phase, detail: None)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self.base_url = self.config.get("webshop", "base_url")
        self.login_url = self.config.get(
            "webshop",
            "login_url",
            fallback="https://webshop.hiab.com/en/login/ExternalLogin?ReturnUrl=/en/",
        )
        self.username = self.config.get("webshop", "username")
        self.password = webshop_password(self.config)
        self.headless = self.config.getboolean("webshop", "headless", fallback=False)
        self.slow_mo = self.config.getint("webshop", "slow_mo_ms", fallback=100)
        self.timeout = self.config.getint("webshop", "default_timeout_ms", fallback=60000)
        self.nav_timeout = self.config.getint(
            "webshop", "navigation_timeout_ms", fallback=90000
        )
        self.storage_state_path = self.config.get(
            "webshop",
            "storage_state_path",
            fallback="static/session/storage_state.json",
        ).strip() or "static/session/storage_state.json"

    def _phase(self, detail: str) -> None:
        self.on_phase("PROCESSING", detail)
        logger.info(detail)

    def _wait_loaded(self, page: Optional[Page] = None, settle_s: float = 1.0) -> None:
        """Wait for document load, then brief settle sleep."""
        target = page or self.page
        assert target is not None
        try:
            target.wait_for_load_state("domcontentloaded", timeout=self.nav_timeout)
        except PlaywrightTimeout:
            logger.warning("domcontentloaded wait timed out.")
        try:
            target.wait_for_load_state("networkidle", timeout=min(self.nav_timeout, 30000))
        except PlaywrightTimeout:
            logger.debug("networkidle not reached; continuing after settle sleep.")
        time.sleep(settle_s)

    def _save_storage_state(self) -> None:
        """Persist cookies + localStorage for the next run."""
        assert self.context is not None
        path = Path(self.storage_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(path))
        logger.info("Saved storage_state to %s", path)

    def start(self) -> Page:
        logger.info("Initializing browser...")
        self._playwright = sync_playwright().start()
        logger.info("Launching Chrome...")
        self._browser = self._playwright.chromium.launch(
            channel="chrome",
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",
                "--disable-save-password-bubble",
                "--disable-features=PasswordManagerOnboarding,PasswordCheck",
            ],
        )

        context_kwargs: dict = {
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 900},
        }
        state_path = Path(self.storage_state_path)
        if state_path.is_file():
            context_kwargs["storage_state"] = str(state_path)
            logger.info("Loading storage_state from %s", state_path)
        else:
            logger.info(
                "No storage_state at %s yet; will save after first successful login.",
                state_path,
            )

        self.context = self._browser.new_context(**context_kwargs)
        self.context.set_default_timeout(self.timeout)
        self.context.set_default_navigation_timeout(self.nav_timeout)
        self.page = self.context.new_page()
        logger.info("Browser ready; continuing with login steps.")
        self._goto_webshop()
        return self.page

    def _goto_webshop(self) -> None:
        """Open the webshop URL in the active browser tab."""
        assert self.page is not None
        url = self.base_url
        logger.info("Navigating to %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        self._wait_loaded(self.page, settle_s=1.0)
        logger.info("Opened: %s", self.page.url)

    def stop(self) -> None:
        try:
            if self.context is not None:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
        finally:
            self._browser = None
            self.context = None
            self.page = None
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            logger.info("Browser closed.")

    def __enter__(self) -> "WebshopBot":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def run_batch_order(
        self,
        client_number: str,
        client_name: str,
        batch_csvs: Sequence[str | Path],
    ) -> None:
        """
        Login once, impersonate, then upload each batch CSV and add to cart.
        Each file must already be <= batch_max_rows (typically 100).
        """
        if not self.page:
            self.start()
        assert self.page is not None

        paths: List[Path] = [Path(p).resolve() for p in batch_csvs]
        if not paths:
            raise ValueError("No batch CSV files provided.")
        for csv_path in paths:
            if not csv_path.exists():
                raise FileNotFoundError(f"Batch CSV not found: {csv_path}")

        if not self.password:
            raise ValueError(
                "Webshop password missing. Set [webshop] password in webshop_config.ini."
            )

        # Re-read password from config right before login (ignore stale values)
        self.config = load_config()
        self.username = self.config.get("webshop", "username")
        self.password = webshop_password(self.config)
        logger.info(
            "Using webshop login %s (password from config, length=%s, ends_with=%s).",
            self.username,
            len(self.password),
            self.password[-2:] if len(self.password) >= 2 else "?",
        )

        already_signed_in = self._login()
        if not already_signed_in:
            # Passkey dialog appears after Log in -> Continue -> back to web.
            self._handle_passkey_continue()
            self._click_verify_button()
            self._handle_microsoft_auth()
        self._save_storage_state()
        self._impersonate_user(client_number, client_name)
        self._open_batch_order()

        total = len(paths)
        for index, csv_path in enumerate(paths, start=1):
            self._phase(f"Batch upload {index}/{total}: {csv_path.name}")
            if index > 1:
                # Stay on / reopen Batch Order between uploads
                self._ensure_batch_order_page()
            self._upload_and_add_to_cart(csv_path, batch_index=index, batch_total=total)

        logger.info(
            "Batch order flow completed for client %s (%s file(s)).",
            client_number,
            total,
        )

    def _fill_exact(self, locator, value: str) -> None:
        """Clear field and fill, then verify (blocks Chrome password autofill overwrite)."""
        locator.click()
        locator.fill("")
        locator.press("Control+A")
        locator.press("Backspace")
        locator.fill(value)
        time.sleep(0.3)
        actual = locator.input_value()
        if actual != value:
            # Autofill overwrote — force via JS
            locator.evaluate(
                """(el, v) => {
                    el.focus();
                    el.value = '';
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            time.sleep(0.2)
            actual = locator.input_value()
        if actual != value:
            raise RuntimeError(
                f"Password/username field mismatch after fill "
                f"(expected length {len(value)}, got length {len(actual)})."
            )

    def _dismiss_cookie_banner(self, page: Optional[Page] = None) -> None:
        """OneTrust cookie banner blocks clicks (Sign in) until accepted/closed."""
        target = page or self.page
        assert target is not None
        for selector in (
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("Allow all")',
            "#onetrust-reject-all-handler",
            ".onetrust-close-btn-handler",
        ):
            loc = target.locator(selector)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    logger.info("Dismissed cookie banner via %s.", selector)
                    time.sleep(0.8)
                    return
            except Exception:
                continue
        try:
            target.evaluate(
                """() => {
                    const sdk = document.getElementById('onetrust-consent-sdk');
                    if (sdk) sdk.style.display = 'none';
                    document.querySelectorAll('.onetrust-pc-dark-filter')
                        .forEach(el => el.remove());
                }"""
            )
        except Exception:
            pass

    def _login(self) -> bool:
        """
        Sign in if needed.
        Returns True when an existing storage_state session is already signed in.
        """
        assert self.page is not None
        page = self.page

        self._phase(f"Opening webshop as {self.username}")
        page.goto(self.base_url, wait_until="domcontentloaded")
        self._wait_loaded(page, settle_s=1.5)
        self._dismiss_cookie_banner(page)

        if page.locator('[data-testid="impersonator-toggle-button"]').count() > 0:
            logger.info("Already signed in (storage_state session restored).")
            return True

        # Direct login URL avoids Sign-in click behind cookie overlay
        self._phase(f"Navigating to login for {self.username}")
        try:
            page.goto(self.login_url, wait_until="domcontentloaded")
            self._wait_loaded(page, settle_s=2.0)
            self._dismiss_cookie_banner(page)
        except Exception as exc:
            logger.warning("Direct login URL failed (%s); clicking Sign in.", exc)
            sign_in = page.locator(
                "a.l-s-header__link-user-nav", has_text="Sign in"
            ).first
            if sign_in.count() == 0:
                sign_in = page.locator('a[href*="/login/ExternalLogin"]').first
            try:
                sign_in.click(timeout=10000)
            except Exception:
                sign_in.click(force=True)
            self._wait_loaded(page, settle_s=2.0)

        self._phase(f"Entering credentials for {self.username}")
        user_input = page.locator(
            'input[placeholder="Username"], input[type="text"][required]'
        ).first
        pass_input = page.locator(
            'input[placeholder="Password"], input[type="password"]'
        ).first
        user_input.wait_for(state="visible")
        self._fill_exact(user_input, self.username)
        self._fill_exact(pass_input, self.password)
        logger.info(
            "Entered login credentials for %s (password length=%s).",
            self.username,
            len(self.password),
        )

        self._phase("Submitting login form")
        login_btn = page.locator(
            'button.loginButton, button:has-text("Log in")'
        ).first
        login_btn.click()
        self._wait_loaded(page, settle_s=2.0)
        logger.info("Clicked the Log in button.")
        time.sleep(1.5)
        return False

    def _handle_passkey_continue(self) -> None:
        """
        After Log in: Chrome passkey popup appears
        ("Use a saved passkey for hiab.my.salesforce.com").
        Focus it, press Continue, then focus back on the web page.
        """
        assert self.context is not None and self.page is not None
        self._phase("Passkey popup: focus and press Continue")

        deadline = time.time() + 40
        while time.time() < deadline:
            for pg in list(self.context.pages):
                url = (pg.url or "").lower()
                if any(
                    t in url
                    for t in (
                        "webauth",
                        "verification",
                        "passkey",
                        "salesforce.com",
                    )
                ):
                    try:
                        pg.bring_to_front()
                        self.page = pg
                    except Exception:
                        pass
                    break

            # Rare: Continue exists as HTML on the page
            if self._try_click_page_continue():
                logger.info("Clicked page-level Continue.")
                break

            # Chrome OS/browser passkey dialog (outside DOM)
            if self._press_browser_passkey_continue():
                logger.info("Pressed Continue on passkey dialog.")
                break

            time.sleep(0.8)
        else:
            logger.warning(
                "Passkey Continue not confirmed automatically. "
                "If the dialog is open, click Continue once."
            )
            time.sleep(10.0)

        time.sleep(1.5)
        self._focus_web_page()
        self._phase("Focus returned to web after passkey Continue")
        logger.info("Focus returned to web page after passkey Continue.")

    def _try_click_page_continue(self) -> bool:
        assert self.context is not None
        for pg in list(self.context.pages):
            for selector in (
                'button:has-text("Continue")',
                'input[type="submit"][value="Continue"]',
                '[role="button"]:has-text("Continue")',
            ):
                loc = pg.locator(selector).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        pg.bring_to_front()
                        loc.click(timeout=3000)
                        return True
                except Exception:
                    continue
        return False

    def _press_browser_passkey_continue(self) -> bool:
        """Chrome passkey dialog is outside DOM — use Windows UIA / Enter."""
        self._focus_chrome_window()
        if sys.platform == "win32":
            if self._win_click_button_by_name("Continue"):
                return True
            # Continue is the default primary button
            self._win_send_enter()
            time.sleep(0.6)
            self._win_send_enter()
            return True
        try:
            assert self.page is not None
            self.page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    def _focus_web_page(self) -> None:
        assert self.context is not None
        chosen: Optional[Page] = None
        for pg in list(self.context.pages):
            try:
                if pg.query_selector("input#savebtn, input[value='Verify']"):
                    chosen = pg
                    break
            except Exception:
                continue
        if chosen is None:
            for pg in reversed(list(self.context.pages)):
                url = (pg.url or "").lower()
                if url and url != "about:blank":
                    chosen = pg
                    break
        if chosen is None and self.context.pages:
            chosen = self.context.pages[0]
        if chosen is not None:
            self.page = chosen
            try:
                chosen.bring_to_front()
            except Exception:
                pass
        self._focus_chrome_window()

    def _focus_chrome_window(self) -> bool:
        if sys.platform != "win32":
            return False
        user32 = ctypes.windll.user32
        found: list = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            lower = title.lower()
            if (
                "chrome" in lower
                or "passkey" in lower
                or "salesforce" in lower
                or "chromium" in lower
            ):
                found.append((hwnd, title))
            return True

        user32.EnumWindows(enum_proc, 0)
        if not found:
            return False

        hwnd = found[0][0]
        for h, title in found:
            if "passkey" in title.lower():
                hwnd = h
                break

        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        return True

    @staticmethod
    def _win_send_enter() -> None:
        user32 = ctypes.windll.user32
        VK_RETURN = 0x0D
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

    def _win_click_button_by_name(self, name: str) -> bool:
        """Click accessible button (Continue) via Windows UI Automation."""
        safe = name.replace("'", "''")
        script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$nameCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NameProperty, '{safe}')
$btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $nameCond)
if ($null -eq $btn) {{ exit 2 }}
try {{
  $pattern = [System.Windows.Automation.InvokePattern]::Pattern
  $inv = $btn.GetCurrentPattern($pattern)
  $inv.Invoke()
  exit 0
}} catch {{
  exit 3
}}
"""
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info("UI Automation clicked %r.", name)
                return True
        except Exception as exc:
            logger.debug("UI Automation click failed: %s", exc)
        return False

    def _click_verify_button(self, page: Optional[Page] = None) -> None:
        """
        After Log in: press Salesforce Verify (#savebtn).
        Uses native DOM click / invokeSfdcApp() because Playwright click
        often skips inline onclick handlers.
        """
        assert self.context is not None
        target = page or self.page
        assert target is not None
        self._phase("Clicking Verify")

        deadline = time.time() + 45
        last_error: Optional[Exception] = None

        while time.time() < deadline:
            # New window may open after Log in — search every page + frame
            for pg in list(self.context.pages):
                try:
                    if self._try_press_verify_on(pg):
                        logger.info("Verify pressed on page: %s", pg.url)
                        self._wait_loaded(pg, settle_s=2.0)
                        # Keep focus on the page that advanced after Verify
                        self.page = pg
                        return
                except Exception as exc:
                    last_error = exc

            time.sleep(0.8)

        detail = f" Last error: {last_error}" if last_error else ""
        raise TimeoutError(
            'Verify button not pressed after Log in '
            '(expected <input id="savebtn" value="Verify">).'
            + detail
        )

    def _try_press_verify_on(self, page: Page) -> bool:
        """Return True if Verify was found and activated on this page (or a frame)."""
        scopes = [page] + list(page.frames)

        for scope in scopes:
            try:
                handle = scope.query_selector(
                    'input#savebtn, '
                    'input[name="save"][value="Verify"], '
                    'input.bluebutton[value="Verify"], '
                    'input[type="button"][value="Verify"]'
                )
            except Exception:
                continue

            if handle is None:
                continue

            try:
                visible = handle.is_visible()
            except Exception:
                visible = False

            if not visible:
                # Still try — some Salesforce screens mark it oddly
                logger.debug("Verify element found but not reported visible; trying anyway.")

            page.bring_to_front()
            try:
                handle.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass

            # 1) Native DOM click (fires inline onclick="invokeSfdcApp()")
            try:
                handle.evaluate("el => el.click()")
                logger.info("Fired native DOM click on Verify (#savebtn).")
            except Exception as exc:
                logger.warning("Native Verify click failed: %s", exc)

            time.sleep(0.5)

            # 2) Call the onclick handler directly if still on the same screen
            try:
                still_there = scope.query_selector("input#savebtn")
                if still_there is not None:
                    invoked = handle.evaluate(
                        """el => {
                            try {
                                if (typeof invokeSfdcApp === 'function') {
                                    invokeSfdcApp();
                                    return 'invokeSfdcApp';
                                }
                            } catch (e) {}
                            try {
                                if (typeof window.invokeSfdcApp === 'function') {
                                    window.invokeSfdcApp();
                                    return 'window.invokeSfdcApp';
                                }
                            } catch (e) {}
                            try {
                                el.dispatchEvent(new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                }));
                                return 'MouseEvent';
                            } catch (e) {}
                            return null;
                        }"""
                    )
                    if invoked:
                        logger.info("Invoked Verify handler via %s.", invoked)
            except Exception as exc:
                logger.debug("invokeSfdcApp fallback: %s", exc)

            # 3) Playwright click as last resort
            try:
                handle.click(force=True, timeout=5000)
                logger.info("Playwright force-click on Verify.")
            except Exception:
                pass

            time.sleep(1.0)
            return True

        return False

    def _handle_microsoft_auth(self) -> None:
        """
        Microsoft SSO if it still appears after passkey Continue + Verify.
        """
        assert self.context is not None and self.page is not None
        self._phase(f"Waiting for post-login session ({self.username})")

        deadline = time.time() + 120
        handled = False

        while time.time() < deadline:
            for popup in list(self.context.pages):
                url = (popup.url or "").lower()
                if not any(
                    host in url
                    for host in (
                        "login.microsoftonline.com",
                        "login.live.com",
                        "microsoft.com",
                        "account.microsoft",
                    )
                ):
                    continue

                logger.info("Microsoft auth window detected: %s", popup.url)
                try:
                    popup.bring_to_front()
                    self._wait_loaded(popup, settle_s=1.0)
                    if self._complete_microsoft_login(popup):
                        handled = True
                except Exception as exc:
                    logger.warning("Microsoft auth interaction issue: %s", exc)

            try:
                if self.page.locator(
                    '[data-testid="impersonator-toggle-button"]'
                ).count() > 0:
                    logger.info("Webshop session ready after auth.")
                    return
            except Exception:
                pass

            time.sleep(2.0 if handled else 1.0)

        if self.page.locator('[data-testid="impersonator-toggle-button"]').count() == 0:
            logger.warning(
                "Microsoft auth not confirmed automatically. "
                "Approve passkey / Continue for %s in the browser window.",
                self.username,
            )
            self.page.locator('[data-testid="impersonator-toggle-button"]').wait_for(
                state="visible", timeout=120000
            )
            logger.info("Webshop session ready (manual auth completed).")

    def _complete_microsoft_login(self, popup: Page) -> bool:
        """Select hiabdeals account / fill email / press Continue on MS login page."""
        acted = False

        account_tile = popup.locator(
            f'div[data-test-id="{self.username}"], '
            f'small:has-text("{self.username}"), '
            f'div[role="button"]:has-text("{self.username}")'
        ).first
        try:
            if account_tile.count() > 0 and account_tile.is_visible():
                account_tile.click(timeout=5000)
                logger.info("Selected Microsoft account tile: %s", self.username)
                acted = True
                time.sleep(1.0)
        except Exception:
            pass

        email_input = popup.locator(
            'input[name="loginfmt"], input[type="email"], input#i0116'
        ).first
        try:
            if email_input.count() > 0 and email_input.is_visible():
                email_input.fill("")
                email_input.fill(self.username)
                logger.info("Entered Microsoft email: %s", self.username)
                acted = True
                next_btn = popup.locator(
                    'input[type="submit"]#idSIButton9, '
                    'input[type="submit"][value="Next"], '
                    'button:has-text("Next")'
                ).first
                if next_btn.count() > 0 and next_btn.is_visible():
                    next_btn.click()
                    time.sleep(1.5)
        except Exception:
            pass

        ms_pass = popup.locator(
            'input[name="passwd"], input[type="password"], input#i0118'
        ).first
        try:
            if ms_pass.count() > 0 and ms_pass.is_visible() and self.password:
                self._fill_exact(ms_pass, self.password)
                logger.info("Entered Microsoft password for %s.", self.username)
                acted = True
        except Exception:
            pass

        for selector in (
            'input[type="submit"][value="Continue"]',
            'button:has-text("Continue")',
            'input[type="submit"]#idSIButton9',
            'input[type="submit"][value="Sign in"]',
            'button:has-text("Sign in")',
            'input[type="submit"]',
            'button[type="submit"]',
        ):
            loc = popup.locator(selector)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("Clicked Microsoft submit (%s).", selector)
                    acted = True
                    time.sleep(1.5)
                    break
            except Exception:
                continue

        try:
            yes_btn = popup.locator(
                'input#idSIButton9[value="Yes"], '
                'button:has-text("Yes"), '
                'input[type="submit"][value="Yes"]'
            ).first
            if yes_btn.count() > 0 and yes_btn.is_visible():
                yes_btn.click()
                logger.info("Clicked Microsoft Stay signed in: Yes.")
                acted = True
        except Exception:
            pass

        return acted

    def _impersonate_user(self, client_number: str, client_name: str) -> None:
        assert self.page is not None
        page = self.page

        self._phase(f"Finding user {client_number} {client_name}".strip())
        toggle = page.locator('[data-testid="impersonator-toggle-button"]')
        toggle.wait_for(state="visible")
        toggle.click()
        time.sleep(0.8)

        search = page.locator(
            'input[data-testid="impersonator-user-search-input"], '
            'input.c-impersonator__input-search, '
            "#downshift-0-input"
        ).first
        search.wait_for(state="visible")

        query = " ".join(part for part in (client_number, client_name) if part).strip()
        search.fill("")
        search.fill(query)
        logger.info("Entered impersonator search: %s", query)
        time.sleep(1.5)

        item = page.locator("#downshift-0-item-0, [id^='downshift-'][id$='-item-0']").first
        try:
            item.wait_for(state="visible", timeout=15000)
            item.click()
        except PlaywrightTimeout:
            option = page.locator(
                '[role="option"], .c-impersonator__menu-item, li[id*="item"]'
            ).first
            option.wait_for(state="visible", timeout=15000)
            option.click()

        self._wait_loaded(page, settle_s=2.0)
        logger.info("Selected impersonated user from list.")

    def _open_batch_order(self) -> None:
        assert self.page is not None
        page = self.page

        self._phase("Opening Batch Order")
        link = page.locator(
            'a.l-s-header__link-site-nav[href*="/shop-by/batch"], '
            'a[href="/en/shop-by/batch/"], '
            'a:has-text("Batch Order")'
        ).first
        link.wait_for(state="visible")
        link.click()
        self._wait_loaded(page, settle_s=2.0)
        logger.info("Opened Batch Order page.")

    def _ensure_batch_order_page(self) -> None:
        """Return to Batch Order if navigation left the page after Add to cart."""
        assert self.page is not None
        page = self.page
        file_input = page.locator(
            'input[type="file"][name="file"], input.upload, input[type="file"]'
        )
        if file_input.count() > 0 and "/shop-by/batch" in (page.url or ""):
            return
        self._open_batch_order()

    def _upload_and_add_to_cart(
        self,
        csv_path: Path,
        batch_index: int = 1,
        batch_total: int = 1,
    ) -> None:
        assert self.page is not None
        page = self.page

        self._phase(
            f"Uploading batch {batch_index}/{batch_total}: {csv_path.name}"
        )
        # Prefer Playwright set_input_files over OS file dialog (more reliable).
        file_input = page.locator(
            'input[type="file"][name="file"], input.upload, input[type="file"]'
        ).first
        file_input.wait_for(state="attached", timeout=self.timeout)
        file_input.set_input_files(str(csv_path))
        logger.info("Attached batch CSV via input[type=file]: %s", csv_path.name)
        time.sleep(2.0)

        self._phase(f"Adding batch {batch_index}/{batch_total} to cart")
        add_btn = page.locator(
            "#batch-add-to-cart, button.batch-add-to-cart, button:has-text('Add to cart')"
        ).first
        add_btn.wait_for(state="visible")

        deadline = time.time() + 60
        while time.time() < deadline:
            disabled = add_btn.get_attribute("disabled")
            aria_disabled = add_btn.get_attribute("aria-disabled")
            if disabled is None and aria_disabled not in ("true", "True"):
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(
                f"Add to cart stayed disabled after upload of {csv_path.name}."
            )

        add_btn.click()
        self._wait_loaded(page, settle_s=2.0)
        logger.info("Clicked Add to cart for batch %s/%s.", batch_index, batch_total)

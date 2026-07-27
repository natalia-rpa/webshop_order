"""Playwright automation for Hiab webshop batch order."""

from __future__ import annotations

import ctypes
import os
import socket
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
    4. Already in shop (Microsoft auth only if still required)
    5. Find user (impersonate)
    6. Batch Order uploads

    Session reuse: real Chrome on user_data_dir, Playwright attached via CDP
    (same profile as --login; avoids Playwright automation flags).
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
        self._chrome_proc: Optional[subprocess.Popen] = None
        self._cdp_port: Optional[int] = None
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
        self.user_data_dir = self.config.get(
            "webshop",
            "user_data_dir",
            fallback="static/browser_profile",
        ).strip() or "static/browser_profile"
        self.cdp_port = self.config.getint(
            "webshop", "cdp_port", fallback=0
        )

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

    @staticmethod
    def _clear_profile_locks(user_data_dir: Path) -> None:
        """Remove Chrome lock files left from a previous unclean exit."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            for path in (
                user_data_dir / name,
                user_data_dir / "Default" / name,
            ):
                try:
                    if path.exists() or path.is_symlink():
                        path.unlink(missing_ok=True)
                        logger.debug("Removed profile lock: %s", path)
                except OSError:
                    pass

    @staticmethod
    def _find_chrome_exe() -> Path:
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google/Chrome/Application/chrome.exe",
        ]
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError("chrome.exe not found")

    @staticmethod
    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def start(self) -> Page:
        """
        Launch real Chrome on the bot profile (same as --login), then attach
        Playwright over CDP. Avoids Playwright's automation flags that break
        the restored webshop/passkey session.
        """
        logger.info("Initializing browser...")
        profile = Path(self.user_data_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._clear_profile_locks(profile)
        logger.info("Using bot Chrome profile: %s", profile)

        chrome_exe = self._find_chrome_exe()
        port = self.cdp_port if self.cdp_port > 0 else self._pick_free_port()
        self._cdp_port = port

        chrome_args = [
            str(chrome_exe),
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--password-store=basic",
            "--disable-save-password-bubble",
        ]
        if self.headless:
            chrome_args.append("--headless=new")
        chrome_args.append(self.base_url)

        logger.info("Launching real Chrome (CDP port %s)...", port)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        self._chrome_proc = subprocess.Popen(
            chrome_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        self._playwright = sync_playwright().start()
        cdp_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 45
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            if self._chrome_proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited early (code {self._chrome_proc.returncode}). "
                    "Close other Hiab Bot Chrome windows and retry."
                )
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                break
            except Exception as exc:
                last_err = exc
                time.sleep(0.4)
        else:
            raise RuntimeError(
                f"Could not attach to Chrome CDP at {cdp_url}: {last_err}"
            )

        assert self._browser is not None
        if not self._browser.contexts:
            raise RuntimeError("Chrome CDP connected but no browser contexts found.")
        self.context = self._browser.contexts[0]
        self.context.set_default_timeout(self.timeout)
        self.context.set_default_navigation_timeout(self.nav_timeout)

        self.page = self._pick_webshop_page() or self.context.new_page()
        logger.info("Browser ready via CDP; continuing with login steps.")
        self._goto_webshop()
        return self.page

    def _pick_webshop_page(self) -> Optional[Page]:
        assert self.context is not None
        for pg in list(self.context.pages):
            url = (pg.url or "").lower()
            if "webshop.hiab.com" in url or "hiab.com" in url:
                return pg
        return self.context.pages[0] if self.context.pages else None

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
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
            elif self.context is not None:
                try:
                    self.context.close()
                except Exception:
                    pass
        finally:
            self._browser = None
            self.context = None
            self.page = None
            if self._chrome_proc is not None:
                try:
                    if self._chrome_proc.poll() is None:
                        self._chrome_proc.terminate()
                        try:
                            self._chrome_proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            self._chrome_proc.kill()
                except Exception:
                    pass
                self._chrome_proc = None
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

    def wait_until_impersonator_ready(self, timeout_ms: int = 600_000) -> None:
        """
        Block until the Find/Add user (impersonator) control is visible.
        Use for a one-time manual login bootstrap; session stays in user_data_dir.
        """
        assert self.page is not None
        toggle = self.page.locator('[data-testid="impersonator-toggle-button"]')
        if toggle.count() > 0 and toggle.first.is_visible():
            logger.info("Impersonator already visible — session is ready.")
            return

        logger.info(
            "Complete login manually in the Chrome window "
            "(passkey / Chrome verify / MFA as needed). "
            "Waiting up to %s min for impersonator (add user) button...",
            max(1, timeout_ms // 60_000),
        )
        toggle.first.wait_for(state="visible", timeout=timeout_ms)
        logger.info(
            "Impersonator visible — session saved in profile %s",
            Path(self.user_data_dir).resolve(),
        )

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
            # Passkey dialog appears after Log in -> Continue -> already in shop.
            self._handle_passkey_continue()
            self._handle_microsoft_auth()
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
        Returns True when an existing profile session is already signed in.
        """
        assert self.page is not None
        page = self.page

        self._phase(f"Opening webshop as {self.username}")
        page.goto(self.base_url, wait_until="domcontentloaded")
        self._wait_loaded(page, settle_s=1.5)
        self._dismiss_cookie_banner(page)

        if page.locator('[data-testid="impersonator-toggle-button"]').count() > 0:
            logger.info("Already signed in (Chrome profile session restored).")
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
        Microsoft SSO if it still appears after passkey Continue.
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
            "#downshift-0-input, "
            "input[id^='downshift-'][id$='-input']"
        ).first
        search.wait_for(state="visible")

        # Prefer client_number alone — full "number + name" often returns no hits.
        queries: List[str] = []
        if client_number:
            queries.append(client_number.strip())
        full = " ".join(part for part in (client_number, client_name) if part).strip()
        if full and full not in queries:
            queries.append(full)

        option = page.locator(
            "[id^='downshift-'][id*='-item-'], "
            '[role="option"], .c-impersonator__menu-item, li[id*="item"]'
        ).first

        last_query = ""
        for query in queries:
            last_query = query
            search.click()
            search.fill("")
            search.type(query, delay=40)
            logger.info("Entered impersonator search: %s", query)
            try:
                option.wait_for(state="visible", timeout=12000)
                option.click()
                self._wait_loaded(page, settle_s=2.0)
                logger.info("Selected impersonated user from list.")
                return
            except PlaywrightTimeout:
                logger.warning("No impersonator results for %r; trying next query.", query)

        raise TimeoutError(
            f"No impersonator user found for queries {queries!r} "
            f"(last tried {last_query!r})."
        )

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
        csv_path = Path(csv_path).resolve()
        if not csv_path.is_file():
            raise FileNotFoundError(f"Batch CSV not found: {csv_path}")

        self._phase(
            f"Uploading batch {batch_index}/{batch_total}: {csv_path.name}"
        )
        self._attach_batch_csv(page, csv_path)
        logger.info("Attached batch CSV via input[type=file]: %s", csv_path.name)

        self._phase(f"Adding batch {batch_index}/{batch_total} to cart")
        add_btn = page.locator(
            "#batch-add-to-cart, button.batch-add-to-cart, button:has-text('Add to cart')"
        ).first
        add_btn.wait_for(state="visible")

        deadline = time.time() + 90
        wait_started = time.time()
        redispatched = False
        while time.time() < deadline:
            if self._add_to_cart_enabled(add_btn):
                break
            # Site may still be parsing; re-dispatch change once mid-wait.
            if not redispatched and time.time() - wait_started > 10:
                self._dispatch_file_change(page)
                redispatched = True
            time.sleep(0.5)
        else:
            hint = self._batch_upload_error_hint(page)
            raise TimeoutError(
                f"Add to cart stayed disabled after upload of {csv_path.name}."
                + (f" Page hint: {hint}" if hint else "")
            )

        add_btn.click()
        self._wait_loaded(page, settle_s=2.0)
        logger.info("Clicked Add to cart for batch %s/%s.", batch_index, batch_total)

    def _attach_batch_csv(self, page: Page, csv_path: Path) -> None:
        """Attach CSV to Batch Order file input; fire change so the page enables Add to cart."""
        file_input = page.locator(
            'input[type="file"][name="file"], input.upload, input[type="file"]'
        ).first
        file_input.wait_for(state="attached", timeout=self.timeout)

        # Some themes hide the native input; keep it attachable for CDP.
        try:
            file_input.evaluate(
                """el => {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                    el.removeAttribute('hidden');
                    el.removeAttribute('disabled');
                }"""
            )
        except Exception:
            pass

        attached = False
        # Prefer native chooser (matches manual Browse click on this site).
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                clicked = False
                for selector in (
                    'label:has(input[type="file"][name="file"])',
                    'label:has(input.upload)',
                    ".upload-btn, .batch-upload, button:has-text('Browse')",
                    "button:has-text('Choose'), button:has-text('Upload')",
                    'input[type="file"][name="file"]',
                ):
                    loc = page.locator(selector).first
                    try:
                        if loc.count() > 0:
                            loc.click(timeout=2000, force=True)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    file_input.click(force=True, timeout=2000)
            fc_info.value.set_files(str(csv_path))
            attached = True
            logger.info("Attached CSV via file chooser.")
        except Exception as exc:
            logger.info("File chooser path skipped (%s); using set_input_files.", exc)

        if not attached:
            file_input.set_input_files(str(csv_path))

        self._dispatch_file_change(page)
        time.sleep(1.5)

        # Confirm the input actually holds a file.
        try:
            names = file_input.evaluate(
                "el => el.files ? Array.from(el.files).map(f => f.name) : []"
            )
            logger.info("File input now has: %s", names)
            if not names:
                # Retry set_input_files once more
                file_input.set_input_files(str(csv_path))
                self._dispatch_file_change(page)
                time.sleep(1.0)
                names = file_input.evaluate(
                    "el => el.files ? Array.from(el.files).map(f => f.name) : []"
                )
                logger.info("File input after retry: %s", names)
        except Exception as exc:
            logger.warning("Could not read file input files list: %s", exc)

    def _dispatch_file_change(self, page: Page) -> None:
        try:
            page.locator(
                'input[type="file"][name="file"], input.upload, input[type="file"]'
            ).first.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
        except Exception:
            pass

    @staticmethod
    def _add_to_cart_enabled(add_btn) -> bool:
        try:
            disabled = add_btn.get_attribute("disabled")
            aria_disabled = add_btn.get_attribute("aria-disabled")
            cls = (add_btn.get_attribute("class") or "").lower()
            if disabled is not None:
                return False
            if aria_disabled in ("true", "True"):
                return False
            if "disabled" in cls.split():
                return False
            return add_btn.is_enabled()
        except Exception:
            return False

    @staticmethod
    def _batch_upload_error_hint(page: Page) -> str:
        selectors = (
            ".error, .alert-error, .c-message--error, [role='alert'], "
            ".batch-error, .upload-error, .form-error"
        )
        try:
            loc = page.locator(selectors)
            texts = []
            for i in range(min(loc.count(), 5)):
                t = (loc.nth(i).inner_text(timeout=1000) or "").strip()
                if t:
                    texts.append(t)
            return " | ".join(texts)[:500]
        except Exception:
            return ""

# """Playwright automation for Hiab webshop batch order."""

# from __future__ import annotations

# import datetime
# import os
# import socket
# import subprocess
# import sys
# import time
# from pathlib import Path
# from typing import Callable, List, Optional, Sequence
# import psutil

# from playwright.sync_api import (
#     Browser,
#     BrowserContext,
#     Page,
#     Playwright,
#     TimeoutError as PlaywrightTimeout,
#     sync_playwright,
# )

# from config_loader import load_config, webshop_password
# from logging_setup import get_logger

# logger = get_logger()

# PhaseCallback = Callable[[str, str], None]

# class WebshopBot:
#     """
#     Browser flow:
#     1. Open webshop -> Sign in
#     2. Login with SSO (reuse Chrome/SSO session; skips password + Verify)
#     3. Already in shop (passkey / Microsoft only if SSO still prompts)
#     4. Find user (impersonate)
#     5. Batch Order uploads

#     Session reuse: real Chrome on user_data_dir, Playwright attached via CDP
#     (same profile as --login; avoids Playwright automation flags).
#     """

#     def __init__(
#         self,
#         config=None,
#         on_phase: Optional[PhaseCallback] = None,
#     ):
#         self.config = config or load_config()
#         self.on_phase = on_phase or (lambda phase, detail: None)
#         self._playwright: Optional[Playwright] = None
#         self._browser: Optional[Browser] = None
#         self._owns_chrome = False
#         self.context: Optional[BrowserContext] = None
#         self.page: Optional[Page] = None

#         self.base_url = self.config.get("webshop", "base_url")
#         self.login_url = self.config.get(
#             "webshop",
#             "login_url",
#             fallback="https://webshop.hiab.com/en/login/ExternalLogin?ReturnUrl=/en/",
#         )
#         self.username = self.config.get("webshop", "username")
#         self.password = webshop_password(self.config)
#         self.headless = self.config.getboolean("webshop", "headless", fallback=False)
#         self.slow_mo = self.config.getint("webshop", "slow_mo_ms", fallback=100)
#         self.timeout = self.config.getint("webshop", "default_timeout_ms", fallback=60000)
#         self.nav_timeout = self.config.getint(
#             "webshop", "navigation_timeout_ms", fallback=90000
#         )
#         self.user_data_dir = self.config.get(
#             "webshop",
#             "user_data_dir",
#             fallback="static/browser_profile",
#         ).strip() or "static/browser_profile"


#     def _phase(self, detail: str) -> None:
#         self.on_phase("PROCESSING", detail)
#         logger.info(detail)

#     def _wait_loaded(self, page: Optional[Page] = None, settle_s: float = 1.0) -> None:
#         """Wait for document load, then brief settle sleep."""
#         target = page or self.page
#         assert target is not None
#         try:
#             target.wait_for_load_state("domcontentloaded", timeout=self.nav_timeout)
#         except PlaywrightTimeout:
#             logger.warning("domcontentloaded wait timed out.")
#         try:
#             target.wait_for_load_state("networkidle", timeout=min(self.nav_timeout, 30000))
#         except PlaywrightTimeout:
#             logger.debug("networkidle not reached; continuing after settle sleep.")
#         time.sleep(settle_s)

#     @staticmethod
#     def _clear_profile_locks(user_data_dir: Path) -> None:
#         """Remove Chrome lock files left from a previous unclean exit."""
#         for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
#             for path in (
#                 user_data_dir / name,
#                 user_data_dir / "Default" / name,
#             ):
#                 try:
#                     if path.exists() or path.is_symlink():
#                         path.unlink(missing_ok=True)
#                         logger.debug("Removed profile lock: %s", path)
#                 except OSError:
#                     pass

#     @staticmethod
#     def _find_chrome_exe() -> Path:
#         candidates = [
#             Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
#             / "Google/Chrome/Application/chrome.exe",
#             Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
#             / "Google/Chrome/Application/chrome.exe",
#             Path(os.environ.get("LOCALAPPDATA", ""))
#             / "Google/Chrome/Application/chrome.exe",
#         ]
#         for path in candidates:
#             if path.is_file():
#                 return path
#         raise FileNotFoundError("chrome.exe not found")

#     @staticmethod
#     def _pick_free_port() -> int:
#         with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
#             sock.bind(("127.0.0.1", 0))
#             return int(sock.getsockname()[1])

#     def _stop_chrome_on_port(self, port: int) -> None:
#         """close chrome which belongs to bot (delete all processes)"""
#         logger.info("Searching for chrome processes on port %s...", port)
#         for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
#             try:
#                 name = proc.info['name']
#                 if name and name.lower() in ('chrome.exe', 'chrome'):
#                     cmdline = proc.info['cmdline']
#                     # close only if the port is in the command line
#                     if cmdline and any(f"--remote-debugging-port={port}" in arg for arg in cmdline):
#                         logger.info("Killing old bot process: PID %s", proc.info['pid'])
#                         proc.kill()
#             except (psutil.NoSuchProcess, psutil.AccessDenied):
#                 continue

#     def start(self) -> Page:
#         """Starts "normal" Chrome and attaches Playwright to it (allows MFA windows)."""
#         logger.info("Initializing browser...")
#         profile = Path(self.user_data_dir).resolve()
#         profile.mkdir(parents=True, exist_ok=True)
#         self._clear_profile_locks(profile)

#         # pick port and ensure it is free (kill old bot processes)
#         port = getattr(self, 'cdp_port', 9222)
#         if port <= 0:
#             port = self._pick_free_port()
#         self._cdp_port = port
#         self._stop_chrome_on_port(port)

#         chrome_exe = self._find_chrome_exe()
#         chrome_args = [
#             str(chrome_exe),
#             f"--user-data-dir={profile}",
#             "--profile-directory=Default",
#             f"--remote-debugging-port={port}",
#             "--no-first-run",
#             "--no-default-browser-check",
#             "--disable-session-crashed-bubble",
#             "--password-store=basic",
#             "--disable-save-password-bubble",
#         ]
#         if self.headless:
#             chrome_args.append("--headless=new")
#         chrome_args.append(self.base_url)

#         mode = "hidden (headless)" if self.headless else "visible"
#         logger.info("Launching Chrome %s (CDP port %s)...", mode, port)
        
#         # start physical Chrome
#         creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
#         if sys.platform == "win32" and self.headless:
#             creationflags |= subprocess.CREATE_NO_WINDOW
            
#         self._chrome_proc = subprocess.Popen(
#             chrome_args,
#             stdout=subprocess.DEVNULL,
#             stderr=subprocess.DEVNULL,
#             creationflags=creationflags,
#         )

#         # attach Playwright
#         self._playwright = sync_playwright().start()
#         cdp_url = f"http://127.0.0.1:{port}"
#         deadline = time.time() + 45
        
#         while time.time() < deadline:
#             try:
#                 self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
#                 break
#             except Exception:
#                 time.sleep(0.5)
#         else:
#             raise RuntimeError(f"Could not attach to Chrome CDP at {cdp_url}")

#         self.context = self._browser.contexts[0]
#         self.context.set_default_timeout(self.timeout)
#         self.context.set_default_navigation_timeout(self.nav_timeout)

#         self.page = self._pick_webshop_page() or self.context.pages[0]
#         logger.info("Browser ready via CDP.")
#         self._goto_webshop()
#         return self.page


#     def _pick_webshop_page(self) -> Optional[Page]:
#         assert self.context is not None
#         for pg in list(self.context.pages):
#             url = (pg.url or "").lower()
#             if "webshop.hiab.com" in url or "hiab.com" in url:
#                 return pg
#         return self.context.pages[0] if self.context.pages else None
        

#     def _goto_webshop(self) -> None:
#         """Open the webshop URL in the active browser tab."""
#         assert self.page is not None
#         url = self.base_url
#         logger.info("Navigating to %s", url)
#         self._safe_goto(url)
#         logger.info("Opened: %s", self.page.url)

#     def stop(self, *, keep_browser: bool = False) -> None:
#         """Disconnects the bot. keep_browser=True leaves Chrome open (required for login)."""
#         try:
#             if self._browser is not None:
#                 self._browser.close()
#         except Exception:
#             pass

#         self._browser = None
#         self.context = None
#         self.page = None

#         # close physical Chrome if it was not manual login
#         if not keep_browser and self._chrome_proc is not None:
#             try:
#                 if self._chrome_proc.poll() is None:
#                     self._chrome_proc.terminate()
#             except Exception:
#                 pass
#             self._chrome_proc = None
#             logger.info("Browser closed.")
#         else:
#             logger.info("Disconnected from Playwright. Chrome stays open.")

#         if getattr(self, '_playwright', None):
#             try:
#                 self._playwright.stop()
#             except Exception:
#                 pass
#             self._playwright = None



#     def __enter__(self) -> "WebshopBot":
#         self.start()
#         return self

#     def __exit__(self, exc_type, exc, tb) -> None:
#         self.stop()

#     def is_signed_in(self) -> bool:
#         """True when signed-in UI is present (impersonator control preferred)."""
#         assert self.page is not None
#         page = self.page
#         try:
#             toggle = page.locator(self._impersonator_toggle_selector())
#             if toggle.count() > 0:
#                 return True

#             for selector in (
#                 'a.l-s-header__link-user-nav:has-text("Sign out")',
#                 'button:has-text("Sign out")',
#                 '[data-testid="impersonator"]',
#                 ".c-impersonator",
#             ):
#                 loc = page.locator(selector)
#                 if loc.count() > 0:
#                     return True

#             sign_in = page.locator(
#                 'a.l-s-header__link-user-nav:has-text("Sign in"), '
#                 'a[href*="/login/ExternalLogin"]'
#             )
#             if sign_in.count() > 0:
#                 try:
#                     return not sign_in.first.is_visible()
#                 except Exception:
#                     return False
#             return False
#         except Exception:
#             return False

#     @staticmethod
#     def _impersonator_toggle_selector() -> str:
#         return (
#             '[data-testid="impersonator-toggle-button"], '
#             'button[data-testid="impersonator-toggle-button"], '
#             'button.c-impersonator__toggle, '
#             'button:has-text("Add user"), '
#             'button:has-text("Find user")'
#         )

#     def _stop_impersonation_if_needed(self) -> None:
#         """Leave a previous customer impersonation so Find user works again."""
#         assert self.page is not None
#         page = self.page
#         for selector in (
#             '[data-testid="impersonator-signout"] a',
#             '[data-testid="impersonator-signout"]',
#             "div.c-impersonator__signout a",
#             "div.c-impersonator__signout",
#             'a:has-text("Sign out as user")',
#             'button:has-text("Stop impersonating")',
#             'button:has-text("Exit impersonation")',
#             'a:has-text("Stop impersonating")',
#             '[data-testid="impersonator-stop-button"]',
#             'button.c-impersonator__stop',
#         ):
#             loc = page.locator(selector)
#             try:
#                 if loc.count() == 0:
#                     continue
#                 target = loc.first
#                 if not target.is_visible():
#                     continue
#                 target.click(timeout=5000)
#                 logger.info("Signed out as impersonated user via %s.", selector)
#                 self._wait_loaded(page, settle_s=1.5)
#                 return
#             except Exception:
#                 continue

#     def _open_impersonator_toggle(self) -> None:
#         """Open the Find/Add user control; recover with reload if needed."""
#         assert self.page is not None
#         page = self.page
#         selector = self._impersonator_toggle_selector()

#         for attempt in range(1, 4):
#             self._dismiss_cookie_banner(page)
#             self._stop_impersonation_if_needed()
#             toggle = page.locator(selector).first
#             try:
#                 # Prefer visible; fall back to attached + force (header can be sticky/obscured).
#                 try:
#                     toggle.wait_for(state="visible", timeout=12_000)
#                     toggle.click(timeout=5000)
#                 except PlaywrightTimeout:
#                     if toggle.count() == 0:
#                         raise
#                     logger.warning(
#                         "Impersonator toggle not visible (attempt %s); force-clicking.",
#                         attempt,
#                     )
#                     toggle.wait_for(state="attached", timeout=5_000)
#                     toggle.click(force=True, timeout=5000)

#                 search = page.locator(
#                     'input[data-testid="impersonator-user-search-input"], '
#                     'input.c-impersonator__input-search, '
#                     "#downshift-0-input, "
#                     "input[id^='downshift-'][id$='-input']"
#                 ).first
#                 search.wait_for(state="visible", timeout=8_000)
#                 return
#             except Exception as exc:
#                 logger.warning(
#                     "Could not open impersonator (attempt %s/%s, url=%s): %s",
#                     attempt,
#                     3,
#                     page.url,
#                     exc,
#                 )
#                 self._safe_goto(self.base_url)
#                 try:
#                     page.reload(wait_until="domcontentloaded", timeout=self.nav_timeout)
#                 except Exception:
#                     pass
#                 self._wait_loaded(page, settle_s=1.5)

#         raise TimeoutError(
#             "Impersonator (Find/Add user) control not available after retries. "
#             f"url={page.url!r}. If the header looks wrong, run: python main.py --login"
#         )

#     def ensure_signed_in_session(self) -> None:
#         """
#         Confirm the shared Chrome session is signed in.
#         If the session is over, try Login with SSO to restore it.
#         """
#         assert self.page is not None
#         self._phase(f"Checking active session for {self.username}")
#         self._safe_goto(self.base_url)
#         self._dismiss_cookie_banner(self.page)

#         if self.is_signed_in():
#             logger.info("Active signed-in webshop session confirmed.")
#             return

#         logger.warning("Session expired. Trying automatic SSO login...")
#         self._login()
        
#         if not self.is_signed_in():
#             raise RuntimeError(
#                 "Failed to restore session automatically. "
#                 "Run: python main.py --login, to login manually."
#             )

#     def _restore_session_via_sso(self) -> None:
#         """Session expired: Login with SSO, then leftover auth prompts if needed."""
#         already_signed_in = self._login()
#         if not already_signed_in:
#             self._handle_passkey_continue()
#             self._handle_microsoft_auth()

#         assert self.page is not None
#         if not self.is_signed_in():
#             self._safe_goto(self.base_url)
#             self._dismiss_cookie_banner(self.page)

#         if self.is_signed_in():
#             logger.info("Session restored via Login with SSO.")
#             return

#         raise RuntimeError(
#             "Webshop session is not active after Login with SSO "
#             "(impersonator not visible). "
#             "Run: python main.py --login  then complete sign-in / MFA. "
#             f"(current url={self.page.url!r})"
#         )

#     def wait_until_impersonator_ready(self, timeout_ms: int = 600_000) -> None:
#         """
#         Block until the Find/Add user (impersonator) control is visible.
#         Use for a one-time manual login bootstrap; leave Chrome open afterward.
#         """
#         assert self.page is not None
#         toggle = self.page.locator(self._impersonator_toggle_selector()).first
#         try:
#             if toggle.count() > 0 and toggle.is_visible():
#                 logger.info("Impersonator already visible — session is ready.")
#                 return
#         except Exception:
#             pass

#         logger.info(
#             "Complete login manually in the Chrome window "
#             "(passkey / Chrome verify / MFA as needed). "
#             "Waiting up to %s min for impersonator (add user) button...",
#             max(1, timeout_ms // 60_000),
#         )
#         toggle.wait_for(state="visible", timeout=timeout_ms)
#         logger.info(
#             "Impersonator visible — active session ready (profile %s)",
#             Path(self.user_data_dir).resolve(),
#         )

#     def _safe_goto(self, url: str) -> None:
#         """Navigate without failing when Chrome races another same-URL load."""
#         assert self.page is not None
#         page = self.page
#         current = (page.url or "").rstrip("/").lower()
#         target = url.rstrip("/").lower()
#         if current == target:
#             try:
#                 page.wait_for_load_state("domcontentloaded", timeout=10_000)
#             except PlaywrightTimeout:
#                 pass
#             self._wait_loaded(page, settle_s=0.5)
#             return
#         try:
#             page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
#         except Exception as exc:
#             msg = str(exc).lower()
#             if "interrupted by another navigation" in msg:
#                 logger.debug("Navigation race ignored: %s", exc)
#                 try:
#                     page.wait_for_load_state("domcontentloaded", timeout=15_000)
#                 except PlaywrightTimeout:
#                     pass
#             else:
#                 raise
#         self._wait_loaded(page, settle_s=1.0)

#     def run_batch_order(
#         self,
#         client_number: str,
#         client_name: str,
#         client_mail: str,
#         batch_csvs: Sequence[str | Path],
#         *,
#         require_existing_session: bool = False,
#     ) -> None:
#         """
#         Reuse active session when signed in; if session is over, Login with SSO.
#         Then impersonate, upload each batch CSV and add to cart.
#         Each file must already be <= batch_max_rows.
#         """
#         if not self.page:
#             self.start()
#         assert self.page is not None

#         paths: List[Path] = [Path(p).resolve() for p in batch_csvs]
#         if not paths:
#             raise ValueError("No batch CSV files provided.")
#         for csv_path in paths:
#             if not csv_path.exists():
#                 raise FileNotFoundError(f"Batch CSV not found: {csv_path}")

#         # Active session → continue. Session over → Login with SSO.
#         if require_existing_session:
#             # Wait for header settle / reload, then SSO if still expired.
#             self.ensure_signed_in_session()
#         else:
#             self._safe_goto(self.base_url)
#             self._dismiss_cookie_banner(self.page)
#             if self.is_signed_in():
#                 logger.info("Active session OK — skipping login.")
#             else:
#                 logger.warning("Session is over — trying Login with SSO.")
#                 self._restore_session_via_sso()

#         self._impersonate_user(client_number, client_name)

#         email_prefix = (client_mail or "").strip()[:8]
#         cart_name = f"{datetime.datetime.now().strftime('%d%m%Y_%H%M')}_{email_prefix}"
#         self._create_saved_cart(cart_name=cart_name)

#         self._open_batch_order()

#         total = len(paths)
#         for index, csv_path in enumerate(paths, start=1):
#             self._phase(f"Batch upload {index}/{total}: {csv_path.name}")
#             if index > 1:
#                 # Stay on / reopen Batch Order between uploads
#                 self._ensure_batch_order_page()
#             self._upload_and_add_to_cart(csv_path, batch_index=index, batch_total=total)

#         logger.info(
#             "Batch order flow completed for client %s (%s file(s)).",
#             client_number,
#             total,
#         )

#     def _fill_exact(self, locator, value: str) -> None:
#         """Clear field and fill, then verify (blocks Chrome password autofill overwrite)."""
#         locator.click()
#         locator.fill("")
#         locator.press("Control+A")
#         locator.press("Backspace")
#         locator.fill(value)
#         time.sleep(0.3)
#         actual = locator.input_value()
#         if actual != value:
#             # Autofill overwrote — force via JS
#             locator.evaluate(
#                 """(el, v) => {
#                     el.focus();
#                     el.value = '';
#                     el.value = v;
#                     el.dispatchEvent(new Event('input', { bubbles: true }));
#                     el.dispatchEvent(new Event('change', { bubbles: true }));
#                 }""",
#                 value,
#             )
#             time.sleep(0.2)
#             actual = locator.input_value()
#         if actual != value:
#             raise RuntimeError(
#                 f"Password/username field mismatch after fill "
#                 f"(expected length {len(value)}, got length {len(actual)})."
#             )

#     def _dismiss_cookie_banner(self, page: Optional[Page] = None) -> None:
#         """OneTrust cookie banner blocks clicks (Sign in) until accepted/closed."""
#         target = page or self.page
#         assert target is not None
#         for selector in (
#             "#onetrust-accept-btn-handler",
#             "button#onetrust-accept-btn-handler",
#             'button:has-text("Accept All")',
#             'button:has-text("Accept all")',
#             'button:has-text("Allow all")',
#             "#onetrust-reject-all-handler",
#             ".onetrust-close-btn-handler",
#         ):
#             loc = target.locator(selector)
#             try:
#                 if loc.count() > 0 and loc.first.is_visible():
#                     loc.first.click(timeout=3000)
#                     logger.info("Dismissed cookie banner via %s.", selector)
#                     time.sleep(0.8)
#                     return
#             except Exception:
#                 continue
#         try:
#             target.evaluate(
#                 """() => {
#                     const sdk = document.getElementById('onetrust-consent-sdk');
#                     if (sdk) sdk.style.display = 'none';
#                     document.querySelectorAll('.onetrust-pc-dark-filter')
#                         .forEach(el => el.remove());
#                 }"""
#             )
#         except Exception:
#             pass

#     def _login(self) -> bool:
#         """
#         Sign in if needed via Login with SSO (skips password + Verify when
#         the Chrome profile already has an active SSO session).
#         Returns True when already signed in (or SSO completed into shop).
#         """
#         assert self.page is not None
#         page = self.page

#         self._phase(f"Opening webshop as {self.username}")
#         self._safe_goto(self.base_url)
#         self._dismiss_cookie_banner(page)

#         if self.is_signed_in():
#             logger.info("Already signed in (Chrome profile session restored).")
#             return True

#         # Direct login URL avoids Sign-in click behind cookie overlay
#         self._phase(f"Navigating to login for {self.username}")
#         try:
#             self._safe_goto(self.login_url)
#             self._dismiss_cookie_banner(page)
#         except Exception as exc:
#             logger.warning("Direct login URL failed (%s); clicking Sign in.", exc)
#             sign_in = page.locator(
#                 "a.l-s-header__link-user-nav", has_text="Sign in"
#             ).first
#             if sign_in.count() == 0:
#                 sign_in = page.locator('a[href*="/login/ExternalLogin"]').first
#             try:
#                 sign_in.click(timeout=10000)
#             except Exception:
#                 sign_in.click(force=True)
#             self._wait_loaded(page, settle_s=2.0)

#         self._phase("Clicking Login with SSO")
#         sso_btn = page.locator(
#             'button.sso-button, '
#             'button.slds-button.sso-button, '
#             'button:has-text("Login with SSO")'
#         ).first
#         sso_btn.wait_for(state="visible", timeout=20000)
#         try:
#             sso_btn.click(timeout=10000)
#         except Exception:
#             sso_btn.click(force=True)
#         logger.info("Clicked Login with SSO.")
#         self._wait_loaded(page, settle_s=2.0)
#         time.sleep(1.5)

#         # SSO reuses existing session — often lands already in the shop.
#         deadline = time.time() + 45
#         while time.time() < deadline:
#             if self.is_signed_in():
#                 logger.info("Signed in via Login with SSO (verification skipped).")
#                 return True
#             time.sleep(0.8)

#         logger.warning(
#             "Login with SSO clicked but signed-in state not confirmed yet; "
#             "continuing with any leftover auth prompts."
#         )
#         return False

#     def _handle_passkey_continue(self) -> None:
#         """
#         After Log in: Chrome passkey popup appears
#         ("Use a saved passkey for hiab.my.salesforce.com").
#         Focus it, press Continue, then focus back on the web page.
#         """
#         assert self.context is not None and self.page is not None
#         self._phase("Passkey popup: focus and press Continue")

#         deadline = time.time() + 40
#         while time.time() < deadline:
#             for pg in list(self.context.pages):
#                 url = (pg.url or "").lower()
#                 if any(
#                     t in url
#                     for t in (
#                         "webauth",
#                         "verification",
#                         "passkey",
#                         "salesforce.com",
#                     )
#                 ):
#                     try:
#                         pg.bring_to_front()
#                         self.page = pg
#                     except Exception:
#                         pass
#                     break

#             # Rare: Continue exists as HTML on the page
#             if self._try_click_page_continue():
#                 logger.info("Clicked page-level Continue.")
#                 break


#             time.sleep(0.8)
#         else:
#             logger.warning(
#                 "Passkey Continue not confirmed automatically. "
#                 "If the dialog is open, click Continue once."
#             )
#             time.sleep(10.0)

#         time.sleep(1.5)
#         self._focus_web_page()
#         self._phase("Focus returned to web after passkey Continue")
#         logger.info("Focus returned to web page after passkey Continue.")

#     def _try_click_page_continue(self) -> bool:
#         assert self.context is not None
#         for pg in list(self.context.pages):
#             for selector in (
#                 'button:has-text("Continue")',
#                 'input[type="submit"][value="Continue"]',
#                 '[role="button"]:has-text("Continue")',
#             ):
#                 loc = pg.locator(selector).first
#                 try:
#                     if loc.count() > 0 and loc.is_visible():
#                         pg.bring_to_front()
#                         loc.click(timeout=3000)
#                         return True
#                 except Exception:
#                     continue
#         return False

   

#     def _focus_web_page(self) -> None:
#         assert self.context is not None
#         chosen: Optional[Page] = None
#         for pg in list(self.context.pages):
#             try:
#                 if pg.query_selector("input#savebtn, input[value='Verify']"):
#                     chosen = pg
#                     break
#             except Exception:
#                 continue
#         if chosen is None:
#             for pg in reversed(list(self.context.pages)):
#                 url = (pg.url or "").lower()
#                 if url and url != "about:blank":
#                     chosen = pg
#                     break
#         if chosen is None and self.context.pages:
#             chosen = self.context.pages[0]
#         if chosen is not None:
#             self.page = chosen
#             try:
#                 chosen.bring_to_front()
#             except Exception:
#                 pass
   

#     def _handle_microsoft_auth(self) -> None:
#         """
#         Microsoft SSO if it still appears after passkey Continue.
#         """
#         assert self.context is not None and self.page is not None
#         self._phase(f"Waiting for post-login session ({self.username})")

#         deadline = time.time() + 120
#         handled = False

#         while time.time() < deadline:
#             for popup in list(self.context.pages):
#                 url = (popup.url or "").lower()
#                 if not any(
#                     host in url
#                     for host in (
#                         "login.microsoftonline.com",
#                         "login.live.com",
#                         "microsoft.com",
#                         "account.microsoft",
#                     )
#                 ):
#                     continue

#                 logger.info("Microsoft auth window detected: %s", popup.url)
#                 try:
#                     popup.bring_to_front()
#                     self._wait_loaded(popup, settle_s=1.0)
#                     if self._complete_microsoft_login(popup):
#                         handled = True
#                 except Exception as exc:
#                     logger.warning("Microsoft auth interaction issue: %s", exc)

#             try:
#                 if self.page.locator(
#                     '[data-testid="impersonator-toggle-button"]'
#                 ).count() > 0:
#                     logger.info("Webshop session ready after auth.")
#                     return
#             except Exception:
#                 pass

#             time.sleep(2.0 if handled else 1.0)

#         if self.page.locator('[data-testid="impersonator-toggle-button"]').count() == 0:
#             logger.warning(
#                 "Microsoft auth not confirmed automatically. "
#                 "Approve passkey / Continue for %s in the browser window.",
#                 self.username,
#             )
#             self.page.locator('[data-testid="impersonator-toggle-button"]').wait_for(
#                 state="visible", timeout=120000
#             )
#             logger.info("Webshop session ready (manual auth completed).")

#     def _complete_microsoft_login(self, popup: Page) -> bool:
#         """Select hiabdeals account / fill email / press Continue on MS login page."""
#         acted = False

#         account_tile = popup.locator(
#             f'div[data-test-id="{self.username}"], '
#             f'small:has-text("{self.username}"), '
#             f'div[role="button"]:has-text("{self.username}")'
#         ).first
#         try:
#             if account_tile.count() > 0 and account_tile.is_visible():
#                 account_tile.click(timeout=5000)
#                 logger.info("Selected Microsoft account tile: %s", self.username)
#                 acted = True
#                 time.sleep(1.0)
#         except Exception:
#             pass

#         email_input = popup.locator(
#             'input[name="loginfmt"], input[type="email"], input#i0116'
#         ).first
#         try:
#             if email_input.count() > 0 and email_input.is_visible():
#                 email_input.fill("")
#                 email_input.fill(self.username)
#                 logger.info("Entered Microsoft email: %s", self.username)
#                 acted = True
#                 next_btn = popup.locator(
#                     'input[type="submit"]#idSIButton9, '
#                     'input[type="submit"][value="Next"], '
#                     'button:has-text("Next")'
#                 ).first
#                 if next_btn.count() > 0 and next_btn.is_visible():
#                     next_btn.click()
#                     time.sleep(1.5)
#         except Exception:
#             pass

#         ms_pass = popup.locator(
#             'input[name="passwd"], input[type="password"], input#i0118'
#         ).first
#         try:
#             if ms_pass.count() > 0 and ms_pass.is_visible() and self.password:
#                 self._fill_exact(ms_pass, self.password)
#                 logger.info("Entered Microsoft password for %s.", self.username)
#                 acted = True
#         except Exception:
#             pass

#         for selector in (
#             'input[type="submit"][value="Continue"]',
#             'button:has-text("Continue")',
#             'input[type="submit"]#idSIButton9',
#             'input[type="submit"][value="Sign in"]',
#             'button:has-text("Sign in")',
#             'input[type="submit"]',
#             'button[type="submit"]',
#         ):
#             loc = popup.locator(selector)
#             try:
#                 if loc.count() > 0 and loc.first.is_visible():
#                     loc.first.click()
#                     logger.info("Clicked Microsoft submit (%s).", selector)
#                     acted = True
#                     time.sleep(1.5)
#                     break
#             except Exception:
#                 continue

#         try:
#             yes_btn = popup.locator(
#                 'input#idSIButton9[value="Yes"], '
#                 'button:has-text("Yes"), '
#                 'input[type="submit"][value="Yes"]'
#             ).first
#             if yes_btn.count() > 0 and yes_btn.is_visible():
#                 yes_btn.click()
#                 logger.info("Clicked Microsoft Stay signed in: Yes.")
#                 acted = True
#         except Exception:
#             pass

#         return acted

#     def _impersonate_user(self, client_number: str, client_name: str) -> None:
#         assert self.page is not None
#         page = self.page

#         self._phase(f"Finding user {client_number} {client_name}".strip())
#         # Always start from home — leftover impersonation / bad nav hides the toggle.
#         self._safe_goto(self.base_url)
#         self._open_impersonator_toggle()
#         time.sleep(0.3)

#         search = page.locator(
#             'input[data-testid="impersonator-user-search-input"], '
#             'input.c-impersonator__input-search, '
#             "#downshift-0-input, "
#             "input[id^='downshift-'][id$='-input']"
#         ).first
#         search.wait_for(state="visible")

#         # Prefer full "number + name"; fall back to client_number alone.
#         queries: List[str] = []
#         full = " ".join(part for part in (client_number, client_name) if part).strip()
#         if full:
#             queries.append(full)
#         if client_number:
#             number_only = client_number.strip()
#             if number_only and number_only not in queries:
#                 queries.append(number_only)
#         if not queries:
#             raise ValueError("client_number and client_name are both empty.")

#         options = page.locator(
#             "[id^='downshift-'][id*='-item-'], "
#             '[role="option"], .c-impersonator__menu-item, li[id*="item"]'
#         )
#         number_l = (client_number or "").casefold()
#         name_l = (client_name or "").casefold()
#         last_query = ""
#         last_count = 0

#         for query in queries:
#             last_query = query
#             search.click()
#             search.fill("")
#             search.type(query, delay=40)
#             logger.info("Entered impersonator search: %s", query)

#             try:
#                 options.first.wait_for(state="visible", timeout=12000)
#             except PlaywrightTimeout:
#                 logger.info("No impersonator results for query %r; trying next.", query)
#                 continue

#             last_count = options.count()
#             for index in range(last_count):
#                 option = options.nth(index)
#                 try:
#                     text = (option.inner_text(timeout=2000) or "").strip()
#                 except PlaywrightTimeout:
#                     continue
#                 text_l = text.casefold()
#                 number_ok = (not number_l) or (number_l in text_l)
#                 name_ok = (not name_l) or (name_l in text_l)
#                 if number_ok and name_ok:
#                     option.click()
#                     self._wait_loaded(page, settle_s=2.0)
#                     logger.info("Selected impersonated user: %s", text)
#                     return

#         raise TimeoutError(
#             f"No impersonator option matched client_number={client_number!r} "
#             f"and client_name={client_name!r} "
#             f"(last query {last_query!r}, {last_count} result(s))."
#         )

#     def _create_saved_cart(self, cart_name: str) -> None:
#         """Open cart menu, create new 'Saved cart' and close the window."""
#         assert self.page is not None
#         page = self.page

#         self._phase(f"Creating new cart: {cart_name}")
#         time.sleep(2.0)
#         # 1. Click on cart icon (ignore "4", search for cart link/button)
#         try:
#             cart_icon = page.locator(
#                 'button.wishlist-toggle:visible, '
#                 'button.right-off-canvas-toggle[title*="Saved carts"]:visible'
#             ).first
#             cart_icon.wait_for(state="visible", timeout=5000)
            
#             # force=True forces click ignoring weird CSS layers
#             cart_icon.click(force=True)
#             logger.info("TIK TAK CART ICON - clicked")
#             time.sleep(2.0)
#         except Exception as exc:
#             logger.warning("Failed to click on cart icon (maybe it is already open): %s", exc)

#         # 2. Click on 'Create new cart'
#         create_btn = page.get_by_role("button", name="Create new cart").first
#         create_btn.wait_for(state="visible", timeout=10000)
#         create_btn.click()

#         # 3. Fill cart data
#         name_input = page.get_by_role("textbox", name="Cart name").first
#         name_input.wait_for(state="visible", timeout=5000)
        
#         # clear field and enter name
#         name_input.click()
#         name_input.fill(cart_name)


#         logger.info("Entered cart name: %s", cart_name)

#         desc_input = page.get_by_role("textbox", name="Cart description").first
#         desc_input.fill("Hiabdeals generated by bot")

#         # 4. Save cart
#         page.get_by_role("button", name="Save", exact=True).first.click()
#         self._wait_loaded(page, settle_s=1.5)

#         # 5. Close confirmation window
#         close_btn = page.get_by_role("button", name="Close saved cart").first
#         if close_btn.count() > 0 and close_btn.is_visible():
#             close_btn.click()
#             time.sleep(0.5)

#         logger.info("Successfully created cart: %s", cart_name)

#     def _batch_order_url(self) -> str:
#         """Absolute Batch Order URL (relative paths fail under CDP)."""
#         base = self.base_url.rstrip("/")
#         if base.endswith("/en"):
#             return f"{base}/shop-by/batch/"
#         return f"{base}/en/shop-by/batch/"

#     def _open_batch_order(self) -> None:
#         assert self.page is not None
#         page = self.page

#         self._phase("Opening Batch Order")
#         batch_url = self._batch_order_url()
#         link = page.locator(
#             'a.l-s-header__link-site-nav[href*="/shop-by/batch"], '
#             'a[href="/en/shop-by/batch/"], '
#             'a:has-text("Batch Order")'
#         ).first

#         # Nav link often exists but stays hidden (collapsed site menu).
#         try:
#             if link.count() > 0 and link.is_visible():
#                 link.click()
#             else:
#                 logger.info(
#                     "Batch Order nav link hidden; opening %s directly.",
#                     batch_url,
#                 )
#                 self._safe_goto(batch_url)
#         except Exception as exc:
#             logger.warning(
#                 "Batch Order click failed (%s); navigating directly.", exc
#             )
#             self._safe_goto(batch_url)

#         self._wait_loaded(page, settle_s=2.0)
#         logger.info("Opened Batch Order page.")

#     def _ensure_batch_order_page(self) -> None:
#         """Return to Batch Order if navigation left the page after Add to cart."""
#         assert self.page is not None
#         page = self.page
#         file_input = page.locator(
#             'input[type="file"][name="file"], input.upload, input[type="file"]'
#         )
#         if file_input.count() > 0 and "/shop-by/batch" in (page.url or ""):
#             return
#         self._open_batch_order()

#     def _upload_and_add_to_cart(
#         self,
#         csv_path: Path,
#         batch_index: int = 1,
#         batch_total: int = 1,
#     ) -> None:
#         assert self.page is not None
#         page = self.page
#         csv_path = Path(csv_path).resolve()
#         if not csv_path.is_file():
#             raise FileNotFoundError(f"Batch CSV not found: {csv_path}")

#         self._phase(
#             f"Uploading batch {batch_index}/{batch_total}: {csv_path.name}"
#         )
#         self._attach_batch_csv(page, csv_path)
#         logger.info("Attached batch CSV via input[type=file]: %s", csv_path.name)

#         self._phase(f"Adding batch {batch_index}/{batch_total} to cart")
#         add_btn = page.locator(
#             "#batch-saved-card-add, button.batch-saved-card-add, button:has-text('add to saved cart')"
#         ).first
#         add_btn.wait_for(state="visible")

#         deadline = time.time() + 90
#         wait_started = time.time()
#         redispatched = False
#         while time.time() < deadline:
#             if self._add_to_cart_enabled(add_btn):
#                 break
#             # Site may still be parsing; re-dispatch change once mid-wait.
#             if not redispatched and time.time() - wait_started > 10:
#                 self._dispatch_file_change(page)
#                 redispatched = True
#             time.sleep(0.5)
#         else:
#             hint = self._batch_upload_error_hint(page)
#             raise TimeoutError(
#                 f"Add to cart stayed disabled after upload of {csv_path.name}."
#                 + (f" Page hint: {hint}" if hint else "")
#             )

#         add_btn.click()
#         self._wait_loaded(page, settle_s=2.0)
#         logger.info("Clicked Add to cart for batch %s/%s.", batch_index, batch_total)

#     def _attach_batch_csv(self, page: Page, csv_path: Path) -> None:
#         """Attach CSV to Batch Order file input; fire change so the page enables Add to cart."""
#         file_input = page.locator(
#             'input[type="file"][name="file"], input.upload, input[type="file"]'
#         ).first
#         file_input.wait_for(state="attached", timeout=self.timeout)

#         # Some themes hide the native input; keep it attachable for CDP.
#         try:
#             file_input.evaluate(
#                 """el => {
#                     el.style.display = 'block';
#                     el.style.visibility = 'visible';
#                     el.style.opacity = '1';
#                     el.removeAttribute('hidden');
#                     el.removeAttribute('disabled');
#                 }"""
#             )
#         except Exception:
#             pass

#         attached = False
#         # Prefer native chooser (matches manual Browse click on this site).
#         try:
#             with page.expect_file_chooser(timeout=5000) as fc_info:
#                 clicked = False
#                 for selector in (
#                     'label:has(input[type="file"][name="file"])',
#                     'label:has(input.upload)',
#                     ".upload-btn, .batch-upload, button:has-text('Browse')",
#                     "button:has-text('Choose'), button:has-text('Upload')",
#                     'input[type="file"][name="file"]',
#                 ):
#                     loc = page.locator(selector).first
#                     try:
#                         if loc.count() > 0:
#                             loc.click(timeout=2000, force=True)
#                             clicked = True
#                             break
#                     except Exception:
#                         continue
#                 if not clicked:
#                     file_input.click(force=True, timeout=2000)
#             fc_info.value.set_files(str(csv_path))
#             attached = True
#             logger.info("Attached CSV via file chooser.")
#         except Exception as exc:
#             logger.info("File chooser path skipped (%s); using set_input_files.", exc)

#         if not attached:
#             file_input.set_input_files(str(csv_path))

#         self._dispatch_file_change(page)
#         time.sleep(1.5)

#         # Confirm the input actually holds a file.
#         try:
#             names = file_input.evaluate(
#                 "el => el.files ? Array.from(el.files).map(f => f.name) : []"
#             )
#             logger.info("File input now has: %s", names)
#             if not names:
#                 # Retry set_input_files once more
#                 file_input.set_input_files(str(csv_path))
#                 self._dispatch_file_change(page)
#                 time.sleep(1.0)
#                 names = file_input.evaluate(
#                     "el => el.files ? Array.from(el.files).map(f => f.name) : []"
#                 )
#                 logger.info("File input after retry: %s", names)
#         except Exception as exc:
#             logger.warning("Could not read file input files list: %s", exc)

#     def _dispatch_file_change(self, page: Page) -> None:
#         try:
#             page.locator(
#                 'input[type="file"][name="file"], input.upload, input[type="file"]'
#             ).first.evaluate(
#                 """el => {
#                     el.dispatchEvent(new Event('input', { bubbles: true }));
#                     el.dispatchEvent(new Event('change', { bubbles: true }));
#                 }"""
#             )
#         except Exception:
#             pass

#     @staticmethod
#     def _add_to_cart_enabled(add_btn) -> bool:
#         try:
#             disabled = add_btn.get_attribute("disabled")
#             aria_disabled = add_btn.get_attribute("aria-disabled")
#             cls = (add_btn.get_attribute("class") or "").lower()
#             if disabled is not None:
#                 return False
#             if aria_disabled in ("true", "True"):
#                 return False
#             if "disabled" in cls.split():
#                 return False
#             return add_btn.is_enabled()
#         except Exception:
#             return False

#     @staticmethod
#     def _batch_upload_error_hint(page: Page) -> str:
#         selectors = (
#             ".error, .alert-error, .c-message--error, [role='alert'], "
#             ".batch-error, .upload-error, .form-error"
#         )
#         try:
#             loc = page.locator(selectors)
#             texts = []
#             for i in range(min(loc.count(), 5)):
#                 t = (loc.nth(i).inner_text(timeout=1000) or "").strip()
#                 if t:
#                     texts.append(t)
#             return " | ".join(texts)[:500]
#         except Exception:
#             return ""

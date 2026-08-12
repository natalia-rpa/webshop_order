"""Authentication handling for the webshop (SSO, Passkeys, Microsoft auth)."""

from __future__ import annotations

import time
from typing import Callable, Optional
from pathlib import Path
from playwright.sync_api import Page, BrowserContext, TimeoutError as PlaywrightTimeout

from config_loader import webshop_password
from logging_setup import get_logger

logger = get_logger()
PhaseCallback = Callable[[str, str], None]


def impersonator_toggle_selector() -> str:
    """Selector identifying the signed-in state."""
    return (
        '[data-testid="impersonator-toggle-button"], '
        'button[data-testid="impersonator-toggle-button"], '
        'button.c-impersonator__toggle, '
        'button:has-text("Add user"), '
        'button:has-text("Find user")'
    )


def dismiss_cookie_banner(page: Page) -> None:
    """Closes cookie consent overlays."""
    for selector in (
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        "#onetrust-reject-all-handler",
        ".onetrust-close-btn-handler",
    ):
        loc = page.locator(selector)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=3000)
                time.sleep(0.8)
                return
        except Exception:
            continue
    try:
        page.evaluate(
            """() => {
                const sdk = document.getElementById('onetrust-consent-sdk');
                if (sdk) sdk.style.display = 'none';
                document.querySelectorAll('.onetrust-pc-dark-filter')
                    .forEach(el => el.remove());
            }"""
        )
    except Exception:
        pass


def is_signed_in(page: Page) -> bool:
    """Checks if the user is currently signed in (impersonator control visible)."""
    try:
        if page.locator(impersonator_toggle_selector()).count() > 0:
            return True

        for selector in (
            'a.l-s-header__link-user-nav:has-text("Sign out")',
            'button:has-text("Sign out")',
            '[data-testid="impersonator"]',
            ".c-impersonator",
        ):
            if page.locator(selector).count() > 0:
                return True
        return False
    except Exception:
        return False


def login(page: Page, config) -> bool:
    """Executes the SSO login flow. Returns True if successful."""
    username = config.get("webshop", "username")
    base_url = config.get("webshop", "base_url")
    login_url = config.get("webshop", "login_url", fallback="https://webshop.hiab.com/en/login/ExternalLogin?ReturnUrl=/en/")

    dismiss_cookie_banner(page)
    if is_signed_in(page):
        return True

    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        dismiss_cookie_banner(page)
    except Exception:
        sign_in = page.locator("a.l-s-header__link-user-nav", has_text="Sign in").first
        if sign_in.count() == 0:
            sign_in = page.locator('a[href*="/login/ExternalLogin"]').first
        try:
            sign_in.click(timeout=10000)
        except Exception:
            sign_in.click(force=True)

    sso_btn = page.locator(
        'button.sso-button, '
        'button.slds-button.sso-button, '
        'button:has-text("Login with SSO")'
    ).first
    sso_btn.wait_for(state="visible", timeout=20000)
    try:
        sso_btn.click(timeout=10000)
    except Exception:
        sso_btn.click(force=True)
    time.sleep(1.5)

    deadline = time.time() + 45
    while time.time() < deadline:
        if is_signed_in(page):
            return True
        time.sleep(0.8)
    return False


def _fill_exact(locator, value: str) -> None:
    """Fills a field and ensures Chrome autofill doesn't overwrite it."""
    locator.click()
    locator.fill("")
    locator.press("Control+A")
    locator.press("Backspace")
    locator.fill(value)
    time.sleep(0.3)
    if locator.input_value() != value:
        locator.evaluate(
            """(el, v) => {
                el.focus(); el.value = ''; el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )


def complete_microsoft_login(popup: Page, config) -> bool:
    """Handles the actual interaction with the Microsoft login popup."""
    username = config.get("webshop", "username")
    password = webshop_password(config)
    acted = False

    account_tile = popup.locator(f'div[role="button"]:has-text("{username}")').first
    try:
        if account_tile.count() > 0 and account_tile.is_visible():
            account_tile.click(timeout=5000)
            acted = True
            time.sleep(1.0)
    except Exception:
        pass

    ms_pass = popup.locator('input[name="passwd"], input[type="password"]').first
    try:
        if ms_pass.count() > 0 and ms_pass.is_visible() and password:
            _fill_exact(ms_pass, password)
            acted = True
    except Exception:
        pass

    for selector in ('input[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Sign in")'):
        loc = popup.locator(selector)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                acted = True
                time.sleep(1.5)
                break
        except Exception:
            continue
    return acted


def handle_microsoft_auth(page: Page, context: BrowserContext, config) -> None:
    """Detects and interacts with Microsoft SSO popups."""
    deadline = time.time() + 60
    while time.time() < deadline:
        for popup in list(context.pages):
            if any(host in (popup.url or "").lower() for host in ("login.microsoftonline.com", "login.live.com")):
                try:
                    popup.bring_to_front()
                    complete_microsoft_login(popup, config)
                except Exception:
                    pass

        if is_signed_in(page):
            return
        time.sleep(1.0)


def handle_passkey_continue(page: Page, context: BrowserContext) -> None:
    """Interacts with the Salesforce passkey verification prompts."""
    deadline = time.time() + 20
    while time.time() < deadline:
        for pg in list(context.pages):
            for selector in ('button:has-text("Continue")', 'input[type="submit"][value="Continue"]'):
                loc = pg.locator(selector).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        pg.bring_to_front()
                        loc.click(timeout=3000)
                        return
                except Exception:
                    continue
        time.sleep(0.8)


def restore_session_via_sso(page: Page, context: BrowserContext, config) -> None:
    """Restores the session completely by running through SSO and multi-factor auth if needed."""
    already_signed_in = login(page, config)
    if not already_signed_in:
        handle_passkey_continue(page, context)
        handle_microsoft_auth(page, context, config)

    if not is_signed_in(page):
        raise RuntimeError("Webshop session is not active after Login with SSO.")
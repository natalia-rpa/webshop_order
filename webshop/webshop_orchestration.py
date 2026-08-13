"""
Playwright automation client for Hiab webshop batch order.
Fully minimized procedural approach using a single pipeline function without nested helpers.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from config_loader import load_config
from logging_setup import get_logger

# Ensure these imports match your project structure
from webshop.profil_handler import ProfileHandler
from webshop import login_handler

logger = get_logger()
PhaseCallback = Callable[[str, str], None]


def webshop_orchestration(
    page: Page,
    context: BrowserContext,
    client_number: str,
    client_name: str,
    client_mail: str,
    batch_csvs: Sequence[str | Path],
    config=None,
    on_phase: Optional[PhaseCallback] = None,
    require_existing_session: bool = False,
) -> None:
    """
    Executes the entire webshop batch order process in a single procedural flow.
    Includes browser startup, session check, impersonation, cart creation, 
    file uploads, and browser teardown inline.
    """
    cfg = config or load_config()
    
    paths: List[Path] = [Path(p).resolve() for p in batch_csvs]
    if not paths:
        raise ValueError("No batch CSV files provided.")

    base_url = cfg.get("webshop", "base_url")
    nav_timeout = cfg.getint("webshop", "navigation_timeout_ms", fallback=90000)
    timeout = cfg.getint("webshop", "default_timeout_ms", fallback=60000)

    
    

    #  _safe_goto (Navigate to base_url)
    target_url = base_url
    if (page.url or "").rstrip("/").lower() != target_url.rstrip("/").lower():
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception:
            pass
    
    #  _wait_loaded
    try:
        page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
    except PlaywrightTimeout:
        pass
    time.sleep(1.0)
    
    #  ensure_signed_in_session
    logger.info("Checking active session")
    login_handler.dismiss_cookie_banner(page)

    if require_existing_session:
        if not login_handler.is_signed_in(page):
            logger.warning("Session expired. Trying automatic SSO login...")
            login_handler.login(page, cfg)
            if not login_handler.is_signed_in(page):
                raise RuntimeError("Failed to restore session automatically. Run: python main.py --login")
        else:
            logger.info("Active signed-in webshop session confirmed.")
    else:
        if not login_handler.is_signed_in(page):
            logger.warning("Session is over — trying Login with SSO.")
            login_handler.restore_session_via_sso(page, context, cfg)

    #  _impersonate_user
    msg = f"Finding user {client_number} {client_name}".strip()
    logger.info(msg); on_phase and on_phase("PROCESSING", msg)

    #  _safe_goto (Navigate to base_url for impersonation reset)
    if (page.url or "").rstrip("/").lower() != base_url.rstrip("/").lower():
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception:
            pass
    #  _wait_loaded
    try:
        page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
    except PlaywrightTimeout:
        pass
    time.sleep(1.0)

    #  _open_impersonator_toggle
    selector = login_handler.impersonator_toggle_selector()
    for attempt in range(1, 4):
        login_handler.dismiss_cookie_banner(page)
        for exit_selector in ('[data-testid="impersonator-signout"] a', 'button:has-text("Stop impersonating")'):
            loc = page.locator(exit_selector)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=5000)
                #  _wait_loaded
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
                except PlaywrightTimeout:
                    pass
                time.sleep(1.5)

        toggle = page.locator(selector).first
        try:
            toggle.wait_for(state="attached", timeout=12_000)
            toggle.click(force=True, timeout=5000)
            break
        except Exception:
            #  _safe_goto (Retry reset on failure)
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=nav_timeout)
            except Exception:
                pass
            #  _wait_loaded
            try:
                page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
            except PlaywrightTimeout:
                pass
            time.sleep(1.0)
    else:
        raise TimeoutError("Impersonator control not available after retries.")

    time.sleep(0.3)
    search = page.locator('input.c-impersonator__input-search, input[id^="downshift-"]').first
    search.wait_for(state="visible")

    queries = [f"{client_number} {client_name}".strip()]
    if client_number:
        queries.append(client_number.strip())

    options = page.locator('[role="option"], .c-impersonator__menu-item')
    number_l = (client_number or "").casefold()
    name_l = (client_name or "").casefold()
    impersonated = False

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
                #  _wait_loaded
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
                except PlaywrightTimeout:
                    pass
                time.sleep(2.0)
                impersonated = True
                break
        if impersonated:
            break

    if not impersonated:
        raise TimeoutError(f"No impersonator option matched client_number={client_number}")

    #  _create_saved_cart
    email_prefix = (client_mail or "").strip()[:8]
    cart_name = f"{datetime.datetime.now().strftime('%d%m%Y_%H%M')}_{email_prefix}"
    
    msg = f"Creating new cart: {cart_name}"
    logger.info(msg); on_phase and on_phase("PROCESSING", msg)
    time.sleep(2.0)

    try:
        cart_icon = page.locator('button.wishlist-toggle:visible, button.right-off-canvas-toggle[title*="Saved carts"]:visible').first
        cart_icon.wait_for(state="visible", timeout=5000)
        cart_icon.click(force=True)
        time.sleep(2.0)
    except Exception:
        pass

    page.get_by_role("button", name="Create new cart").first.click()
    name_input = page.get_by_role("textbox", name="Cart name").first
    name_input.wait_for(state="visible", timeout=5000)
    name_input.click()
    name_input.fill(cart_name)

    page.get_by_role("textbox", name="Cart description").first.fill("Hiabdeals generated by bot")
    page.get_by_role("button", name="Save", exact=True).first.click()
    
    #  _wait_loaded
    try:
        page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
    except PlaywrightTimeout:
        pass
    time.sleep(1.5)

    close_btn = page.get_by_role("button", name="Close saved cart").first
    if close_btn.count() > 0 and close_btn.is_visible():
        close_btn.click()
        time.sleep(0.5)

    #  _open_batch_order
    base = base_url.rstrip("/")
    batch_url = f"{base}/shop-by/batch/" if base.endswith("/en") else f"{base}/en/shop-by/batch/"
    
    logger.info("Opening Batch Order")

    #  _safe_goto (Navigate to batch order page)
    if (page.url or "").rstrip("/").lower() != batch_url.rstrip("/").lower():
        try:
            page.goto(batch_url, wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception:
            pass

    #  _wait_loaded
    try:
        page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
    except PlaywrightTimeout:
        pass
    time.sleep(2.0)

    #  _upload_and_add_to_cart (Loop for multiple files)
    total = len(paths)
    for index, csv_path in enumerate(paths, start=1):
        
        logger.info(f"Batch upload {index}/{total}: {csv_path.name}")
        
        #  _ensure_batch_order_page
        if index > 1:
            if "/shop-by/batch" not in (page.url or ""):
                #  _safe_goto
                try:
                    page.goto(batch_url, wait_until="domcontentloaded", timeout=nav_timeout)
                except Exception:
                    pass
                #  _wait_loaded
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
                except PlaywrightTimeout:
                    pass
                time.sleep(2.0)

        #  _attach_batch_csv
        file_input = page.locator('input[type="file"][name="file"], input.upload, input[type="file"]').first
        file_input.wait_for(state="attached", timeout=timeout)
        file_input.set_input_files(str(csv_path))

        try:
            file_input.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
        except Exception:
            pass
        time.sleep(1.5)

        msg = f"Adding batch {index}/{total} to cart"
        logger.info(msg); on_phase and on_phase("PROCESSING", msg)
        
        add_btn = page.locator("#batch-saved-card-add, button.batch-saved-card-add, button:has-text('add to saved cart')").first
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
        
        #  _wait_loaded
        try:
            page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
        except PlaywrightTimeout:
            pass
        time.sleep(2.0)

    logger.info("Batch order flow completed for client %s.", client_number)
    

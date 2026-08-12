"""Manages physical Chrome processes and Playwright CDP attachment."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
import psutil

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page
from logging_setup import get_logger

logger = get_logger()

class ProfileHandler:
    """Handles Chrome process creation, cleanup, and Playwright CDP connection."""

    def __init__(self, config):
        self.config = config
        self.headless = config.getboolean("webshop", "headless", fallback=False)
        self.base_url = config.get("webshop", "base_url")
        self.timeout = config.getint("webshop", "default_timeout_ms", fallback=60000)
        self.nav_timeout = config.getint("webshop", "navigation_timeout_ms", fallback=90000)
        self.user_data_dir = config.get(
            "webshop", "user_data_dir", fallback="static/browser_profile"
        ).strip() or "static/browser_profile"
        
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._chrome_proc: Optional[subprocess.Popen] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._cdp_port = config.getint("webshop", "cdp_port", fallback=9222)

    @staticmethod
    def _clear_profile_locks(user_data_dir: Path) -> None:
        """Remove Chrome lock files left from a previous unclean exit."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            for path in (user_data_dir / name, user_data_dir / "Default" / name):
                try:
                    if path.exists() or path.is_symlink():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _find_chrome_exe() -> Path:
        """Locates the Chrome executable on the system."""
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError("chrome.exe not found")

    @staticmethod
    def _pick_free_port() -> int:
        """Finds an available network port for CDP."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _stop_chrome_on_port(self, port: int) -> None:
        """Kills any existing Chrome processes bound to the target CDP port."""
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = proc.info['name']
                if name and name.lower() in ('chrome.exe', 'chrome'):
                    cmdline = proc.info['cmdline']
                    if cmdline and any(f"--remote-debugging-port={port}" in arg for arg in cmdline):
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def start(self) -> Page:
        """Starts Chrome and attaches Playwright via CDP, returning the main page."""
        profile = Path(self.user_data_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._clear_profile_locks(profile)

        if self._cdp_port <= 0:
            self._cdp_port = self._pick_free_port()
        self._stop_chrome_on_port(self._cdp_port)

        chrome_exe = self._find_chrome_exe()
        chrome_args = [
            str(chrome_exe),
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            f"--remote-debugging-port={self._cdp_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--password-store=basic",
            "--disable-save-password-bubble",
        ]
        if self.headless:
            chrome_args.append("--headless=new")
        chrome_args.append(self.base_url)

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        if sys.platform == "win32" and self.headless:
            creationflags |= subprocess.CREATE_NO_WINDOW
            
        self._chrome_proc = subprocess.Popen(
            chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags
        )

        self._playwright = sync_playwright().start()
        cdp_url = f"http://127.0.0.1:{self._cdp_port}"
        deadline = time.time() + 45
        
        while time.time() < deadline:
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Could not attach to Chrome CDP at {cdp_url}")

        self.context = self._browser.contexts[0]
        self.context.set_default_timeout(self.timeout)
        self.context.set_default_navigation_timeout(self.nav_timeout)

        self.page = self._pick_webshop_page() or self.context.pages[0]
        return self.page

    def _pick_webshop_page(self) -> Optional[Page]:
        assert self.context is not None
        for pg in list(self.context.pages):
            url = (pg.url or "").lower()
            if "webshop.hiab.com" in url or "hiab.com" in url:
                return pg
        return self.context.pages[0] if self.context.pages else None

    def stop(self, *, keep_browser: bool = False) -> None:
        """Disconnects Playwright and optionally kills the Chrome process."""
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass

        self._browser = None
        self.context = None
        self.page = None

        if not keep_browser and self._chrome_proc is not None:
            try:
                if self._chrome_proc.poll() is None:
                    self._chrome_proc.terminate()
            except Exception:
                pass
            self._chrome_proc = None

        if getattr(self, '_playwright', None):
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
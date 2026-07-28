"""
Unattended entry point — hidden background operation with a signed-in session.

Runs Chrome headless (no window; logs only), keeps the bot session active,
polls MAIN every poll_interval_sec (default 60s), and processes rows where
MANUAL_PHASE=VALID and ROBOT_PHASE is empty — without signing in again.

If the session is not active first run:
  python main.py --login
then (you can close the login Chrome window):
  python unattended_main.py
  python unattended_main.py --max 1
"""

from __future__ import annotations

import sys

from main import main


if __name__ == "__main__":
    # Forward CLI args and force --unattended.
    argv = list(sys.argv[1:])
    if "--unattended" not in argv:
        argv.insert(0, "--unattended")
    sys.exit(main(argv))

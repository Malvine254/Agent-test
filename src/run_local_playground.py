"""Local launcher for Microsoft 365 Agents Playground / emulator testing.

Runs the exact same app as app.py, but forces the Teams SDK to skip JWT
validation on /api/messages so a local channel (Agents Playground / emulator /
direct POST) can talk to the bot without a Bot Framework token.

The bot identity (CLIENT_ID) is re-populated deep in app.py's import chain, so
instead of trying to strip it we patch microsoft_teams.apps.App to inject
``skip_auth=True`` before app.py binds the ``App`` symbol.

This file is for local testing only; production/Teams runs use app.py.
"""
import asyncio
import logging

# Make SDK errors visible — the SDK catches all exceptions and logs via its own
# logger, but that logger has no handlers configured by default, so 500 errors
# are completely silent. Route it to stderr so we can see what's failing.
logging.basicConfig(level=logging.WARNING, format="%(name)s | %(levelname)s | %(message)s")
logging.getLogger("microsoft_teams").setLevel(logging.DEBUG)

# Patch App BEFORE importing app.py (which does `from microsoft_teams.apps import App`).
import microsoft_teams.apps as _mta

_OrigApp = _mta.App


class _NoAuthApp(_OrigApp):  # type: ignore[misc, valid-type]
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("skip_auth", True)
        super().__init__(*args, **kwargs)


_mta.App = _NoAuthApp

import app  # noqa: E402

# app.py silences the microsoft_teams logger (CRITICAL). Re-enable ERROR+
# so SDK exceptions are visible in local dev — we need to see the traceback.
import logging as _logging
_logging.getLogger("microsoft_teams").setLevel(_logging.ERROR)

if __name__ == "__main__":
    print("[run_local_playground] JWT validation DISABLED (local emulator mode)")
    asyncio.run(app.startup())

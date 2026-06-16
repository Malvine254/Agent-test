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

# Patch App BEFORE importing app.py (which does `from microsoft_teams.apps import App`).
import microsoft_teams.apps as _mta

_OrigApp = _mta.App


class _NoAuthApp(_OrigApp):  # type: ignore[misc, valid-type]
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("skip_auth", True)
        super().__init__(*args, **kwargs)


_mta.App = _NoAuthApp

import app  # noqa: E402

if __name__ == "__main__":
    print("[run_local_playground] JWT validation DISABLED (local emulator mode)")
    asyncio.run(app.startup())

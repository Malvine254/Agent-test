"""Temporary diagnostic: test bot connector API directly with the acquired token."""
import asyncio
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Match what app.py does for multi-tenant
os.environ.pop("TENANT_ID", None)
os.environ.pop("CLIENT_SECRET", None)

from config import Config  # noqa: E402
from microsoft_teams.api.auth.credentials import ClientCredentials  # noqa: E402
from microsoft_teams.api.auth.cloud_environment import PUBLIC  # noqa: E402
from microsoft_teams.apps.token_manager import TokenManager  # noqa: E402
import requests  # noqa: E402


async def main():
    creds = ClientCredentials(client_id=Config.APP_ID, client_secret=Config.APP_PASSWORD)
    tm = TokenManager(credentials=creds, cloud=PUBLIC)

    tok = await tm.get_bot_token()
    raw_token = tok.value if hasattr(tok, "value") else str(tok)

    print(f"Token prefix: {raw_token[:30]}...")
    print(f"App ID: {Config.APP_ID}")

    # Use the conversation ID from the latest Teams message
    service_url = "https://smba.trafficmanager.net/amer/588cadf4-9902-4465-86c0-8bcf04f4f102"
    conv_id = "a:1t5n552P41a4icAy24PYWV6O_sijp1qtzvksarNRObXbe6avVb0rtytO7H7XjTpSVx_haLPt0CDoYL1JFwDcITpTEgFGT3Frk8raIkInmog1Uhxx-tox64-cZsTbvk23D"

    url = f"{service_url}/v3/conversations/{conv_id}/activities"
    headers = {
        "Authorization": f"Bearer {raw_token}",
        "Content-Type": "application/json",
    }
    body = {"type": "message", "text": "test"}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    print(f"Status: {r.status_code}")
    try:
        print(f"Response: {json.dumps(r.json(), indent=2)}")
    except Exception:
        print(f"Response text: {r.text[:500]}")


asyncio.run(main())

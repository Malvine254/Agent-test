"""
test_ai_search.py
=================
Quick sanity test for Azure AI Search (Cognitive Search) to catch api-version / auth / index issues.

Run:
  python test_ai_search.py "your query here"

Required env vars:
  AZURE_SEARCH_ENDPOINT   e.g. https://<service>.search.windows.net
  AZURE_SEARCH_INDEX      e.g. documents-index
  AZURE_SEARCH_KEY        (admin key or query key)
Optional:
  AZURE_SEARCH_API_VERSION  default: 2024-07-01
  AZURE_SEARCH_TOP          default: 5
  AZURE_SEARCH_SELECT       e.g. "name,file_path,content"
"""

import os
import sys
import json
import requests


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v and v.strip() else default


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    return endpoint[:-1] if endpoint.endswith("/") else endpoint


def test_search(query: str) -> None:

    # Use provided values directly - load from environment
    endpoint = os.environ.get("AI_SEARCH_ENDPOINT", "https://your-search.search.windows.net")
    index_name = os.environ.get("AI_SEARCH_INDEX", "your-index")
    api_key = os.environ.get("AI_SEARCH_KEY", "")  # Set via environment variable
    api_version = "2023-10-01-Preview"

    top = 5
    select = None

    url = f"{endpoint}/indexes/{index_name}/docs/search"
    params = {"api-version": api_version}

    # Minimal request body: `search` is required
    body: dict = {
        "search": query,
        "top": top,
        "count": True,
    }
    if select:
        body["select"] = select

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,  # ✅ Azure AI Search uses api-key header
    }

    print("=== Azure AI Search Test ===")
    print(f"URL: {url}")
    print(f"api-version: {api_version}")
    print(f"index: {index_name}")
    print(f"top: {top}")
    if select:
        print(f"select: {select}")
    print("============================\n")

    try:
        resp = requests.post(url, params=params, headers=headers, json=body, timeout=30)
    except Exception as e:
        print(f"REQUEST FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"HTTP {resp.status_code}")
    ct = resp.headers.get("content-type", "")
    print(f"content-type: {ct}")

    if resp.status_code != 200:
        # Print useful debugging info
        print("\n--- ERROR BODY (first 1200 chars) ---")
        print(resp.text[:1200])
        print("------------------------------------")
        print("\nMost common causes:")
        print("1) Wrong AZURE_SEARCH_API_VERSION (do NOT use OpenAI api-version)")
        print("2) Wrong index name")
        print("3) Wrong key (query key vs admin key) or wrong service endpoint")
        sys.exit(1)

    try:
        data = resp.json()
    except Exception:
        print("\n--- RESPONSE (first 1200 chars) ---")
        print(resp.text[:1200])
        print("----------------------------------")
        print("Response was not JSON—check service endpoint.")
        sys.exit(1)

    count = data.get("@odata.count")
    values = data.get("value", [])
    print(f"\nResults count: {count if count is not None else '(count not returned)'}")
    print(f"Returned items: {len(values)}\n")

    # Print first few results cleanly
    for i, item in enumerate(values[:top], 1):
        name = item.get("name") or item.get("title") or "(no name field)"
        path = item.get("file_path") or item.get("url") or item.get("webUrl") or ""
        snippet = ""
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            snippet = content.strip().replace("\n", " ")[:200]
        print(f"[{i}] {name}")
        if path:
            print(f"    path: {path}")
        if snippet:
            print(f"    snippet: {snippet}...")
        print()

    print("✅ AI Search request succeeded.")


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    if not q:
        print('Usage: python test_ai_search.py "your query here"')
        sys.exit(2)
    test_search(q)

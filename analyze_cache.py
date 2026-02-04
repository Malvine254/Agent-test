#!/usr/bin/env python3
"""
Analyze document cache for various types of duplicates.
"""
import json
from collections import defaultdict
from pathlib import Path

CACHE_FILE = Path("src/document_cache.json")

def analyze_cache():
    """Analyze cache for duplicates by multiple methods."""
    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    
    if 'users' not in cache:
        print("No user cache found.")
        return
    
    user_id = list(cache['users'].keys())[0]
    user_docs = cache['users'][user_id]['documents']
    
    print(f"Analyzing {len(user_docs)} documents...\n")
    
    # Analysis 1: By URL (exact match)
    by_url = defaultdict(list)
    for doc_id, doc in user_docs.items():
        url = doc.get('url', '')
        if url:
            by_url[url].append((doc_id, doc.get('name', 'unknown')))
    
    url_dupes = {k: v for k, v in by_url.items() if len(v) > 1}
    print(f"1. Duplicates by exact URL match: {len(url_dupes)}")
    if url_dupes:
        for url, instances in list(url_dupes.items())[:3]:
            print(f"   URL: {url[:60]}...")
            print(f"     - {len(instances)} copies")
    
    # Analysis 2: By name + path
    by_name_path = defaultdict(list)
    for doc_id, doc in user_docs.items():
        name = doc.get('name', '')
        path = doc.get('metadata', {}).get('path', '')
        key = f"{path}:{name}"
        by_name_path[key].append((doc_id, doc.get('name', 'unknown')))
    
    name_path_dupes = {k: v for k, v in by_name_path.items() if len(v) > 1}
    print(f"\n2. Duplicates by name + path: {len(name_path_dupes)}")
    if name_path_dupes:
        for key, instances in list(name_path_dupes.items())[:3]:
            print(f"   {key[:70]}...")
            print(f"     - {len(instances)} copies")
    
    # Analysis 3: By content hash (first 100 chars of content)
    by_content = defaultdict(list)
    for doc_id, doc in user_docs.items():
        content = doc.get('content', '')[:100]
        if content:
            by_content[content].append((doc_id, doc.get('name', 'unknown')))
    
    content_dupes = {k: v for k, v in by_content.items() if len(v) > 1}
    print(f"\n3. Duplicates by content (first 100 chars): {len(content_dupes)}")
    if content_dupes:
        for content, instances in list(content_dupes.items())[:3]:
            print(f"   Content: '{content[:50]}...'")
            print(f"     - {len(instances)} copies ({', '.join([d[1] for d in instances[:2]])})")
    
    # Analysis 4: By name (case-insensitive)
    by_name = defaultdict(list)
    for doc_id, doc in user_docs.items():
        name = doc.get('name', '').lower()
        if name:
            by_name[name].append((doc_id, doc.get('name', 'unknown')))
    
    name_dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    print(f"\n4. Duplicates by name (case-insensitive): {len(name_dupes)}")
    if name_dupes:
        for name, instances in list(name_dupes.items())[:5]:
            print(f"   '{name}'")
            print(f"     - {len(instances)} copies")
            for doc_id, _ in instances[:2]:
                url = user_docs[doc_id].get('url', 'no-url')
                print(f"       {doc_id[:40]}... -> {url[:50]}...")

if __name__ == "__main__":
    analyze_cache()

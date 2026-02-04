#!/usr/bin/env python3
"""
Clean up document cache by removing duplicates with identical content.
Keeps the first (earliest cached) copy of each duplicate set.
"""
import json
from collections import defaultdict
from pathlib import Path

CACHE_FILE = Path("src/document_cache.json")

def clean_cache_by_content():
    """Remove documents with identical content, keeping the first cached."""
    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    
    if 'users' not in cache:
        print("No user cache found.")
        return
    
    total_removed = 0
    
    # Process each user's cache
    for user_id, user_data in cache['users'].items():
        if 'documents' not in user_data:
            continue
        
        user_docs = user_data['documents']
        
        # Group by content (full text match)
        by_content = defaultdict(list)
        for doc_id, doc in user_docs.items():
            content = doc.get('content', '')
            
            # Only consider content > 50 chars to avoid matching empty/minimal content
            if len(content) > 50:
                by_content[content].append({
                    'doc_id': doc_id,
                    'name': doc.get('name', ''),
                    'cached_at': doc.get('cached_at', ''),
                    'url': doc.get('url', '')
                })
        
        # Find duplicates by content
        duplicates = {k: v for k, v in by_content.items() if len(v) > 1}
        
        if duplicates:
            print(f"\nUser {user_id[:20]}... found {len(duplicates)} duplicate content groups:")
            docs_to_remove = []
            
            for content_hash, instances in duplicates.items():
                # Sort by cached_at to keep the oldest
                instances.sort(key=lambda x: x.get('cached_at', ''))
                
                name = instances[0]['name']
                count = len(instances)
                print(f"  • {name} ({count} copies)")
                
                # Remove all but the first (oldest)
                for instance in instances[1:]:
                    doc_id = instance['doc_id']
                    docs_to_remove.append(doc_id)
                    print(f"    └─ Removing: {doc_id[:40]}... (cached: {instance['cached_at'][:19]})")
            
            # Remove duplicates
            for doc_id in docs_to_remove:
                del user_docs[doc_id]
                total_removed += 1
    
    # Also clean shared cache if present
    if 'shared' in cache and 'documents' in cache['shared']:
        shared_docs = cache['shared']['documents']
        
        by_content = defaultdict(list)
        for doc_id, doc in shared_docs.items():
            content = doc.get('content', '')
            
            if len(content) > 50:
                by_content[content].append({
                    'doc_id': doc_id,
                    'name': doc.get('name', ''),
                    'cached_at': doc.get('cached_at', ''),
                    'url': doc.get('url', '')
                })
        
        duplicates = {k: v for k, v in by_content.items() if len(v) > 1}
        
        if duplicates:
            print(f"\nShared cache found {len(duplicates)} duplicate content groups:")
            docs_to_remove = []
            
            for content_hash, instances in duplicates.items():
                instances.sort(key=lambda x: x.get('cached_at', ''))
                
                name = instances[0]['name']
                count = len(instances)
                print(f"  • {name} ({count} copies)")
                
                for instance in instances[1:]:
                    doc_id = instance['doc_id']
                    docs_to_remove.append(doc_id)
                    print(f"    └─ Removing: {doc_id[:40]}...")
            
            for doc_id in docs_to_remove:
                del shared_docs[doc_id]
                total_removed += 1
    
    # Save cleaned cache
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*70}")
    if total_removed > 0:
        print(f"✓ Cache cleaned successfully!")
        print(f"  Duplicate documents removed: {total_removed}")
    else:
        print(f"✓ Cache is clean - no duplicate content found")
    print(f"{'='*70}")

if __name__ == "__main__":
    clean_cache_by_content()

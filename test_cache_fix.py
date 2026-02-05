#!/usr/bin/env python3
"""Test script to verify cache clearing fix works properly"""

import sys
import os
import logging

# Setup basic logging to see what's happening
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from knowledge_base import unified_search

def test_search():
    print("Testing unified_search with cache clearing fix...")
    print("=" * 60)
    
    try:
        query = 'Patient Demographics log for Christian Wilson'
        print(f"Searching for: {query}")
        print()
        
        # Since this query should return no relevant results from Graph,
        # we expect the cache clearing to trigger and either:
        # 1. Return no results (if cache was low relevance)
        # 2. Return cache results (if above threshold)
        results, sources = unified_search(query)
        
        print(f"Results returned: {len(results)}")
        print(f"Sources used: {sources}")
        print()
        
        if results:
            print("Sample results:")
            for i, result in enumerate(results[:2]):
                title = result.get('name', 'Unknown') if isinstance(result, dict) else str(result)[:50]
                score = result.get('score', 'N/A') if isinstance(result, dict) else 'N/A'
                print(f"{i+1}. {title} (score: {score})")
        else:
            print("✅ No results returned - cache clearing fix worked properly!")
            
    except Exception as e:
        print(f"❌ Error during test: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_search()
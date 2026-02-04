#!/usr/bin/env python3
"""
Quick syntax and import check for the app.
"""
import sys
import os
import asyncio

# Add src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Check if basic imports work
try:
    print("Checking imports...")
    from app import handle_message, ensure_user_crawl, ensure_shared_crawl
    print("✓ App imports OK")
    
    # Verify functions are async
    import inspect
    assert inspect.iscoroutinefunction(ensure_user_crawl), "ensure_user_crawl should be async"
    assert inspect.iscoroutinefunction(ensure_shared_crawl), "ensure_shared_crawl should be async"
    assert inspect.iscoroutinefunction(handle_message), "handle_message should be async"
    print("✓ All functions are properly async")
    
    print("\n✅ All checks passed!")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

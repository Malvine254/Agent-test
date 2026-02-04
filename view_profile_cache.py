"""
View User Profile Cache Statistics
Shows cached user profiles and their age
"""

import os
import json
import time
from datetime import datetime, timedelta

cache_file = os.path.join(os.path.dirname(__file__), "src", "user_profiles_cache.json")
ttl_hours = 24  # Must match _profile_cache_ttl in app.py

def format_age(seconds):
    """Format age in human-readable format"""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m"
    elif seconds < 86400:
        return f"{int(seconds/3600)}h"
    else:
        return f"{int(seconds/86400)}d"

def main():
    print("\n" + "="*70)
    print("USER PROFILE CACHE STATISTICS")
    print("="*70)
    
    if not os.path.exists(cache_file):
        print(f"\n❌ Cache file not found: {cache_file}")
        print("The cache will be created when the bot starts.")
        return
    
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except Exception as e:
        print(f"\n❌ Error reading cache: {e}")
        return
    
    if not cache:
        print("\n📭 Cache is empty")
        return
    
    current_time = time.time()
    ttl_seconds = ttl_hours * 3600
    
    print(f"\nCache File: {cache_file}")
    print(f"TTL: {ttl_hours} hours")
    print(f"Total Entries: {len(cache)}")
    print("\n" + "-"*70)
    print(f"{'User ID':<40} {'Name':<25} {'Age':<10} {'Status'}")
    print("-"*70)
    
    fresh_count = 0
    expired_count = 0
    
    for user_id, entry in sorted(cache.items()):
        profile = entry.get('profile', {})
        cached_at = entry.get('cached_at', 0)
        age = current_time - cached_at
        
        display_name = profile.get('displayName', 'Unknown')[:24]
        age_str = format_age(age)
        status = "✓ Fresh" if age < ttl_seconds else "⚠ Expired"
        
        if age < ttl_seconds:
            fresh_count += 1
        else:
            expired_count += 1
        
        # Truncate user_id for display
        display_id = user_id[:37] + "..." if len(user_id) > 40 else user_id
        
        print(f"{display_id:<40} {display_name:<25} {age_str:<10} {status}")
    
    print("-"*70)
    print(f"\nSummary:")
    print(f"  Fresh entries:   {fresh_count} (will be used from cache)")
    print(f"  Expired entries: {expired_count} (will trigger API call)")
    print(f"  Cache hit rate:  {fresh_count/len(cache)*100:.1f}%")
    
    if expired_count > 0:
        print(f"\n💡 Tip: {expired_count} profile(s) will be refreshed on next use")
    
    print("\n✓ Cache is working to minimize API calls!")
    print("="*70 + "\n")

if __name__ == "__main__":
    main()

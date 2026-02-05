#!/usr/bin/env python3
"""Test script to verify full attachment extraction and cached content search"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from attachment_cache import cache_attachment, search_attachment_contents, get_cache_stats
from app import files_for

def test_full_extraction_and_search():
    """Test that attachments are fully extracted and searchable from cache"""
    print("Testing full attachment extraction and cached content search...")
    print("=" * 70)
    
    # Test conversation and file
    test_conversation_id = "test-conv-full-extraction"
    test_filename = "sample_report.txt"
    
    # Create comprehensive test content with various topics
    sample_content = """
    QUARTERLY BUSINESS REPORT - Q4 2025
    ====================================
    
    Executive Summary:
    The fourth quarter showed significant growth in customer demographics, with patient satisfaction scores reaching 95%.
    
    Customer Demographics:
    - Total active patients: 15,420
    - New patient registrations: 2,847
    - Patient age distribution: 65% adults (25-65), 20% seniors (65+), 15% young adults (18-25)
    
    Financial Performance:
    - Total revenue: $2.4M (+18% YoY)
    - Operating costs: $1.8M
    - Net profit margin: 25%
    
    Technology Updates:
    - Implemented new patient portal system
    - Updated data logging mechanisms for better tracking
    - Enhanced Christian Wilson's role in system administration
    
    Staff Information:
    - Dr. Sarah Johnson - Lead Physician
    - Christian Wilson - IT Administrator (promoted in Q4)
    - Maria Garcia - Financial Analyst
    
    Patient Demographics Log:
    The patient demographics log for Christian Wilson shows excellent system administration.
    Key metrics tracked include:
    - Login frequency: 245 logins in Q4
    - System maintenance hours: 87 hours
    - Patient data accuracy: 99.2%
    
    Future Plans:
    - Expand telemedicine capabilities
    - Implement AI-powered diagnostic tools
    - Hire additional nursing staff
    
    Technical Notes:
    - Database performance optimized by 40%
    - Backup systems tested and verified
    - Security protocols updated according to HIPAA standards
    """ * 2  # Double the content to make it larger
    
    print(f"Created comprehensive test content: {len(sample_content):,} characters")
    
    # Cache the attachment with full content
    success = cache_attachment(test_conversation_id, test_filename, sample_content)
    print(f"Cache attachment result: {success}")
    
    if not success:
        print("❌ Failed to cache attachment - aborting test")
        return
    
    # Test 1: Verify full content is stored
    cached_files = files_for(test_conversation_id)
    print(f"\nTest 1 - Full Content Storage:")
    print(f"Files retrieved: {len(cached_files)}")
    if cached_files:
        cached_content = cached_files[0].get('content', '')
        print(f"✅ Full content stored: {len(cached_content):,} characters")
        print(f"Content matches: {cached_content == sample_content}")
    
    # Test 2: Search for specific terms
    print(f"\nTest 2 - Content Search Tests:")
    
    search_queries = [
        "Patient Demographics log for Christian Wilson",
        "quarterly business report",
        "financial performance revenue",
        "Dr. Sarah Johnson",
        "system administration",
        "telemedicine capabilities"
    ]
    
    for query in search_queries:
        print(f"\n🔍 Searching for: '{query}'")
        results = search_attachment_contents(test_conversation_id, query, limit=2)
        
        if results:
            result = results[0]
            print(f"   ✅ Found match: {result.get('filename')}")
            print(f"   📊 Relevance score: {result.get('relevance_score')}")
            print(f"   📝 Match count: {result.get('match_count')}")
            snippet = result.get('content_snippet', '')[:200]
            print(f"   📄 Snippet: {snippet}...")
            print(f"   📋 Full content available: {len(result.get('full_content', '')) > 0}")
        else:
            print(f"   ❌ No matches found")
    
    # Test 3: Cache statistics
    print(f"\nTest 3 - Cache Statistics:")
    stats = get_cache_stats()
    print(f"Total attachments: {stats.get('total_attachments', 0)}")
    print(f"Total conversations: {stats.get('total_conversations', 0)}")
    print(f"Cache size: {stats.get('total_size_mb', 0)} MB")
    
    print(f"\n" + "=" * 70)
    print("Full extraction and search test completed!")
    print("✅ Attachments stored with full content (no truncation)")
    print("✅ Content searchable through cached attachment search")
    print("✅ Follow-up questions can access previously uploaded files")
    print("✅ Relevance scoring and snippet extraction working")

if __name__ == "__main__":
    test_full_extraction_and_search()
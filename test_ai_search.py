"""
Test suite for AI Search integration
Tests Azure Cognitive Search response parsing and document cache storage
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from document_cache import DocumentCache
from knowledge_base import search_documents


# =====================================================
# Mock Azure Cognitive Search Response
# =====================================================

MOCK_AI_SEARCH_RESPONSE = {
    "@odata.context": "https://armelyaisearch.search.windows.net/indexes('fileshare-vector-documents')/$metadata#docs(*)",
    "@odata.count": 2,
    "@search.answers": [
        {
            "key": "ce9d028939b6105256e64871b5d0b7e4",
            "text": "AGDIAG will detect and report on these issues: Detect and analyze Cluster or SQL health issues that cause availability group to fail over or go offline.",
            "highlights": "<em>AGDIAG will detect and report on these issues: Detect and analyze Cluster or SQL health issues that cause availability group to fail over or go offline.</em>",
            "score": 0.9409999847412109
        }
    ],
    "value": [
        {
            "id": "doc-123",
            "name": "SQL Server Failover Guide.pdf",
            "content": "AGDIAG will detect and report on these issues: Detect and analyze Cluster or SQL health issues that cause availability group to fail over or go offline. Detect and analyze why availability group failed to failover-to-failover partner during manual or automatic failover attempt.",
            "file_path": "https://armely-my.sharepoint.com/personal/user/Documents/SQL%20Server%20Failover%20Guide.pdf",
            "file_type": "pdf",
            "upload_date": "2025-12-15T10:30:00Z",
            "@search.score": 0.8756789994239807,
            "@search.highlights": {
                "content": [
                    "AGDIAG will <mark>detect and report on these issues</mark>"
                ]
            }
        },
        {
            "id": "doc-456",
            "name": "Azure SQL Availability Groups.docx",
            "content": "Availability groups provide a high-availability solution at the instance level. An availability group supports a replication environment for a discrete set of user databases.",
            "file_path": "https://armely-my.sharepoint.com/personal/user/Documents/Azure%20SQL%20Availability%20Groups.docx",
            "file_type": "docx",
            "upload_date": "2025-12-10T14:20:00Z",
            "@search.score": 0.7654321193695068,
            "@search.highlights": {
                "content": [
                    "Availability groups provide a high-availability <mark>solution</mark>"
                ]
            }
        }
    ]
}


# =====================================================
# Test 1: Parse AI Search Response
# =====================================================

def test_parse_ai_search_response():
    """Test that search_documents correctly parses Azure Cognitive Search response"""
    print("\n=== TEST 1: Parse AI Search Response ===")
    
    with patch('knowledge_base.requests.post') as mock_post:
        # Mock the HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_AI_SEARCH_RESPONSE
        mock_post.return_value = mock_response
        
        # Mock environment variables
        with patch.dict(os.environ, {
            'AZURE_SEARCH_ENDPOINT': 'https://armelyaisearch.search.windows.net',
            'AZURE_SEARCH_KEY': 'test-key-12345',
            'AZURE_SEARCH_INDEX': 'swope-vector-documents'
        }):
            results = search_documents("failover", top=5)
            
            # Verify results
            assert results is not None, "Results should not be None"
            assert len(results) >= 2, f"Expected at least 2 results, got {len(results)}"
            
            # Check first result (should be semantic answer)
            first = results[0]
            print(f"✓ Result 1 (Semantic Answer):")
            print(f"  - ID: {first.get('id')}")
            print(f"  - Name: {first.get('name')}")
            print(f"  - Score: {first.get('score')}")
            print(f"  - Content length: {len(first.get('content', ''))}")
            assert first.get('file_type') == 'semantic-answer', "First result should be semantic answer"
            assert first.get('content'), "Semantic answer should have content"
            
            # Check second result (regular document)
            second = results[1]
            print(f"\n✓ Result 2 (Document):")
            print(f"  - ID: {second.get('id')}")
            print(f"  - Name: {second.get('name')}")
            print(f"  - File Type: {second.get('file_type')}")
            print(f"  - Score: {second.get('score')}")
            print(f"  - Content length: {len(second.get('content', ''))}")
            assert second.get('id') == 'doc-123', "Second result should be first document"
            assert second.get('name') == 'SQL Server Failover Guide.pdf'
            assert second.get('file_type') == 'pdf'
            
            print("\n✓ TEST 1 PASSED: Response parsed correctly")


# =====================================================
# Test 2: Cache Storage Structure
# =====================================================

def test_cache_storage_structure():
    """Test that AI search results are stored in cache with correct structure"""
    print("\n=== TEST 2: Cache Storage Structure ===")
    
    # Create temporary cache file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        temp_cache_file = f.name
    
    try:
        cache = DocumentCache(temp_cache_file)
        
        # Simulate adding AI search results to cache
        user_id = "test-user-123"
        doc_id = "doc-123"
        
        cache.add_document(
            doc_id=doc_id,
            name="SQL Server Failover Guide.pdf",
            url="https://armely-my.sharepoint.com/personal/user/Documents/SQL%20Server%20Failover%20Guide.pdf",
            content="AGDIAG will detect and report on these issues...",
            user_id=user_id,
            metadata={
                "source": "ai-search",
                "file_type": "pdf",
                "upload_date": "2025-12-15T10:30:00Z",
                "ai_score": 0.8756789994239807
            }
        )
        
        # Verify document is cached
        cached_doc = cache.get_document(doc_id, user_id)
        assert cached_doc is not None, "Document should be in cache"
        
        print(f"✓ Cached Document Structure:")
        print(f"  - ID: {cached_doc.get('id')}")
        print(f"  - Name: {cached_doc.get('name')}")
        print(f"  - URL: {cached_doc.get('url')}")
        print(f"  - Visibility: {cached_doc.get('visibility')}")
        print(f"  - Cached At: {cached_doc.get('cached_at')}")
        print(f"  - Metadata: {json.dumps(cached_doc.get('metadata'), indent=2)}")
        
        # Verify structure
        assert cached_doc.get('id') == doc_id
        assert cached_doc.get('name') == "SQL Server Failover Guide.pdf"
        assert cached_doc.get('visibility') == 'user'
        assert cached_doc.get('metadata', {}).get('source') == 'ai-search'
        assert cached_doc.get('metadata', {}).get('ai_score') == 0.8756789994239807
        
        # Verify JSON file structure
        with open(temp_cache_file, 'r') as f:
            cache_json = json.load(f)
            assert 'users' in cache_json
            assert user_id in cache_json['users']
            assert 'documents' in cache_json['users'][user_id]
            assert doc_id in cache_json['users'][user_id]['documents']
            
        print(f"\n✓ JSON File Structure Valid:")
        print(f"  - Root key 'users': ✓")
        print(f"  - User ID '{user_id}': ✓")
        print(f"  - Documents collection: ✓")
        print(f"  - Document ID '{doc_id}': ✓")
        
        print("\n✓ TEST 2 PASSED: Cache structure is correct")
        
    finally:
        # Cleanup
        if os.path.exists(temp_cache_file):
            os.remove(temp_cache_file)


# =====================================================
# Test 3: Search Cache Retrieval
# =====================================================

def test_search_cache_retrieval():
    """Test that cached AI search results can be retrieved and matched"""
    print("\n=== TEST 3: Search Cache Retrieval ===")
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        temp_cache_file = f.name
    
    try:
        cache = DocumentCache(temp_cache_file)
        user_id = "test-user-123"
        
        # Add multiple documents with AI search source
        docs = [
            {
                "id": "ai-doc-1",
                "name": "Failover Guide",
                "content": "AGDIAG failover detection cluster health analysis",
            },
            {
                "id": "ai-doc-2",
                "name": "Availability Groups",
                "content": "Availability groups high availability replication databases",
            },
            {
                "id": "ai-doc-3",
                "name": "SQL Server Basics",
                "content": "SQL Server database management system relational",
            }
        ]
        
        for doc in docs:
            cache.add_document(
                doc_id=doc['id'],
                name=doc['name'],
                url=f"https://example.com/{doc['id']}",
                content=doc['content'],
                user_id=user_id,
                metadata={
                    "source": "ai-search",
                    "file_type": "pdf"
                }
            )
        
        # Search cache for "failover"
        results = cache.search_cache("failover", user_id=user_id, limit=5)
        assert len(results) > 0, "Should find results for 'failover'"
        print(f"✓ Search 'failover': Found {len(results)} result(s)")
        for r in results:
            print(f"  - {r.get('name')}: score={r.get('score', 'N/A')}")
        
        # Search for "availability"
        results = cache.search_cache("availability", user_id=user_id, limit=5)
        assert len(results) > 0, "Should find results for 'availability'"
        print(f"\n✓ Search 'availability': Found {len(results)} result(s)")
        for r in results:
            print(f"  - {r.get('name')}: score={r.get('score', 'N/A')}")
        
        # Search for non-existent term
        results = cache.search_cache("nonexistent", user_id=user_id, limit=5)
        print(f"\n✓ Search 'nonexistent': Found {len(results)} result(s)")
        
        print("\n✓ TEST 3 PASSED: Cache retrieval and search works")
        
    finally:
        if os.path.exists(temp_cache_file):
            os.remove(temp_cache_file)


# =====================================================
# Test 4: Response Field Mapping
# =====================================================

def test_response_field_mapping():
    """Test that all Azure Search response fields are correctly mapped"""
    print("\n=== TEST 4: Response Field Mapping ===")
    
    response = MOCK_AI_SEARCH_RESPONSE
    
    # Check OData metadata
    assert "@odata.context" in response, "Missing @odata.context"
    assert "fileshare-vector-documents" in response["@odata.context"]
    print(f"✓ OData Context: {response['@odata.context'][:60]}...")
    
    # Check result count
    assert "@odata.count" in response, "Missing @odata.count"
    print(f"✓ Result Count: {response['@odata.count']}")
    
    # Check semantic answers
    assert "@search.answers" in response, "Missing @search.answers"
    assert len(response["@search.answers"]) > 0, "Should have semantic answers"
    answer = response["@search.answers"][0]
    print(f"\n✓ Semantic Answer:")
    print(f"  - Key: {answer.get('key')}")
    print(f"  - Score: {answer.get('score')}")
    print(f"  - Text: {answer.get('text')[:60]}...")
    print(f"  - Highlights: {len(answer.get('highlights', ''))} chars")
    
    # Check document results
    assert "value" in response, "Missing 'value' field"
    assert len(response["value"]) > 0, "Should have document results"
    doc = response["value"][0]
    print(f"\n✓ Document Result:")
    print(f"  - id: {doc.get('id')}")
    print(f"  - name: {doc.get('name')}")
    print(f"  - file_path: {doc.get('file_path')}")
    print(f"  - file_type: {doc.get('file_type')}")
    print(f"  - upload_date: {doc.get('upload_date')}")
    print(f"  - @search.score: {doc.get('@search.score')}")
    print(f"  - @search.highlights: {list(doc.get('@search.highlights', {}).keys())}")
    
    # Verify all required fields exist
    required_fields = ['id', 'name', 'content', 'file_path', 'file_type', 'upload_date', '@search.score']
    for field in required_fields:
        assert field in doc, f"Missing required field: {field}"
    
    print("\n✓ TEST 4 PASSED: All response fields mapped correctly")


# =====================================================
# Main Test Runner
# =====================================================

if __name__ == "__main__":
    print("╔════════════════════════════════════════════════════════════╗")
    print("║         AI Search Integration Test Suite                   ║")
    print("║   Testing Azure Cognitive Search & Document Cache          ║")
    print("╚════════════════════════════════════════════════════════════╝")
    
    try:
        test_response_field_mapping()
        test_parse_ai_search_response()
        test_cache_storage_structure()
        test_search_cache_retrieval()
        
        print("\n╔════════════════════════════════════════════════════════════╗")
        print("║              ✓ ALL TESTS PASSED                            ║")
        print("╚════════════════════════════════════════════════════════════╝\n")
        
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)

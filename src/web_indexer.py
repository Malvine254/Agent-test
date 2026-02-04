"""
Web Indexer - Background web page crawler and indexer (WEBSITES ONLY)

IMPORTANT: This module is for crawling EXTERNAL WEBSITES only (e.g., company websites, help pages).
- OneDrive/SharePoint documents use live Graph API search (search_sharepoint in knowledge_base.py)
- OneDrive/SharePoint background indexing uses crawl_accessible_documents (knowledge_base.py)
- This web indexer should NOT be used for OneDrive/SharePoint

Crawls websites and stores indexed pages in JSON for search and analysis.
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse
from collections import deque

logger = logging.getLogger(__name__)

try:
    import aiohttp
    from bs4 import BeautifulSoup
except ImportError:
    aiohttp = None
    BeautifulSoup = None


class WebIndexer:
    def __init__(self, cache_file: str = "web_cache.json"):
        self.cache_file = cache_file
        self.cache = self._load_cache()
        self.is_indexing = {}  # Track indexing status per URL
        
    def _load_cache(self) -> Dict:
        """Load cache from JSON file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        return {"websites": {}, "last_updated": None}
                    return json.loads(content)
            except json.JSONDecodeError as e:
                logger.error(f"Web cache file corrupted: {e}. Starting fresh.")
                return {"websites": {}, "last_updated": None}
            except Exception as e:
                logger.error(f"Error loading web cache: {e}")
                return {"websites": {}, "last_updated": None}
        return {"websites": {}, "last_updated": None}
    
    def _save_cache(self):
        """Save cache to JSON file"""
        try:
            self.cache["last_updated"] = datetime.now().isoformat()
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving web cache: {e}")
    
    def _get_domain(self, url: str) -> str:
        """Extract domain from URL"""
        parsed = urlparse(url)
        return parsed.netloc or parsed.path
    
    async def crawl_website(self, start_url: str, max_pages: int = 50, max_depth: int = 3) -> int:
        """
        Crawl a website and index all pages.
        
        Args:
            start_url: Starting URL to crawl from
            max_pages: Maximum pages to crawl
            max_depth: Maximum depth to crawl
            
        Returns:
            Number of pages indexed
        """
        if not aiohttp or not BeautifulSoup:
            logger.warning("aiohttp or BeautifulSoup not installed. Web crawling disabled.")
            return 0
        
        domain = self._get_domain(start_url)
        if domain in self.is_indexing and self.is_indexing[domain]:
            logger.info(f"Already indexing {domain}")
            return 0
        
        self.is_indexing[domain] = True
        
        try:
            if domain not in self.cache["websites"]:
                self.cache["websites"][domain] = {"pages": {}, "status": "indexing"}
            
            visited = set()
            queue = deque([(start_url, 0)])  # (url, depth)
            pages_indexed = 0
            consecutive_errors = 0  # Track consecutive failures
            max_consecutive_errors = 5  # Stop if too many consecutive errors
            
            # Create SSL context that doesn't verify certificates (for problematic sites)
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                while queue and pages_indexed < max_pages:
                    url, depth = queue.popleft()
                    
                    # Skip if already visited or depth exceeded
                    if url in visited or depth > max_depth:
                        continue
                    
                    visited.add(url)
                    
                    try:
                        logger.info(f"Crawling: {url}")
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                # Only log first error for the domain, then silently skip
                                if consecutive_errors == 0:
                                    logger.warning(f"Failed to fetch {url}: HTTP {resp.status}. Subsequent errors will be silently skipped.")
                                consecutive_errors += 1
                                if consecutive_errors >= max_consecutive_errors:
                                    logger.error(f"Too many consecutive errors ({consecutive_errors}) for {domain}. Marking as failed.")
                                    self.cache["websites"][domain]["status"] = "failed"
                                    self._save_cache()
                                    return pages_indexed
                                continue
                            
                            content_type = resp.headers.get('content-type', '')
                            if 'text/html' not in content_type:
                                logger.debug(f"Skipping non-HTML: {url}")
                                continue
                            
                            html = await resp.text()
                            soup = BeautifulSoup(html, 'html.parser')
                            
                            # Extract title and text content
                            title = soup.title.string if soup.title else url.split('/')[-1]
                            title = str(title).strip() if title else url.split('/')[-1]
                            
                            # Remove unwanted elements completely
                            for element in soup([
                                "script", "style", "meta", "link", "noscript", 
                                "iframe", "svg", "path", "img", "input", "button",
                                "header", "nav", "aside"  # Remove navigation/ads but keep footer (often has contact info)
                            ]):
                                element.decompose()
                            
                            # Remove comments
                            for comment in soup.find_all(text=lambda text: isinstance(text, str) and text.strip().startswith('<!--')):
                                comment.extract()
                            
                            # Prioritize main content areas
                            main_content = (
                                soup.find('main') or 
                                soup.find('article') or 
                                soup.find('div', class_=lambda c: c and any(x in str(c).lower() for x in ['content', 'main', 'article', 'post', 'body'])) or
                                soup.find('body') or
                                soup
                            )
                            
                            # Get text with better formatting
                            text = main_content.get_text(separator='\n', strip=True)
                            
                            # Clean up text: remove excessive whitespace and special characters
                            lines = []
                            for line in text.split('\n'):
                                # Strip line
                                line = line.strip()
                                # Skip empty lines
                                if not line:
                                    continue
                                # Skip lines with only special characters or very short lines
                                if len(line) < 3 or all(c in '|•·→←↑↓►◄-=_*#@' for c in line):
                                    continue
                                # Remove excessive spaces
                                line = ' '.join(line.split())
                                # Skip duplicated lines (common in menus)
                                if lines and line == lines[-1]:
                                    continue
                                lines.append(line)
                            
                            text = '\n'.join(lines)
                            
                            # Remove common noise patterns
                            noise_patterns = [
                                'Skip to content',
                                'Skip to main content',
                                'Cookie Policy',
                                'Accept Cookies',
                                'Privacy Policy',
                                'Terms of Service',
                                'Sign in',
                                'Log in',
                                'Subscribe to newsletter',
                                'Follow us on',
                                'Share on',
                                'We Value Your Privacy',
                                'Cookie Preferences',
                                'Essential Cookies',
                                'Performance Cookies',
                                'Functionality Cookies',
                                'Targeting/Advertising Cookies',
                                'Analytics Cookies',
                                'Save Preferences',
                                'Accept All',
                                'Customize',
                                'Search',
                                'Start Searching',
                                'Enter keywords',
                                'Ask AI About',
                                'Chat now',
                                'No thanks',
                                'Need help?',
                                "Let's chat",
                                'Customer support',
                                'Get Appointment',
                                'Learn More',
                                'READ MORE',
                                'Read More',
                                'View Details',
                                'Read Full Story',
                                'Schedule a Consultation',
                                'Send Message'
                            ]
                            
                            lines_cleaned = []
                            for line in lines:
                                # Skip lines that are just noise patterns
                                if any(noise.lower() in line.lower() for noise in noise_patterns):
                                    continue
                                lines_cleaned.append(line)
                            
                            text = '\n'.join(lines_cleaned)
                            
                            # Remove repetitive menu/navigation sections
                            # Common navigation patterns that repeat across pages
                            nav_patterns = [
                                r'Who We Are\s+Company Overview\s+Career Opportunities.*?What We Do',
                                r'All Services\s+AI Services.*?Managed Services',
                                r'Knowledge Hub\s+Blog Articles.*?Partners',
                                r'Search Armely.*?Customer support',
                            ]
                            
                            import re
                            for pattern in nav_patterns:
                                text = re.sub(pattern, '', text, flags=re.DOTALL | re.IGNORECASE)
                            
                            # Store page
                            if text and len(text) > 50:  # Only store if meaningful content
                                page_id = url
                                self.cache["websites"][domain]["pages"][page_id] = {
                                    "url": url,
                                    "title": title,
                                    "content": text[:8000],  # Increased limit for more complete content
                                    "indexed_at": datetime.now().isoformat()
                                }
                                pages_indexed += 1
                                consecutive_errors = 0  # Reset error counter on success
                                logger.info(f"Indexed: {title} ({len(text)} chars, {pages_indexed}/{max_pages})")
                                self._save_cache()
                            else:
                                logger.debug(f"Skipped {url}: insufficient content ({len(text)} chars)")
                            
                            # Find links for next pages
                            if depth < max_depth:
                                for link in soup.find_all('a', href=True):
                                    next_url = urljoin(url, link['href'])
                                    
                                    # Only follow links on same domain
                                    if self._get_domain(next_url) == domain and next_url not in visited:
                                        queue.append((next_url, depth + 1))
                    
                    except asyncio.TimeoutError:
                        if consecutive_errors == 0:
                            logger.warning(f"Timeout fetching {url}. Subsequent timeouts will be silently skipped.")
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            logger.error(f"Too many consecutive errors for {domain}. Marking as failed.")
                            self.cache["websites"][domain]["status"] = "failed"
                            self._save_cache()
                            return pages_indexed
                        continue
                    except Exception as e:
                        if consecutive_errors == 0:
                            logger.warning(f"Error crawling {url}: {e}. Subsequent errors will be silently skipped.")
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            logger.error(f"Too many consecutive errors for {domain}. Marking as failed.")
                            self.cache["websites"][domain]["status"] = "failed"
                            self._save_cache()
                            return pages_indexed
                        continue
                    
                    # Small delay between requests
                    await asyncio.sleep(0.5)
            
            self.cache["websites"][domain]["status"] = "completed"
            logger.info(f"Completed indexing {domain}: {pages_indexed} pages")
            
        finally:
            self.is_indexing[domain] = False
        
        return pages_indexed
    
    def search_web_cache(self, query: str, limit: int = 10) -> List[Dict]:
        """Search indexed web pages by query"""
        query_lower = query.lower()
        results = []
        
        for domain, domain_data in self.cache.get("websites", {}).items():
            for page_id, page in domain_data.get("pages", {}).items():
                score = 0
                title_lower = page["title"].lower()
                content_lower = page["content"].lower()
                
                # Title match scores higher
                if query_lower in title_lower:
                    score += 20
                if query_lower in content_lower:
                    score += 10
                
                # Word-level matching (including partial word matches for common keywords)
                for word in query_lower.split():
                    if len(word) > 2:
                        if word in title_lower:
                            score += 5
                        if word in content_lower:
                            score += 2
                
                # Special handling for common search terms
                # If looking for "contact" and page has contact info, boost score
                if any(term in query_lower for term in ["contact", "phone", "email", "address"]):
                    if any(indicator in content_lower for indicator in ["info@", "+1 ", "phone:", "email:", "address:", "fax:", "contact us"]):
                        score += 15  # Strong boost for contact pages
                
                # If looking for company name and page mentions it, boost score
                for term in query_lower.split():
                    if len(term) > 3 and term not in ["contact", "information", "about"]:
                        if term in content_lower:
                            score += 3  # Boost for domain/company name matches
                
                if score > 0:
                    # Include domain in result
                    result_page = page.copy()
                    result_page["domain"] = domain
                    results.append({
                        "page": result_page,
                        "domain": domain,
                        "score": score
                    })
        
        # Sort by score and return top results
        results.sort(key=lambda x: x["score"], reverse=True)
        return [r["page"] for r in results[:limit]]
    
    def get_indexing_status(self) -> Dict:
        """Get current indexing status"""
        status = {}
        for domain, domain_data in self.cache.get("websites", {}).items():
            pages = domain_data.get("pages", {})
            status[domain] = {
                "status": domain_data.get("status", "unknown"),
                "pages_indexed": len(pages),
                "is_currently_indexing": self.is_indexing.get(domain, False)
            }
        return status
    
    def clear_web_cache(self):
        """Clear all indexed web pages"""
        self.cache = {"websites": {}, "last_updated": None}
        self._save_cache()
        logger.info("Web cache cleared")


# Singleton instance
_web_indexer_instance = None


def get_web_indexer() -> WebIndexer:
    """Get singleton web indexer instance"""
    global _web_indexer_instance
    if _web_indexer_instance is None:
        cache_dir = os.path.dirname(__file__)
        cache_file = os.path.join(cache_dir, "web_cache.json")
        _web_indexer_instance = WebIndexer(cache_file)
    return _web_indexer_instance

# app/services/ecommerce_scraper_service.py
import asyncio
import logging
import random
import time
import re
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import difflib

try:
    from crawl4ai import AsyncWebCrawler
    from crawl4ai.extraction_strategy import NoExtractionStrategy
except ImportError:
    from crawl4ai.async_webcrawler import AsyncWebCrawler
    from crawl4ai.extraction_strategy import NoExtractionStrategy

from app.utils.validators import validate_url
from app.models.crawler_models import CrawlResult, CrawlConfig

logger = logging.getLogger(__name__)


class EcommerceProductExtractor:
    """Extract e-commerce products with proper ordering and absolute URLs"""
    
    def __init__(self):
        # Tags to completely remove
        self.remove_tags = [
            'script', 'style', 'noscript', 'link', 'meta',
            'form', 'input', 'button', 'select', 'textarea'
        ]
        
        # Common e-commerce selectors for auto-detection
        self.common_ecommerce_selectors = {
            'title': [
                'h1', 'h2', 'h3', '.product-title', '.product-name', '.title',
                '.name', '.product-heading', '.item-title', '.card-title',
                'a[title]', '.product-link'
            ],
            'price': [
                '.price', '.cost', '.amount', '.product-price', '.price-current',
                '.price-now', '.sale-price', '.regular-price', '.final-price',
                '[data-price]', '.money', '.currency', '.price-box'
            ],
            'original_price': [
                '.original-price', '.was-price', '.old-price', '.strike-price',
                '.crossed-price', '.price-was', '.regular-price', '.list-price'
            ],
            'image': [
                'img', '.product-image img', '.item-image img', '.card-image img',
                '.thumbnail img', '.photo img', 'picture img'
            ],
            'link': [
                'a', '.product-link', '.item-link', '.card-link', '.title-link'
            ],
            'rating': [
                '.rating', '.stars', '.review-stars', '.product-rating',
                '[data-rating]', '.star-rating', '.rating-value'
            ],
            'reviews': [
                '.reviews', '.review-count', '.reviews-count', '.rating-count',
                '.review-number', '[data-reviews]'
            ],
            'brand': [
                '.brand', '.manufacturer', '.vendor', '.product-brand',
                '.brand-name', '[data-brand]'
            ],
            'availability': [
                '.availability', '.stock', '.in-stock', '.out-of-stock',
                '.stock-status', '[data-availability]', '.inventory'
            ],
            'discount': [
                '.discount', '.sale', '.off', '.save', '.discount-percent',
                '.sale-badge', '.discount-badge', '.offer'
            ],
            'description': [
                '.description', '.product-description', '.summary', '.excerpt',
                '.product-summary', '.item-description', '.details'
            ]
        }
    
    def _make_absolute_url(self, base_url: str, relative_url: str) -> str:
        """Convert relative URL to absolute URL"""
        if not relative_url:
            return ''
        
        # If already absolute, return as is
        if relative_url.startswith(('http://', 'https://')):
            return relative_url
        
        # Handle protocol-relative URLs
        if relative_url.startswith('//'):
            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme}:{relative_url}"
        
        # Join relative URL with base URL
        try:
            return urljoin(base_url, relative_url)
        except Exception:
            return relative_url
    
    def _extract_image_url(self, element, base_url: str) -> str:
        """Extract image URL from img element and make it absolute"""
        if not element:
            return ''
        
        # Try different image URL attributes
        img_url = ''
        
        # Check common image attributes in order of preference
        for attr in ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset']:
            img_url = element.get(attr, '').strip()
            if img_url:
                # Handle srcset - take the first URL
                if 'srcset' in attr and ',' in img_url:
                    img_url = img_url.split(',')[0].strip().split(' ')[0]
                break
        
        # If no direct URL, check if there's an img tag inside
        if not img_url and element.name != 'img':
            img_tag = element.find('img')
            if img_tag:
                for attr in ['src', 'data-src', 'data-lazy-src', 'data-original']:
                    img_url = img_tag.get(attr, '').strip()
                    if img_url:
                        break
        
        # Make URL absolute
        if img_url:
            return self._make_absolute_url(base_url, img_url)
        
        return ''
    
    def _extract_link_url(self, element, base_url: str) -> str:
        """Extract link URL from anchor element and make it absolute"""
        if not element:
            return ''
        
        link_url = ''
        
        # If element is an anchor tag
        if element.name == 'a':
            link_url = element.get('href', '').strip()
        else:
            # Find anchor tag inside element
            anchor_tag = element.find('a')
            if anchor_tag:
                link_url = anchor_tag.get('href', '').strip()
        
        # Make URL absolute
        if link_url:
            return self._make_absolute_url(base_url, link_url)
        
        return ''
    
    def _extract_price(self, element) -> str:
        """Extract and clean price from element"""
        if not element:
            return ''
        
        text = element.get_text(strip=True)
        
        # Common price patterns
        price_patterns = [
            r'[\$£€¥₹]\s*[\d,]+\.?\d*',  # $123.45, £1,234.56
            r'[\d,]+\.?\d*\s*[\$£€¥₹]',  # 123.45$
            r'[\d,]+\.?\d*',              # 123.45 (fallback)
        ]
        
        for pattern in price_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group().strip()
        
        return text
    
    def _extract_rating(self, element) -> str:
        """Extract rating from element"""
        if not element:
            return ''
        
        # Check for data attributes first
        for attr in ['data-rating', 'data-score', 'title', 'aria-label']:
            value = element.get(attr, '').strip()
            if value and re.search(r'\d+\.?\d*', value):
                return value
        
        # Extract from text
        text = element.get_text(strip=True)
        rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/|\s)\s*(\d+)', text)
        if rating_match:
            return f"{rating_match.group(1)}/{rating_match.group(2)}"
        
        # Just a number
        number_match = re.search(r'\d+\.?\d*', text)
        if number_match:
            return number_match.group()
        
        return text
    
    def _is_duplicate_product(self, product: Dict, existing_products: List[Dict], threshold: float = 0.85) -> bool:
        """Check if product is duplicate of existing products"""
        title = product.get('title', '').strip().lower()
        if not title or len(title) < 10:
            return True
        
        for existing in existing_products:
            existing_title = existing.get('title', '').strip().lower()
            if not existing_title:
                continue
            
            # Check exact match
            if title == existing_title:
                return True
            
            # Check similarity ratio
            similarity = difflib.SequenceMatcher(None, title, existing_title).ratio()
            if similarity > threshold:
                return True
        
        return False
    
    def extract_products(self, html_content: str, 
                        product_selector: str,
                        field_selectors: Dict[str, str],
                        base_url: str = '',
                        limit: Optional[int] = None,
                        exclude_selectors: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Extract e-commerce products from HTML content
        
        Args:
            html_content: Raw HTML content
            product_selector: CSS selector for product containers
            field_selectors: Dict mapping field names to CSS selectors
            base_url: Base URL for making relative URLs absolute
            limit: Maximum number of products to extract
            exclude_selectors: List of CSS selectors to exclude
            
        Returns:
            Dict with extracted products and metadata
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove unwanted tags completely
            for tag in soup(self.remove_tags):
                tag.decompose()
            
            # Remove excluded selectors if specified
            if exclude_selectors:
                for selector in exclude_selectors:
                    try:
                        for element in soup.select(selector):
                            element.decompose()
                    except Exception as e:
                        logger.warning(f"Invalid exclude selector '{selector}': {str(e)}")
            
            # Find all product containers
            product_elements = soup.select(product_selector)
            
            if limit and len(product_elements) > limit:
                product_elements = product_elements[:limit]
            
            products = []
            
            # Process each product element
            for i, product_element in enumerate(product_elements):
                product_data = {
                    'index': i,
                    'title': '',
                    'price': '',
                    'image': '',
                    'link': ''
                }
                
                # Extract each field using provided selectors
                for field_name, field_selector in field_selectors.items():
                    try:
                        field_elements = product_element.select(field_selector)
                        
                        if field_elements:
                            element = field_elements[0]  # Take first match
                            
                            # Handle special field types
                            if field_name.lower() in ['image', 'img', 'picture', 'photo']:
                                product_data[field_name] = self._extract_image_url(element, base_url)
                            
                            elif field_name.lower() in ['link', 'url', 'href']:
                                product_data[field_name] = self._extract_link_url(element, base_url)
                            
                            elif field_name.lower() in ['price', 'cost', 'amount', 'original_price']:
                                product_data[field_name] = self._extract_price(element)
                            
                            elif field_name.lower() in ['rating', 'stars', 'score']:
                                product_data[field_name] = self._extract_rating(element)
                            
                            else:
                                # Extract text content
                                product_data[field_name] = element.get_text(strip=True)
                        
                        else:
                            product_data[field_name] = ''
                    
                    except Exception as e:
                        logger.warning(f"Error extracting field '{field_name}': {str(e)}")
                        product_data[field_name] = ''
                
                # Skip products without title or with very short titles
                if not product_data.get('title') or len(product_data['title'].strip()) < 5:
                    continue
                
                # Add metadata
                product_data['word_count'] = len(product_data['title'].split()) if product_data['title'] else 0
                
                products.append(product_data)
            
            # Remove duplicates while preserving order
            unique_products = []
            for product in products:
                if not self._is_duplicate_product(product, unique_products, threshold=0.8):
                    unique_products.append(product)
            
            # Re-index after deduplication
            for i, product in enumerate(unique_products):
                product['index'] = i
            
            return {
                'products': unique_products,
                'metadata': {
                    'total_found': len(product_elements),
                    'total_extracted': len(unique_products),
                    'deduplication_applied': len(unique_products) < len(products),
                    'order_preserved': True,
                    'urls_absolute': True,
                    'extraction_method': 'ecommerce_optimized'
                }
            }
            
        except Exception as e:
            logger.error(f"Error in product extraction: {str(e)}")
            return {
                'products': [],
                'metadata': {
                    'extraction_method': 'ecommerce_failed',
                    'error': str(e)
                }
            }
    
    def auto_detect_products(self, html_content: str, 
                           base_url: str = '',
                           limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Auto-detect e-commerce products using common patterns
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Common product container selectors
            product_selectors = [
                '.product', '.item', '.card', '.product-item', '.product-card',
                '.listing-item', '.search-result', '.grid-item', '[data-product]',
                '.product-tile', '.product-box', '.merchandise'
            ]
            
            best_selector = None
            max_products = 0
            
            # Find the selector that gives us the most products
            for selector in product_selectors:
                try:
                    elements = soup.select(selector)
                    if len(elements) > max_products and len(elements) >= 2:
                        max_products = len(elements)
                        best_selector = selector
                except:
                    continue
            
            if not best_selector:
                return {
                    'products': [],
                    'metadata': {
                        'extraction_method': 'auto_detect_failed',
                        'error': 'No product containers found'
                    }
                }
            
            # Build field selectors using common patterns
            field_selectors = {}
            for field, selectors in self.common_ecommerce_selectors.items():
                field_selectors[field] = ', '.join(selectors)
            
            # Extract products using detected selector
            return self.extract_products(
                html_content, best_selector, field_selectors, base_url, limit
            )
            
        except Exception as e:
            logger.error(f"Error in auto-detection: {str(e)}")
            return {
                'products': [],
                'metadata': {
                    'extraction_method': 'auto_detect_failed',
                    'error': str(e)
                }
            }


class EcommerceCrawlerService:
    """Crawler service specifically for e-commerce product extraction"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.default_timeout = self.config.get('CRAWLER_TIMEOUT', 25)
        self.extractor = EcommerceProductExtractor()
        
        # User agents for rotation
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        ]
    
    def get_random_delay(self) -> float:
        """Random delay between requests"""
        return random.uniform(0.5, 1.2)
    
    async def scrape_products(self, url: str, 
                            product_selector: str,
                            field_selectors: Dict[str, str],
                            limit: Optional[int] = None,
                            exclude_selectors: Optional[List[str]] = None) -> CrawlResult:
        """
        Scrape e-commerce products from a website
        
        Args:
            url: Website URL to scrape
            product_selector: CSS selector for product containers
            field_selectors: Dict mapping field names to CSS selectors
            limit: Maximum number of products to extract
            exclude_selectors: List of CSS selectors to exclude
            
        Returns:
            CrawlResult with extracted products
        """
        start_time = time.time()
        
        try:
            # Validate URL
            if not validate_url(url):
                return CrawlResult(
                    success=False,
                    url=url,
                    error="Invalid URL format"
                )
            
            # Validate selectors
            if not product_selector:
                return CrawlResult(
                    success=False,
                    url=url,
                    error="product_selector is required"
                )
            
            if not field_selectors or not isinstance(field_selectors, dict):
                return CrawlResult(
                    success=False,
                    url=url,
                    error="field_selectors must be a non-empty dictionary"
                )
            
            # Add delay
            await asyncio.sleep(self.get_random_delay())
            
            # Configure crawler
            crawler_params = {
                'verbose': False,
                'headless': True,
                'user_agent': random.choice(self.user_agents)
            }
            
            # Crawl the URL
            try:
                async with AsyncWebCrawler(**crawler_params) as crawler:
                    result = await asyncio.wait_for(
                        crawler.arun(
                            url=url,
                            word_count_threshold=1,
                            extraction_strategy=NoExtractionStrategy(),
                            bypass_cache=False,
                            delay_before_return_html=2.0
                        ),
                        timeout=self.default_timeout
                    )
                    
                    return self._process_ecommerce_result(
                        result, url, product_selector, field_selectors, 
                        limit, exclude_selectors, time.time() - start_time
                    )
                    
            except TypeError:
                # Fallback for older API
                crawler = AsyncWebCrawler(**crawler_params)
                result = await asyncio.wait_for(
                    crawler.arun(url=url) if hasattr(crawler, 'arun') else asyncio.to_thread(crawler.run, url),
                    timeout=self.default_timeout
                )
                
                return self._process_ecommerce_result(
                    result, url, product_selector, field_selectors, 
                    limit, exclude_selectors, time.time() - start_time
                )
                
        except asyncio.TimeoutError:
            return CrawlResult(
                success=False,
                url=url,
                error=f"E-commerce scraping timeout after {self.default_timeout}s"
            )
        except Exception as e:
            logger.error(f"E-commerce scraping error for {url}: {str(e)}")
            return CrawlResult(
                success=False,
                url=url,
                error=f"E-commerce scraping failed: {str(e)}"
            )
    
    async def auto_scrape_products(self, url: str, 
                                 limit: Optional[int] = None) -> CrawlResult:
        """
        Auto-detect and scrape e-commerce products
        """
        start_time = time.time()
        
        try:
            # Validate URL
            if not validate_url(url):
                return CrawlResult(
                    success=False,
                    url=url,
                    error="Invalid URL format"
                )
            
            # Add delay
            await asyncio.sleep(self.get_random_delay())
            
            # Configure crawler
            crawler_params = {
                'verbose': False,
                'headless': True,
                'user_agent': random.choice(self.user_agents)
            }
            
            # Crawl the URL
            try:
                async with AsyncWebCrawler(**crawler_params) as crawler:
                    result = await asyncio.wait_for(
                        crawler.arun(
                            url=url,
                            word_count_threshold=1,
                            extraction_strategy=NoExtractionStrategy(),
                            bypass_cache=False,
                            delay_before_return_html=2.0
                        ),
                        timeout=self.default_timeout
                    )
                    
                    return self._process_auto_result(
                        result, url, limit, time.time() - start_time
                    )
                    
            except TypeError:
                # Fallback for older API
                crawler = AsyncWebCrawler(**crawler_params)
                result = await asyncio.wait_for(
                    crawler.arun(url=url) if hasattr(crawler, 'arun') else asyncio.to_thread(crawler.run, url),
                    timeout=self.default_timeout
                )
                
                return self._process_auto_result(
                    result, url, limit, time.time() - start_time
                )
                
        except asyncio.TimeoutError:
            return CrawlResult(
                success=False,
                url=url,
                error=f"Auto e-commerce scraping timeout after {self.default_timeout}s"
            )
        except Exception as e:
            logger.error(f"Auto e-commerce scraping error for {url}: {str(e)}")
            return CrawlResult(
                success=False,
                url=url,
                error=f"Auto e-commerce scraping failed: {str(e)}"
            )
    
    def _process_ecommerce_result(self, result: Any, url: str, 
                                product_selector: str,
                                field_selectors: Dict[str, str],
                                limit: Optional[int],
                                exclude_selectors: Optional[List[str]],
                                crawl_time: float) -> CrawlResult:
        """Process crawl result for e-commerce extraction"""
        try:
            # Check if crawl was successful
            if hasattr(result, 'success') and not result.success:
                return CrawlResult(
                    success=False,
                    url=url,
                    error=getattr(result, 'error_message', 'E-commerce scraping failed')
                )
            
            # Get HTML content
            html_content = ''
            if hasattr(result, 'html'):
                html_content = result.html
            elif hasattr(result, 'cleaned_html'):
                html_content = result.cleaned_html
            else:
                return CrawlResult(
                    success=False,
                    url=url,
                    error="No HTML content found"
                )
            
            # Extract products
            extraction_result = self.extractor.extract_products(
                html_content, product_selector, field_selectors, url, limit, exclude_selectors
            )
            
            products = extraction_result["products"]
            extraction_metadata = extraction_result["metadata"]
            
            # Extract title
            title = ''
            if hasattr(result, 'title'):
                title = result.title
            else:
                try:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    title_tag = soup.find('title')
                    if title_tag:
                        title = title_tag.get_text().strip()
                except:
                    title = ''
            
            # Prepare metadata
            metadata = {
                'crawl_time': round(crawl_time, 2),
                'status_code': getattr(result, 'status_code', 200),
                'extraction_mode': 'ecommerce_custom',
                'product_selector': product_selector,
                'field_selectors': field_selectors,
                'exclude_selectors': exclude_selectors or [],
                'products': products,
                **extraction_metadata
            }
            
            return CrawlResult(
                success=True,
                url=url,
                title=title[:200] if title else '',
                content=f"Extracted {len(products)} products",
                word_count=sum(p.get('word_count', 0) for p in products),
                images=[],
                internal_links=[],
                external_links=[],
                metadata=metadata
            )
            
        except Exception as e:
            logger.error(f"Error processing e-commerce result: {str(e)}")
            return CrawlResult(
                success=False,
                url=url,
                error=f"E-commerce processing failed: {str(e)}"
            )
    
    def _process_auto_result(self, result: Any, url: str, 
                           limit: Optional[int],
                           crawl_time: float) -> CrawlResult:
        """Process crawl result for auto e-commerce extraction"""
        try:
            # Check if crawl was successful
            if hasattr(result, 'success') and not result.success:
                return CrawlResult(
                    success=False,
                    url=url,
                    error=getattr(result, 'error_message', 'Auto e-commerce scraping failed')
                )
            
            # Get HTML content
            html_content = ''
            if hasattr(result, 'html'):
                html_content = result.html
            elif hasattr(result, 'cleaned_html'):
                html_content = result.cleaned_html
            else:
                return CrawlResult(
                    success=False,
                    url=url,
                    error="No HTML content found"
                )
            
            # Auto-detect and extract products
            extraction_result = self.extractor.auto_detect_products(
                html_content, url, limit
            )
            
            products = extraction_result["products"]
            extraction_metadata = extraction_result["metadata"]
            
            # Extract title
            title = ''
            if hasattr(result, 'title'):
                title = result.title
            else:
                try:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    title_tag = soup.find('title')
                    if title_tag:
                        title = title_tag.get_text().strip()
                except:
                    title = ''
            
            # Prepare metadata
            metadata = {
                'crawl_time': round(crawl_time, 2),
                'status_code': getattr(result, 'status_code', 200),
                'extraction_mode': 'ecommerce_auto',
                'products': products,
                **extraction_metadata
            }
            
            return CrawlResult(
                success=True,
                url=url,
                title=title[:200] if title else '',
                content=f"Auto-detected {len(products)} products",
                word_count=sum(p.get('word_count', 0) for p in products),
                images=[],
                internal_links=[],
                external_links=[],
                metadata=metadata
            )
            
        except Exception as e:
            logger.error(f"Error processing auto e-commerce result: {str(e)}")
            return CrawlResult(
                success=False,
                url=url,
                error=f"Auto e-commerce processing failed: {str(e)}"
            )

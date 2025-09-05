# app/api/v1/ecommerce.py
from flask import request, jsonify, current_app
import asyncio
import time
import traceback
import os
from functools import wraps

from app.api.v1 import api_v1
from app.services.ecommerce_scraper_service import EcommerceCrawlerService
from app.utils.validators import validate_crawl_request
from app.utils.response_helpers import success_response, error_response

# Rate limiting decorator
def apply_rate_limit(limit_string):
    """Apply rate limit only in production without valid API key"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if os.getenv('FLASK_ENV', 'production') == 'development':
                return f(*args, **kwargs)
            
            api_key = request.headers.get('X-API-Key')
            valid_api_key = os.getenv('API_KEY', '')
            
            if api_key and api_key == valid_api_key and valid_api_key:
                return f(*args, **kwargs)
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator

def safe_async_run(coro, timeout=30):
    """Safely run async coroutine with proper event loop handling"""
    try:
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=timeout)
        except RuntimeError:
            return asyncio.run(coro)
    except Exception as e:
        current_app.logger.error(f"Async execution error: {str(e)}")
        raise e

@api_v1.route('/ecommerce/scrape', methods=['POST'])
@apply_rate_limit("10 per minute")
def scrape_ecommerce_products():
    """
    Scrape e-commerce products from a website using custom selectors
    
    Request Example:
    {
        "url": "https://shop.example.com/products",
        "product_selector": ".product-item",
        "selectors": {
            "title": "h3 a, .product-title",
            "price": ".price, .cost",
            "original_price": ".was-price, .old-price",
            "image": "img",
            "link": "a",
            "rating": ".rating, .stars",
            "reviews": ".review-count",
            "brand": ".brand",
            "availability": ".stock-status",
            "discount": ".discount-badge"
        },
        "config": {
            "limit": 50,
            "exclude_selectors": [".ads", ".sponsored"]
        }
    }
    
    Response:
    {
        "success": true,
        "data": {
            "url": "https://shop.example.com/products",
            "total_products": 25,
            "products": [
                {
                    "index": 0,
                    "title": "Product Name",
                    "price": "$29.99",
                    "original_price": "$39.99",
                    "image": "https://shop.example.com/images/product1.jpg",
                    "link": "https://shop.example.com/product/123",
                    "rating": "4.5/5",
                    "reviews": "123 reviews",
                    "brand": "Brand Name",
                    "availability": "In Stock",
                    "discount": "25% OFF",
                    "word_count": 3
                }
            ],
            "extraction_info": {
                "extraction_time": 3.2,
                "order_preserved": true,
                "urls_absolute": true,
                "deduplication_applied": true
            }
        }
    }
    """
    start_time = time.time()
    
    try:
        data = request.get_json()
        
        # Validate basic request
        is_valid, error_msg = validate_crawl_request(data)
        if not is_valid:
            return error_response(error_msg, 400)
        
        url = data['url']
        product_selector = data.get('product_selector', '')
        selectors = data.get('selectors', {})
        config = data.get('config', {})
        
        # Validation
        if not product_selector:
            return error_response("'product_selector' field is required - specify CSS selector for product containers", 400)
        
        if not selectors or not isinstance(selectors, dict):
            return error_response("'selectors' field is required - specify field selectors as a dictionary", 400)
        
        # Extract configuration
        limit = min(config.get('limit', 100), 200)  # Max 200 products
        exclude_selectors = config.get('exclude_selectors', [])
        
        current_app.logger.info(f"E-commerce scraping request:")
        current_app.logger.info(f"  URL: {url}")
        current_app.logger.info(f"  Product selector: {product_selector}")
        current_app.logger.info(f"  Field selectors: {list(selectors.keys())}")
        current_app.logger.info(f"  Limit: {limit}")
        
        # Initialize crawler service
        crawler_service = EcommerceCrawlerService(current_app.config)
        
        # Run extraction
        try:
            result = safe_async_run(
                crawler_service.scrape_products(
                    url, product_selector, selectors, limit, exclude_selectors
                ),
                timeout=45
            )
        except asyncio.TimeoutError:
            return error_response("E-commerce scraping timeout after 45 seconds", 408)
        except Exception as crawl_error:
            current_app.logger.error(f"E-commerce scraping failed: {str(crawl_error)}")
            current_app.logger.error(f"Traceback: {traceback.format_exc()}")
            return error_response(f"E-commerce scraping failed: {str(crawl_error)}", 500)
        
        # Process results
        total_time = time.time() - start_time
        
        if result and result.success:
            # Extract products from the result
            products = result.metadata.get('products', [])
            extraction_metadata = {
                k: v for k, v in result.metadata.items() 
                if k not in ['products']
            }
            
            # Create clean response
            response_data = {
                'url': url,
                'product_selector': product_selector,
                'total_products': len(products),
                'products': products,
                'extraction_info': {
                    'field_selectors_used': list(selectors.keys()),
                    'exclude_selectors_used': exclude_selectors,
                    'extraction_time': round(total_time, 2),
                    'order_preserved': True,
                    'urls_absolute': True,
                    **extraction_metadata
                }
            }
            
            current_app.logger.info(f"E-commerce scraping successful:")
            current_app.logger.info(f"  Total products found: {len(products)}")
            current_app.logger.info(f"  Fields per product: {list(products[0].keys()) if products else []}")
            
            return success_response(response_data)
        
        else:
            error_msg = result.error if result else "E-commerce scraping failed"
            return error_response(error_msg, 400)
            
    except Exception as e:
        current_app.logger.error(f"E-commerce scraping endpoint error: {str(e)}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return error_response("Internal server error", 500)

@api_v1.route('/ecommerce/auto-scrape', methods=['POST'])
@apply_rate_limit("8 per minute")
def auto_scrape_ecommerce_products():
    """
    Auto-detect and scrape e-commerce products from a website
    
    Request Example:
    {
        "url": "https://shop.example.com/products",
        "config": {
            "limit": 30
        }
    }
    
    Automatically detects common e-commerce fields:
    - title, price, original_price, image, link, rating, reviews, brand, availability, discount, description
    """
    start_time = time.time()
    
    try:
        data = request.get_json()
        
        # Validate basic request
        is_valid, error_msg = validate_crawl_request(data)
        if not is_valid:
            return error_response(error_msg, 400)
        
        url = data['url']
        config = data.get('config', {})
        
        # Extract configuration
        limit = min(config.get('limit', 50), 100)  # Max 100 products for auto-detection
        
        current_app.logger.info(f"Auto e-commerce scraping request:")
        current_app.logger.info(f"  URL: {url}")
        current_app.logger.info(f"  Limit: {limit}")
        
        # Initialize crawler service
        crawler_service = EcommerceCrawlerService(current_app.config)
        
        # Run auto-extraction
        try:
            result = safe_async_run(
                crawler_service.auto_scrape_products(url, limit),
                timeout=40
            )
        except asyncio.TimeoutError:
            return error_response("Auto e-commerce scraping timeout after 40 seconds", 408)
        except Exception as crawl_error:
            current_app.logger.error(f"Auto e-commerce scraping failed: {str(crawl_error)}")
            current_app.logger.error(f"Traceback: {traceback.format_exc()}")
            return error_response(f"Auto e-commerce scraping failed: {str(crawl_error)}", 500)
        
        # Process results
        total_time = time.time() - start_time
        
        if result and result.success:
            # Extract products from the result
            products = result.metadata.get('products', [])
            extraction_metadata = {
                k: v for k, v in result.metadata.items() 
                if k not in ['products']
            }
            
            # Create clean response
            response_data = {
                'url': url,
                'total_products': len(products),
                'products': products,
                'extraction_info': {
                    'mode': 'auto_detection',
                    'extraction_time': round(total_time, 2),
                    'order_preserved': True,
                    'urls_absolute': True,
                    'fields_detected': list(products[0].keys()) if products else [],
                    **extraction_metadata
                }
            }
            
            current_app.logger.info(f"Auto e-commerce scraping successful:")
            current_app.logger.info(f"  Total products found: {len(products)}")
            current_app.logger.info(f"  Auto-detected fields: {list(products[0].keys()) if products else []}")
            
            return success_response(response_data)
        
        else:
            error_msg = result.error if result else "Auto e-commerce scraping failed"
            return error_response(error_msg, 400)
            
    except Exception as e:
        current_app.logger.error(f"Auto e-commerce scraping endpoint error: {str(e)}")
        current_app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return error_response("Internal server error", 500)

@api_v1.route('/ecommerce/demo', methods=['GET'])
@apply_rate_limit("30 per minute")
def demo_ecommerce_scraping():
    """Demo endpoint showing e-commerce scraping usage"""
    try:
        return success_response({
            "message": "E-commerce Product Scraper API",
            "description": "Extract multiple products from e-commerce websites with custom selectors",
            "key_features": {
                "custom_selectors": "Define your own CSS selectors for any product field",
                "auto_detection": "Automatically detect common e-commerce fields",
                "order_preservation": "Products returned in same order as they appear on webpage",
                "absolute_urls": "All image and link URLs converted to absolute URLs",
                "deduplication": "Removes duplicate products while preserving order",
                "price_extraction": "Smart price extraction with currency support",
                "rating_extraction": "Extract ratings in various formats"
            },
            "endpoints": {
                "custom": {
                    "url": "POST /api/v1/ecommerce/scrape",
                    "description": "Scrape products using custom field selectors"
                },
                "auto": {
                    "url": "POST /api/v1/ecommerce/auto-scrape",
                    "description": "Auto-detect and scrape common e-commerce fields"
                }
            },
            "example_custom_request": {
                "url": "https://shop.example.com/products",
                "product_selector": ".product-item",
                "selectors": {
                    "title": "h3 a, .product-title",
                    "price": ".price, .cost",
                    "original_price": ".was-price",
                    "image": "img",
                    "link": "a",
                    "rating": ".rating",
                    "brand": ".brand"
                },
                "config": {
                    "limit": 50,
                    "exclude_selectors": [".ads"]
                }
            },
            "example_auto_request": {
                "url": "https://shop.example.com/products",
                "config": {
                    "limit": 30
                }
            },
            "example_response_product": {
                "index": 0,
                "title": "Product Name",
                "price": "$29.99",
                "original_price": "$39.99",
                "image": "https://shop.example.com/images/product.jpg",
                "link": "https://shop.example.com/product/123",
                "rating": "4.5/5",
                "reviews": "123 reviews",
                "brand": "Brand Name",
                "availability": "In Stock",
                "discount": "25% OFF",
                "word_count": 3,
                "note": "index 0 = first product on page, URLs are absolute"
            },
            "supported_fields": {
                "basic": ["title", "price", "original_price", "image", "link"],
                "advanced": ["rating", "reviews", "brand", "availability", "discount", "description"],
                "custom": "Any field name with corresponding CSS selector"
            },
            "python_usage": '''
import requests

# Custom selectors
response = requests.post('http://localhost:5014/api/v1/ecommerce/scrape', json={
    "url": "https://shop.example.com/products",
    "product_selector": ".product-item",
    "selectors": {
        "title": "h3 a",
        "price": ".price",
        "image": "img",
        "link": "a"
    }
})

# Auto-detection
response = requests.post('http://localhost:5014/api/v1/ecommerce/auto-scrape', json={
    "url": "https://shop.example.com/products"
})

data = response.json()
if data['success']:
    for product in data['data']['products']:
        print(f"Product {product['index']}: {product['title']}")
        print(f"Price: {product['price']}")
        print(f"Image: {product['image']}")  # Absolute URL
        print(f"Link: {product['link']}")    # Absolute URL
            ''',
            "common_selectors": {
                "product_containers": [".product", ".item", ".card", ".product-item", ".product-card"],
                "titles": ["h1", "h2", "h3", ".product-title", ".title", ".name"],
                "prices": [".price", ".cost", ".amount", ".product-price"],
                "images": ["img", ".product-image img", ".item-image img"],
                "links": ["a", ".product-link", ".item-link"]
            },
            "tips": {
                "selector_testing": "Test your selectors in browser dev tools first",
                "limit_usage": "Use reasonable limits to avoid timeouts",
                "exclude_ads": "Use exclude_selectors to remove ads and unwanted content",
                "field_naming": "Use descriptive field names for better organization"
            },
            "status": "ready"
        })
    except Exception as e:
        return error_response(f"Demo failed: {str(e)}", 500)

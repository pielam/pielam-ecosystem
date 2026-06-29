# megamind/utils/service_fetcher.py

import requests
from bs4 import BeautifulSoup
from django.utils import timezone
import mimetypes
import base64
from urllib.parse import urljoin, urlparse
import json
import logging

logger = logging.getLogger(__name__)


class ServiceDataFetcher:
    """Enhanced fetcher that handles multiple content types"""
    
    SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml']
    SUPPORTED_VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/ogg', 'video/quicktime']
    SUPPORTED_AUDIO_TYPES = ['audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/mp4', 'audio/webm']
    SUPPORTED_DOCUMENT_TYPES = ['application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
    
    def __init__(self, service):
        self.service = service
        self.timeout = 30
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # Add authentication if available
        if service.api_key:
            self.headers['Authorization'] = f'Bearer {service.api_key}'
        if service.auth_token:
            self.headers['X-Auth-Token'] = service.auth_token
    
    def fetch(self):
        """Main fetch method that routes to appropriate handler"""
        try:
            response = requests.get(
                self.service.service_url,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
            
            # Route to appropriate handler based on content type
            if content_type in self.SUPPORTED_IMAGE_TYPES:
                return self._handle_image(response, content_type)
            elif content_type in self.SUPPORTED_VIDEO_TYPES:
                return self._handle_video(response, content_type)
            elif content_type in self.SUPPORTED_AUDIO_TYPES:
                return self._handle_audio(response, content_type)
            elif content_type in self.SUPPORTED_DOCUMENT_TYPES:
                return self._handle_document(response, content_type)
            elif 'application/json' in content_type:
                return self._handle_json(response)
            elif 'text/html' in content_type:
                return self._handle_html(response)
            elif 'text/plain' in content_type:
                return self._handle_text(response)
            else:
                return self._handle_generic(response, content_type)
                
        except requests.Timeout:
            raise Exception(f"Request timeout after {self.timeout} seconds")
        except requests.RequestException as e:
            raise Exception(f"Request failed: {str(e)}")
        except Exception as e:
            raise Exception(f"Unexpected error: {str(e)}")
    
    def _handle_image(self, response, content_type):
        """Handle image content"""
        # Convert to base64 for storage
        image_data = base64.b64encode(response.content).decode('utf-8')
        
        # Extract metadata
        size = len(response.content)
        
        return {
            'type': 'image',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'data': image_data,
            'preview_url': f"data:{content_type};base64,{image_data[:1000]}...",  # Preview only
            'dimensions': self._get_image_dimensions(response.content, content_type),
            'fetched_at': timezone.now().isoformat()
        }
    
    def _handle_video(self, response, content_type):
        """Handle video content - store metadata only due to size"""
        size = len(response.content)
        
        return {
            'type': 'video',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'url': self.service.service_url,
            'message': 'Video content detected. File too large to store, showing metadata only.',
            'duration': self._extract_video_duration(response.content),
            'fetched_at': timezone.now().isoformat()
        }
    
    def _handle_audio(self, response, content_type):
        """Handle audio content"""
        size = len(response.content)
        
        # For smaller audio files, we can store them
        if size < 5 * 1024 * 1024:  # Less than 5MB
            audio_data = base64.b64encode(response.content).decode('utf-8')
            return {
                'type': 'audio',
                'content_type': content_type,
                'size': size,
                'size_human': self._format_bytes(size),
                'data': audio_data,
                'url': self.service.service_url,
                'duration': self._extract_audio_duration(response.content),
                'fetched_at': timezone.now().isoformat()
            }
        else:
            return {
                'type': 'audio',
                'content_type': content_type,
                'size': size,
                'size_human': self._format_bytes(size),
                'url': self.service.service_url,
                'message': 'Audio file too large to store. Showing metadata only.',
                'fetched_at': timezone.now().isoformat()
            }
    
    def _handle_document(self, response, content_type):
        """Handle document files (PDF, Word, etc.)"""
        size = len(response.content)
        doc_data = base64.b64encode(response.content).decode('utf-8')
        
        return {
            'type': 'document',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'data': doc_data,
            'url': self.service.service_url,
            'filename': self._extract_filename(response),
            'fetched_at': timezone.now().isoformat()
        }
    
    def _handle_json(self, response):
        """Handle JSON API responses"""
        try:
            data = response.json()
            return {
                'type': 'json',
                'content_type': 'application/json',
                'data': data,
                'size': len(response.content),
                'size_human': self._format_bytes(len(response.content)),
                'fetched_at': timezone.now().isoformat()
            }
        except json.JSONDecodeError:
            return {
                'type': 'json',
                'content_type': 'application/json',
                'error': 'Invalid JSON response',
                'raw_content': response.text[:1000],
                'fetched_at': timezone.now().isoformat()
            }
    
    def _handle_html(self, response):
        """Enhanced HTML handler - extracts comprehensive data"""
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Extract basic page info
        title = soup.find('title')
        title_text = title.get_text(strip=True) if title else 'No title'
        
        # Extract meta information
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        description = meta_desc.get('content', '') if meta_desc else ''
        
        meta_keywords = soup.find('meta', attrs={'name': 'keywords'})
        keywords = meta_keywords.get('content', '') if meta_keywords else ''
        
        # Extract Open Graph data
        og_data = {}
        for og_tag in soup.find_all('meta', property=lambda x: x and x.startswith('og:')):
            property_name = og_tag.get('property', '').replace('og:', '')
            og_data[property_name] = og_tag.get('content', '')
        
        # Extract all headings
        headings = {
            'h1': [h.get_text(strip=True) for h in soup.find_all('h1')],
            'h2': [h.get_text(strip=True) for h in soup.find_all('h2')],
            'h3': [h.get_text(strip=True) for h in soup.find_all('h3')],
        }
        
        # Extract all links
        links = []
        for a in soup.find_all('a', href=True):
            link_text = a.get_text(strip=True)
            link_url = urljoin(self.service.service_url, a['href'])
            if link_text and link_url:
                links.append({'text': link_text, 'url': link_url})
        
        # Extract all paragraphs
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)]
        
        # Extract all lists
        lists = []
        for ul in soup.find_all(['ul', 'ol']):
            list_items = [li.get_text(strip=True) for li in ul.find_all('li')]
            if list_items:
                lists.append(list_items)
        
        # Extract tables
        tables = []
        for table in soup.find_all('table'):
            table_data = []
            for row in table.find_all('tr'):
                cells = [cell.get_text(strip=True) for cell in row.find_all(['td', 'th'])]
                if cells:
                    table_data.append(cells)
            if table_data:
                tables.append(table_data)
        
        # Extract product information (common e-commerce patterns)
        product_info = {}
        
        # Try to find price
        price_selectors = [
            {'class': 'price'},
            {'class': 'product-price'},
            {'itemprop': 'price'},
            {'class': lambda x: x and 'price' in x.lower()},
        ]
        for selector in price_selectors:
            price_elem = soup.find(['span', 'div', 'p'], selector)
            if price_elem:
                product_info['price'] = price_elem.get_text(strip=True)
                break
        
        # Try to find product specifications
        spec_keywords = ['specification', 'specs', 'features', 'details', 'technical']
        specifications = {}
        
        for keyword in spec_keywords:
            spec_section = soup.find(['div', 'section', 'table'], 
                                    class_=lambda x: x and keyword in x.lower())
            if spec_section:
                # Extract key-value pairs from the section
                for item in spec_section.find_all(['li', 'tr', 'div']):
                    text = item.get_text(strip=True)
                    if ':' in text:
                        key, value = text.split(':', 1)
                        specifications[key.strip()] = value.strip()
        
        # Extract all images with more details
        images = []
        for img in soup.find_all('img'):
            img_src = img.get('src') or img.get('data-src')
            if img_src:
                img_url = urljoin(self.service.service_url, img_src)
                images.append({
                    'url': img_url,
                    'alt': img.get('alt', ''),
                    'title': img.get('title', ''),
                })
        
        # Extract videos
        videos = []
        for video in soup.find_all('video'):
            video_src = video.get('src')
            if video_src:
                videos.append(urljoin(self.service.service_url, video_src))
            for source in video.find_all('source'):
                src = source.get('src')
                if src:
                    videos.append(urljoin(self.service.service_url, src))
        
        # Extract iframes (YouTube, Vimeo, etc.)
        iframes = []
        for iframe in soup.find_all('iframe'):
            iframe_src = iframe.get('src')
            if iframe_src:
                iframes.append({
                    'url': iframe_src,
                    'title': iframe.get('title', ''),
                })
        
        # Remove scripts, styles, nav, footer, header for clean content
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        
        # Extract main content
        main_content = soup.find(['main', 'article']) or soup.find('body')
        text_content = main_content.get_text(separator='\n', strip=True) if main_content else ''
        
        # Extract structured data (JSON-LD)
        structured_data = []
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                structured_data.append(data)
            except:
                pass
        
        return {
            'type': 'html',
            'content_type': 'text/html',
            
            # Basic Info
            'title': title_text,
            'description': description,
            'keywords': keywords,
            
            # Structured Data
            'og_data': og_data,
            'structured_data': structured_data,
            
            # Content Structure
            'headings': headings,
            'paragraphs': paragraphs[:20],  # First 20 paragraphs
            'lists': lists,
            'tables': tables,
            
            # Navigation
            'links': links[:50],  # First 50 links
            
            # Media
            'images': images[:30],  # First 30 images with details
            'videos': videos,
            'iframes': iframes,
            
            # Product Information
            'product_info': product_info,
            'specifications': specifications,
            
            # Full Content
            'content': text_content[:10000],  # First 10k characters
            'full_content': text_content,  # Complete text content
            
            # Metadata
            'url': self.service.service_url,
            'size': len(response.content),
            'size_human': self._format_bytes(len(response.content)),
            'fetched_at': timezone.now().isoformat()
        }
        
    def _handle_text(self, response):
        """Handle plain text content"""
        return {
            'type': 'text',
            'content_type': 'text/plain',
            'content': response.text[:10000],  # Limit to first 10k chars
            'size': len(response.content),
            'size_human': self._format_bytes(len(response.content)),
            'fetched_at': timezone.now().isoformat()
        }
    
    def _handle_generic(self, response, content_type):
        """Handle unknown content types"""
        return {
            'type': 'generic',
            'content_type': content_type,
            'size': len(response.content),
            'size_human': self._format_bytes(len(response.content)),
            'url': self.service.service_url,
            'message': f'Content type {content_type} detected but no specific handler available.',
            'fetched_at': timezone.now().isoformat()
        }
    
    def _format_bytes(self, size):
        """Convert bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} PB"
    
    def _get_image_dimensions(self, content, content_type):
        """Extract image dimensions (basic implementation)"""
        try:
            from PIL import Image
            from io import BytesIO
            
            img = Image.open(BytesIO(content))
            return {'width': img.width, 'height': img.height}
        except:
            return None
    
    def _extract_video_duration(self, content):
        """Extract video duration (placeholder - requires ffmpeg)"""
        return None  # Implement with ffmpeg-python if needed
    
    def _extract_audio_duration(self, content):
        """Extract audio duration (placeholder)"""
        return None  # Implement with mutagen or similar
    
    def _extract_filename(self, response):
        """Extract filename from Content-Disposition header or URL"""
        content_disp = response.headers.get('Content-Disposition', '')
        if 'filename=' in content_disp:
            return content_disp.split('filename=')[1].strip('"')
        
        # Fallback to URL path
        path = urlparse(self.service.service_url).path
        return path.split('/')[-1] or 'download'


def fetch_service_data(service):
    """
    Main function to fetch data from a connected service
    Updates the service object with fetched data
    """
    try:
        fetcher = ServiceDataFetcher(service)
        data = fetcher.fetch()
        
        service.last_fetched_data = data
        service.last_fetch_time = timezone.now()
        service.fetch_status = 'success'
        service.fetch_error = None
        service.save()
        
        logger.info(f"Successfully fetched data from {service.service_name}")
        return True
        
    except Exception as e:
        error_message = str(e)
        service.fetch_status = 'error'
        service.fetch_error = error_message
        service.last_fetch_time = timezone.now()
        service.save()
        
        logger.error(f"Failed to fetch data from {service.service_name}: {error_message}")
        return False
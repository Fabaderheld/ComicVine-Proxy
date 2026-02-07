#!/usr/bin/env python3
"""
ComicVine API Proxy Server

A transparent proxy that intercepts ComicVine API calls and:
1. First checks the local SQLite database
2. Falls back to the real ComicVine API if not found
3. Caches API responses in the database for future use
4. Returns responses in the same format as ComicVine API

Usage:
    python3 comicvine-proxy.py --db ~/path/to/localcv.db --port 8080

Then configure Kapowarr or other apps to use:
    http://localhost:8080 instead of https://comicvine.gamespot.com
"""

import os
import sys
import re
import json
import sqlite3
import argparse
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests

# Global configuration
COMICVINE_API_KEY = os.getenv('COMICVINE_API_KEY', '')
COMICVINE_BASE_URL = 'https://comicvine.gamespot.com'
LOCAL_DB_PATH = None
DB_CONN = None
VERBOSE = False


class ComicVineProxyDB:
    """Database interface for storing ComicVine API responses"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None
        self._init_database()
    
    def _init_database(self):
        """Initialize database with cache tables if they don't exist"""
        try:
            # Open in read-write mode
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            cursor = self.conn.cursor()
            
            # Create cache table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    response_data TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(resource_type, resource_id)
                )
            """)
            
            # Create index for faster lookups
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_resource_lookup 
                ON api_cache(resource_type, resource_id)
            """)
            
            self.conn.commit()
            
        except Exception as e:
            print(f"Error initializing database: {e}", file=sys.stderr)
            self.conn = None
    
    def get_cached(self, resource_type: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """Get cached response from database"""
        if not self.conn:
            return None
        
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT response_data FROM api_cache 
                WHERE resource_type = ? AND resource_id = ?
            """, (resource_type, str(resource_id)))
            
            result = cursor.fetchone()
            if result:
                return json.loads(result[0])
        except Exception as e:
            if VERBOSE:
                print(f"Error reading from cache: {e}", file=sys.stderr)
        
        return None
    
    def cache_response(self, resource_type: str, resource_id: str, response_data: Dict[str, Any]):
        """Store API response in database cache"""
        if not self.conn:
            return
        
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO api_cache 
                (resource_type, resource_id, response_data, cached_at)
                VALUES (?, ?, ?, ?)
            """, (resource_type, str(resource_id), json.dumps(response_data), datetime.now()))
            
            self.conn.commit()
            
            if VERBOSE:
                print(f"Cached {resource_type}/{resource_id}", file=sys.stderr)
        except Exception as e:
            if VERBOSE:
                print(f"Error caching response: {e}", file=sys.stderr)
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            self.conn = None


def parse_comicvine_url(path: str) -> Optional[Tuple[str, Optional[str], bool]]:
    """
    Parse ComicVine API URL to extract resource type, ID, and whether it's a list endpoint.
    
    Examples:
        /api/issue/4000-12345 -> ('issue', '12345', False)  # Detail endpoint
        /api/issues -> ('issue', None, True)  # List endpoint
        /api/volume/4050-67890 -> ('volume', '67890', False)
        /api/volumes -> ('volume', None, True)
    
    Returns:
        Tuple of (resource_type, resource_id, is_list) or None if not parseable
        resource_id is None for list endpoints
    """
    # Pattern for detail endpoints: /api/{type}/{prefix}-{id}
    detail_match = re.match(r'/api/(issue|volume|character|concept|object|origin|person|power|story_arc|team|location|video|publisher|series|episode|chat|video_type|video_category)/(\d+)-(\d+)', path)
    if detail_match:
        resource_type = detail_match.group(1)
        resource_id = detail_match.group(3)  # Use the ID after the prefix
        return (resource_type, resource_id, False)
    
    # Pattern for list endpoints: /api/{type}s (plural)
    list_match = re.match(r'/api/(issues|volumes|characters|concepts|objects|origins|people|powers|story_arcs|teams|locations|videos|publishers|series|episodes|video_types|video_categories)', path)
    if list_match:
        plural_type = list_match.group(1)
        # Convert plural to singular
        singular_map = {
            'issues': 'issue',
            'volumes': 'volume',
            'characters': 'character',
            'concepts': 'concept',
            'objects': 'object',
            'origins': 'origin',
            'people': 'person',
            'powers': 'power',
            'story_arcs': 'story_arc',
            'teams': 'team',
            'locations': 'location',
            'videos': 'video',
            'publishers': 'publisher',
            'series': 'series',
            'episodes': 'episode',
            'video_types': 'video_type',
            'video_categories': 'video_category'
        }
        resource_type = singular_map.get(plural_type, plural_type)
        return (resource_type, None, True)
    
    # Special case: /api/chat (singular but no ID)
    if path == '/api/chat':
        return ('chat', None, False)
    
    return None


def fetch_from_comicvine(resource_type: str, resource_id: Optional[str] = None, query_params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch data from real ComicVine API.
    
    Args:
        resource_type: Type of resource (issue, volume, character, etc.)
        resource_id: ID of the resource (None for list endpoints)
        query_params: Additional query parameters from the original request
    """
    if not COMICVINE_API_KEY:
        return None
    
    # Map resource types to ComicVine API prefixes (for detail endpoints)
    type_prefixes = {
        'issue': '4000',
        'volume': '4050',
        'character': '4005',
        'concept': '4015',
        'object': '4020',
        'origin': '4025',
        'person': '4040',
        'power': '4027',
        'story_arc': '4045',
        'team': '4060',
        'location': '4023',
        'video': '2300',
        'publisher': '4010',
        'series': '4070',
        'episode': '4075',
        'chat': None,  # No prefix needed
        'video_type': None,  # No prefix needed
        'video_category': None  # No prefix needed
    }
    
    # Build URL
    if resource_id:
        # Detail endpoint: /api/{type}/{prefix}-{id}
        prefix = type_prefixes.get(resource_type)
        if prefix is None:
            # Some resources don't use prefixes
            if resource_type in ['chat', 'video_type', 'video_category']:
                url = f"{COMICVINE_BASE_URL}/api/{resource_type}/{resource_id}"
            else:
                return None
        else:
            url = f"{COMICVINE_BASE_URL}/api/{resource_type}/{prefix}-{resource_id}"
    else:
        # List endpoint: /api/{type}s
        plural_map = {
            'issue': 'issues',
            'volume': 'volumes',
            'character': 'characters',
            'concept': 'concepts',
            'object': 'objects',
            'origin': 'origins',
            'person': 'people',
            'power': 'powers',
            'story_arc': 'story_arcs',
            'team': 'teams',
            'location': 'locations',
            'video': 'videos',
            'publisher': 'publishers',
            'series': 'series',
            'episode': 'episodes',
            'chat': 'chat',
            'video_type': 'video_types',
            'video_category': 'video_categories'
        }
        plural = plural_map.get(resource_type, f"{resource_type}s")
        url = f"{COMICVINE_BASE_URL}/api/{plural}"
    
    # Build params
    params = {
        'api_key': COMICVINE_API_KEY,
        'format': 'json'
    }
    
    # Add query parameters from original request (filters, sort, limit, offset, field_list, etc.)
    if query_params:
        for key, value in query_params.items():
            if key != 'api_key':  # Don't override our API key
                params[key] = value
    
    try:
        if VERBOSE:
            print(f"Fetching from ComicVine: {url}", file=sys.stderr)
            if query_params:
                print(f"  Query params: {query_params}", file=sys.stderr)
        
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        if VERBOSE:
            print(f"Error fetching from ComicVine: {e}", file=sys.stderr)
        return None


@app.route('/api/<path:api_path>', methods=['GET'])
def proxy_api(api_path: str):
    """Proxy ComicVine API requests"""
    full_path = f"/api/{api_path}"
    
    # Parse the URL to extract resource type, ID, and whether it's a list
    parsed = parse_comicvine_url(full_path)
    
    # Get query parameters
    query_params = dict(request.args)
    
    if not parsed:
        # If we can't parse it, forward directly to ComicVine
        if VERBOSE:
            print(f"Could not parse URL, forwarding: {full_path}", file=sys.stderr)
        return forward_request(full_path, query_params)
    
    resource_type, resource_id, is_list = parsed
    
    # For list endpoints, create a cache key based on query parameters
    # This allows caching different filtered/sorted results
    if is_list:
        # Create cache key from query params (excluding api_key)
        cache_key_parts = [f"{k}={v}" for k, v in sorted(query_params.items()) if k != 'api_key']
        cache_key = f"{resource_type}_list_{'_'.join(cache_key_parts)}" if cache_key_parts else f"{resource_type}_list_default"
        cache_resource_id = cache_key
    else:
        cache_resource_id = resource_id
    
    # Try to get from cache first (only for detail endpoints or if no filters)
    proxy_db = ComicVineProxyDB(LOCAL_DB_PATH) if LOCAL_DB_PATH else None
    cached = None
    
    # Only cache list endpoints if they have no filters (default list)
    # Detail endpoints are always cached
    should_cache = not is_list or (is_list and not query_params)
    
    if proxy_db and proxy_db.conn and should_cache:
        cached = proxy_db.get_cached(resource_type, cache_resource_id)
        if cached:
            if VERBOSE:
                print(f"Cache HIT: {resource_type}/{cache_resource_id}", file=sys.stderr)
            return jsonify(cached)
    
    if VERBOSE:
        print(f"Cache MISS: {resource_type}/{cache_resource_id}", file=sys.stderr)
    
    # Fetch from ComicVine API
    api_response = fetch_from_comicvine(resource_type, resource_id, query_params)
    
    if api_response:
        # Cache the response (only detail endpoints or unfiltered lists)
        if proxy_db and proxy_db.conn and should_cache:
            proxy_db.cache_response(resource_type, cache_resource_id, api_response)
        
        return jsonify(api_response)
    
    # If all else fails, forward the request directly
    return forward_request(full_path, query_params)


def forward_request(path: str, query_params: Dict[str, Any] = None):
    """Forward request directly to ComicVine API"""
    url = f"{COMICVINE_BASE_URL}{path}"
    params = query_params or dict(request.args)
    
    # Add API key if we have one
    if COMICVINE_API_KEY and 'api_key' not in params:
        params['api_key'] = COMICVINE_API_KEY
    
    # Ensure format is set
    if 'format' not in params:
        params['format'] = 'json'
    
    try:
        if VERBOSE:
            print(f"Forwarding request: {url}", file=sys.stderr)
        response = requests.get(url, params=params, timeout=30)
        return Response(
            response.content,
            status=response.status_code,
            mimetype='application/json'
        )
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    status = {
        'status': 'ok',
        'database': 'connected' if (LOCAL_DB_PATH and DB_CONN) else 'not_configured',
        'api_key': 'configured' if COMICVINE_API_KEY else 'missing'
    }
    return jsonify(status)


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with usage info"""
    return jsonify({
        'service': 'ComicVine API Proxy',
        'version': '1.0.0',
        'endpoints': {
            '/api/*': 'Proxy ComicVine API requests',
            '/health': 'Health check'
        },
        'usage': 'Configure your application to use this proxy URL instead of comicvine.gamespot.com'
    })


def main():
    global LOCAL_DB_PATH, DB_CONN, COMICVINE_API_KEY, VERBOSE
    
    parser = argparse.ArgumentParser(
        description='ComicVine API Proxy Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start proxy with local database
  python3 comicvine-proxy.py --db ~/localcv.db --port 8080

  # Start with API key for fallback
  python3 comicvine-proxy.py --db ~/localcv.db --api-key YOUR_KEY --port 8080

  # Verbose mode
  python3 comicvine-proxy.py --db ~/localcv.db --verbose

Environment Variables:
  COMICVINE_API_KEY    ComicVine API key (optional, for fallback)
        """
    )
    
    parser.add_argument(
        '--db',
        type=str,
        required=True,
        help='Path to local ComicVine SQLite database'
    )
    
    parser.add_argument(
        '--api-key',
        type=str,
        default=os.getenv('COMICVINE_API_KEY', ''),
        help='ComicVine API key (or set COMICVINE_API_KEY env var)'
    )
    
    parser.add_argument(
        '--port',
        type=int,
        default=8080,
        help='Port to listen on (default: 8080)'
    )
    
    parser.add_argument(
        '--host',
        type=str,
        default='127.0.0.1',
        help='Host to bind to (default: 127.0.0.1)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    VERBOSE = args.verbose
    LOCAL_DB_PATH = args.db
    COMICVINE_API_KEY = args.api_key
    
    # Validate database path
    if not os.path.exists(LOCAL_DB_PATH):
        print(f"Error: Database file not found: {LOCAL_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    
    # Test database connection
    try:
        test_db = ComicVineProxyDB(LOCAL_DB_PATH)
        if not test_db.conn:
            print(f"Error: Could not connect to database: {LOCAL_DB_PATH}", file=sys.stderr)
            sys.exit(1)
        test_db.close()
    except Exception as e:
        print(f"Error: Database connection failed: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Print startup info
    print(f"ComicVine API Proxy Server")
    print(f"==========================")
    print(f"Database: {LOCAL_DB_PATH}")
    print(f"API Key: {'Configured' if COMICVINE_API_KEY else 'Not configured (cache-only mode)'}")
    print(f"Listening on: http://{args.host}:{args.port}")
    print(f"Proxy URL: http://{args.host}:{args.port}/api/...")
    print(f"\nConfigure Kapowarr to use: http://{args.host}:{args.port}")
    print(f"Press Ctrl+C to stop\n")
    
    # Start Flask server
    try:
        app.run(host=args.host, port=args.port, debug=VERBOSE)
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)


if __name__ == '__main__':
    main()

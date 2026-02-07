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
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extras import RealDictCursor
from psycopg2 import sql

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests

# Global configuration
COMICVINE_API_KEY = os.getenv('COMICVINE_API_KEY', '')
COMICVINE_BASE_URL = 'https://comicvine.gamespot.com'
DB_CONFIG = None
DB_CONN = None
VERBOSE = False


class ComicVineProxyDB:
    """Database interface for storing ComicVine API responses"""

    def __init__(self, db_config: Dict[str, str]):
        self.db_config = db_config
        self.conn = None
        self._init_database()

    def _get_connection(self):
        """Get database connection"""
        try:
            conn = psycopg2.connect(
                host=self.db_config.get('host', 'localhost'),
                port=self.db_config.get('port', '5432'),
                database=self.db_config.get('database', 'comicvine'),
                user=self.db_config.get('user', 'comicvine'),
                password=self.db_config.get('password', 'comicvine')
            )
            return conn
        except Exception as e:
            if VERBOSE:
                print(f"Error connecting to database: {e}", file=sys.stderr)
            return None

    def _init_database(self):
        """Initialize database with cache tables if they don't exist"""
        try:
            self.conn = self._get_connection()
            if not self.conn:
                return

            cursor = self.conn.cursor()

            # Create cache table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    id SERIAL PRIMARY KEY,
                    resource_type VARCHAR(50) NOT NULL,
                    resource_id VARCHAR(255) NOT NULL,
                    response_data JSONB NOT NULL,
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
            if self.conn:
                self.conn.rollback()
            self.conn = None

    def _detect_schema(self):
        """Detect database schema by examining tables and columns"""
        if not self.conn:
            self.has_issue_table = False
            self.has_volume_table = False
            return

        # Initialize defaults
        self.has_issue_table = False
        self.has_volume_table = False
        self.issue_columns = []
        self.volume_columns = []

        try:
            cursor = self.conn.cursor()

            # Get all tables
            cursor.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            tables = [row[0] for row in cursor.fetchall()]

            # Look for cv_issue table
            if 'cv_issue' in tables:
                cursor.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'cv_issue'
                """)
                self.issue_columns = [row[0] for row in cursor.fetchall()]
                self.has_issue_table = True
                if VERBOSE:
                    print(f"Detected cv_issue table with columns: {self.issue_columns}", file=sys.stderr)

            # Look for cv_volume table
            if 'cv_volume' in tables:
                cursor.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'cv_volume'
                """)
                self.volume_columns = [row[0] for row in cursor.fetchall()]
                self.has_volume_table = True
                if VERBOSE:
                    print(f"Detected cv_volume table with columns: {self.volume_columns}", file=sys.stderr)

        except Exception as e:
            if VERBOSE:
                print(f"Error detecting schema: {e}", file=sys.stderr)
            self.has_issue_table = False
            self.has_volume_table = False

    def get_issue_from_db(self, issue_id: str) -> Optional[Dict[str, Any]]:
        """Get issue data directly from cv_issue table"""
        if not self.conn:
            return None

        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)

            # Check if table exists and what structure it has
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'cv_issue'
                )
            """)
            result = cursor.fetchone()
            table_exists = result['exists'] if result else False

            if not table_exists:
                return None

            # Try different possible table structures
            # Structure 1: id, data (JSONB) - from our import
            try:
                cursor.execute("SELECT data FROM cv_issue WHERE id = %s LIMIT 1", (issue_id,))
                result = cursor.fetchone()
                if result:
                    issue_data = result['data']
                    if VERBOSE:
                        print(f"Database HIT (cv_issue table): issue/{issue_id}", file=sys.stderr)
                    # Ensure issue_data is a dict (JSONB might be dict already)
                    if isinstance(issue_data, dict):
                        issue_data = dict(issue_data)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_issue (structure 1): {e}", file=sys.stderr)
                pass

            # Structure 2: Direct columns (original SQLite structure)
            try:
                cursor.execute("SELECT * FROM cv_issue WHERE id = %s LIMIT 1", (issue_id,))
                result = cursor.fetchone()
                if result:
                    issue_data = dict(result)
                    if VERBOSE:
                        print(f"Database HIT (cv_issue table, direct columns): issue/{issue_id}", file=sys.stderr)
                    # Ensure results is a dict
                    if isinstance(issue_data, dict):
                        issue_data = dict(issue_data)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_issue (structure 2): {e}", file=sys.stderr)
                pass

            # Try with integer
            try:
                issue_id_int = int(issue_id)
                cursor.execute("SELECT data FROM cv_issue WHERE id = %s LIMIT 1", (issue_id_int,))
                result = cursor.fetchone()
                if result:
                    issue_data = result['data']
                    if VERBOSE:
                        print(f"Database HIT (cv_issue table, integer ID): issue/{issue_id_int}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_issue (integer): {e}", file=sys.stderr)
                pass

        except Exception as e:
            if VERBOSE:
                print(f"Error querying issue from database: {e}", file=sys.stderr)

        return None

    def get_volume_from_db(self, volume_id: str) -> Optional[Dict[str, Any]]:
        """Get volume data directly from cv_volume table"""
        if not self.conn:
            return None

        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)

            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'cv_volume'
                )
            """)
            result = cursor.fetchone()
            table_exists = result['exists'] if result else False

            if not table_exists:
                return None

            # Try different possible table structures
            # Structure 1: id, data (JSONB) - from our import
            try:
                cursor.execute("SELECT data FROM cv_volume WHERE id = %s LIMIT 1", (volume_id,))
                result = cursor.fetchone()
                if result:
                    volume_data = result['data']
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table): volume/{volume_id}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_volume (structure 1): {e}", file=sys.stderr)
                pass

            # Structure 2: Direct columns (original SQLite structure)
            try:
                cursor.execute("SELECT * FROM cv_volume WHERE id = %s LIMIT 1", (volume_id,))
                result = cursor.fetchone()
                if result:
                    volume_data = dict(result)
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table, direct columns): volume/{volume_id}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_volume (structure 2): {e}", file=sys.stderr)
                pass

            # Try with integer
            try:
                volume_id_int = int(volume_id)
                cursor.execute("SELECT data FROM cv_volume WHERE id = %s LIMIT 1", (volume_id_int,))
                result = cursor.fetchone()
                if result:
                    volume_data = result['data']
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table, integer ID): volume/{volume_id_int}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data,
                        '_source': 'local_database_table'  # Indicator for testing
                    }
            except Exception as e:
                if VERBOSE:
                    print(f"Error querying cv_volume (integer): {e}", file=sys.stderr)
                pass

        except Exception as e:
            if VERBOSE:
                print(f"Error querying volume from database: {e}", file=sys.stderr)

        return None

    def get_resource_from_db(self, resource_type: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """Get resource data from the appropriate table based on resource type"""
        # Map resource types to table names
        table_map = {
            'issue': 'cv_issue',
            'volume': 'cv_volume',
            'person': 'cv_person',
            'publisher': 'cv_publisher',
            'character': 'cv_character',
            'concept': 'cv_concept',
            'object': 'cv_object',
            'origin': 'cv_origin',
            'power': 'cv_power',
            'story_arc': 'cv_story_arc',
            'team': 'cv_team',
            'location': 'cv_location',
            'series': 'cv_series',
            'episode': 'cv_episode'
        }

        table_name = table_map.get(resource_type)
        if not table_name:
            return None

        # Use existing methods for issue and volume, generic for others
        if resource_type == 'issue':
            return self.get_issue_from_db(resource_id)
        elif resource_type == 'volume':
            return self.get_volume_from_db(resource_id)
        else:
            # Generic lookup for other resource types
            return self._get_from_table(table_name, resource_id)

    def _get_from_table(self, table_name: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """Generic method to get data from any table"""
        if not self.conn:
            return None

        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)

            # Check if table exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                ) as exists
            """, (table_name,))
            result = cursor.fetchone()
            table_exists = result['exists'] if result else False

            if not table_exists:
                return None

            # Try to get data
            try:
                cursor.execute(f"SELECT data FROM {table_name} WHERE id = %s LIMIT 1", (resource_id,))
                result = cursor.fetchone()
                if result:
                    data = result['data']
                    if isinstance(data, dict):
                        data = dict(data)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': data,
                        '_source': 'local_database_table'
                    }
            except:
                pass

            # Try with integer
            try:
                resource_id_int = int(resource_id)
                cursor.execute(f"SELECT data FROM {table_name} WHERE id = %s LIMIT 1", (resource_id_int,))
                result = cursor.fetchone()
                if result:
                    data = result['data']
                    if isinstance(data, dict):
                        data = dict(data)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': data,
                        '_source': 'local_database_table'
                    }
            except:
                pass

        except Exception as e:
            if VERBOSE:
                print(f"Error querying {table_name} from database: {e}", file=sys.stderr)

        return None

    def get_cached(self, resource_type: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """Get cached response from database (DEPRECATED - kept for backwards compatibility)"""
        if not self.conn:
            # Try to reconnect
            self.conn = self._get_connection()
            if not self.conn:
                return None

        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT response_data FROM api_cache
                WHERE resource_type = %s AND resource_id = %s
            """, (resource_type, str(resource_id)))

            result = cursor.fetchone()
            if result:
                # PostgreSQL JSONB is already a dict
                cached_data = result['response_data']
                # Make a copy to avoid modifying the original (JSONB might be immutable)
                if isinstance(cached_data, dict):
                    cached_data = dict(cached_data)
                    cached_data['_source'] = 'api_cache'
                if VERBOSE:
                    print(f"Cache HIT (api_cache table): {resource_type}/{resource_id}", file=sys.stderr)
                return cached_data
            else:
                if VERBOSE:
                    print(f"Cache MISS: No record found for {resource_type}/{resource_id}", file=sys.stderr)
        except Exception as e:
            if VERBOSE:
                print(f"Error reading from cache: {e}", file=sys.stderr)
            # Try to reconnect on error
            try:
                self.conn.close()
            except:
                pass
            self.conn = self._get_connection()

        return None

    def cache_response(self, resource_type: str, resource_id: str, response_data: Dict[str, Any]):
        """Store API response in the correct table based on resource type"""
        if not self.conn:
            # Try to reconnect
            self.conn = self._get_connection()
            if not self.conn:
                return

        try:
            # Map resource types to table names
            table_map = {
                'issue': 'cv_issue',
                'volume': 'cv_volume',
                'person': 'cv_person',
                'publisher': 'cv_publisher',
                'character': 'cv_character',
                'concept': 'cv_concept',
                'object': 'cv_object',
                'origin': 'cv_origin',
                'power': 'cv_power',
                'story_arc': 'cv_story_arc',
                'team': 'cv_team',
                'location': 'cv_location',
                'series': 'cv_series',
                'episode': 'cv_episode'
            }

            table_name = table_map.get(resource_type)
            if not table_name:
                print(f"Warning: No table mapping for resource_type '{resource_type}', skipping cache", file=sys.stderr)
                return

            # Extract the actual data from ComicVine API response
            # ComicVine API returns: {"status_code": 1, "error": "OK", "results": {...}}
            if isinstance(response_data, dict) and 'results' in response_data:
                actual_data = response_data['results']
            else:
                actual_data = response_data

            # Ensure we have an ID
            if isinstance(actual_data, dict):
                actual_data = dict(actual_data)  # Make a copy
                resource_id_from_data = actual_data.get('id') or actual_data.get('cv_id')
                if resource_id_from_data:
                    resource_id = str(resource_id_from_data)

            # Create table if it doesn't exist
            cursor = self.conn.cursor()
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    id INTEGER PRIMARY KEY,
                    data JSONB
                )
            """)

            # Store in the correct table
            cursor.execute(f"""
                INSERT INTO {table_name} (id, data)
                VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (int(resource_id), json.dumps(actual_data)))

            self.conn.commit()
            print(f"[SOURCE] Cached {resource_type}/{resource_id} in {table_name} table", file=sys.stderr, flush=True)

        except Exception as e:
            print(f"Error caching response in {table_name}: {e}", file=sys.stderr, flush=True)
            if VERBOSE:
                import traceback
                traceback.print_exc(file=sys.stderr)
            self.conn.rollback()
            # Try to reconnect on error
            try:
                self.conn.close()
            except:
                pass
            self.conn = self._get_connection()

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

    # Set a proper User-Agent to avoid bot blocking
    headers = {
        'User-Agent': 'ComicVine-Proxy/1.0 (https://github.com/yourusername/ComicVine-Proxy)',
        'Accept': 'application/json'
    }

    try:
        if VERBOSE:
            print(f"Fetching from ComicVine: {url}", file=sys.stderr)
            if query_params:
                print(f"  Query params: {query_params}", file=sys.stderr)

        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        if VERBOSE:
            print(f"Error fetching from ComicVine: {e}", file=sys.stderr)
            if hasattr(e, 'response') and e.response is not None:
                print(f"  Response status: {e.response.status_code}", file=sys.stderr)
                print(f"  Response body: {e.response.text[:200]}", file=sys.stderr)
        return None


@app.route('/api/<path:api_path>', methods=['GET'])
def proxy_api(api_path: str):
    """Proxy ComicVine API requests"""
    full_path = f"/api/{api_path}"
    print(f"[SOURCE] Request received: {full_path}", file=sys.stderr, flush=True)

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

    # Initialize database connection
    proxy_db = ComicVineProxyDB(DB_CONFIG) if DB_CONFIG else None

    # For detail endpoints, try to get from database tables first
    if not is_list and resource_id and proxy_db and proxy_db.conn:
        db_result = proxy_db.get_resource_from_db(resource_type, resource_id)
        if db_result:
            print(f"[SOURCE] Database HIT (direct table): {resource_type}/{resource_id}", file=sys.stderr, flush=True)
            response = jsonify(db_result)
            response.headers['X-Data-Source'] = db_result.get('_source', 'local_database_table')
            return response

    # For list endpoints, we don't cache them (too many variations)
    # Just fetch directly from API
    if is_list:
        print(f"[SOURCE] List endpoint - fetching from API: {resource_type}", file=sys.stderr, flush=True)
        api_response = fetch_from_comicvine(resource_type, None, query_params)
        if api_response:
            response = jsonify(api_response)
            response.headers['X-Data-Source'] = 'comicvine_api'
            return response
        return forward_request(full_path, query_params)

    cache_resource_id = resource_id
    should_cache = True  # Always cache detail endpoints

    # Fetch from ComicVine API
    api_response = fetch_from_comicvine(resource_type, resource_id, query_params)

    if api_response:
        print(f"[SOURCE] API HIT (ComicVine API): {resource_type}/{cache_resource_id}", file=sys.stderr, flush=True)

        # Add source indicator BEFORE caching (so it's included in cached data too)
        # Create a new dict to ensure _source is properly included
        if isinstance(api_response, dict):
            # Deep copy to ensure we have a mutable dict
            import copy
            api_response = copy.deepcopy(api_response)
            api_response['_source'] = 'comicvine_api'

        # Cache the response if we have a database connection (with _source included)
        if proxy_db and proxy_db.conn and should_cache:
            try:
                proxy_db.cache_response(resource_type, cache_resource_id, api_response)
                print(f"[SOURCE] Cached response: {resource_type}/{cache_resource_id}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[SOURCE] Error caching response: {e}", file=sys.stderr, flush=True)

        response = jsonify(api_response)
        response.headers['X-Data-Source'] = 'comicvine_api'
        return response

    # If all else fails, forward the request directly
    return forward_request(full_path, query_params)


def forward_request(path: str, query_params: Dict[str, Any] = None):
    """Forward request directly to ComicVine API"""
    print(f"[SOURCE] Forwarding request directly to ComicVine: {path}", file=sys.stderr, flush=True)
    url = f"{COMICVINE_BASE_URL}{path}"
    params = query_params or dict(request.args)

    # Add API key if we have one
    if COMICVINE_API_KEY and 'api_key' not in params:
        params['api_key'] = COMICVINE_API_KEY

    # Ensure format is set
    if 'format' not in params:
        params['format'] = 'json'

    # Set a proper User-Agent to avoid bot blocking
    headers = {
        'User-Agent': 'ComicVine-Proxy/1.0 (https://github.com/yourusername/ComicVine-Proxy)',
        'Accept': 'application/json'
    }

    # Forward any additional headers from the original request
    if request.headers.get('Accept'):
        headers['Accept'] = request.headers.get('Accept')

    try:
        if VERBOSE:
            print(f"Forwarding request: {url}", file=sys.stderr)
        response = requests.get(url, params=params, headers=headers, timeout=30)
        flask_response = Response(
            response.content,
            status=response.status_code,
            mimetype='application/json'
        )
        flask_response.headers['X-Data-Source'] = 'comicvine_api'
        return flask_response
    except requests.exceptions.RequestException as e:
        if VERBOSE:
            print(f"Error forwarding request: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    db_status = 'not_configured'
    if DB_CONFIG:
        test_db = ComicVineProxyDB(DB_CONFIG)
        db_status = 'connected' if test_db.conn else 'connection_failed'
        if test_db.conn:
            test_db.close()

    status = {
        'status': 'ok',
        'database': db_status,
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


def import_sqlite_to_postgres(sqlite_path: str, db_config: Dict[str, str]):
    """Import data from SQLite database to PostgreSQL"""
    import shutil
    import tempfile

    # Resolve path and check if file exists
    original_path = sqlite_path
    sqlite_path = os.path.abspath(os.path.expanduser(sqlite_path))

    if VERBOSE:
        print(f"Checking SQLite file: {sqlite_path} (original: {original_path})", file=sys.stderr)

    if not os.path.exists(sqlite_path):
        print(f"Error: SQLite file not found: {sqlite_path}", file=sys.stderr)
        print(f"  Current working directory: {os.getcwd()}", file=sys.stderr)
        return False

    if not os.path.isfile(sqlite_path):
        print(f"Error: Path is not a file: {sqlite_path}", file=sys.stderr)
        return False

    # Check if file is readable
    if not os.access(sqlite_path, os.R_OK):
        print(f"Error: SQLite file is not readable: {sqlite_path}", file=sys.stderr)
        return False

    # Copy file to temporary writable location (SQLite may need to create WAL files)
    temp_dir = tempfile.mkdtemp()
    temp_db_path = os.path.join(temp_dir, 'localcv.db')

    try:
        print(f"Copying SQLite database to temporary location...", file=sys.stderr)
        shutil.copy2(sqlite_path, temp_db_path)
        print(f"Importing SQLite database from {sqlite_path}...", file=sys.stderr)

        # Connect to SQLite using the temporary copy
        # Since it's a copy, we can use normal read-write mode
        sqlite_conn = sqlite3.connect(temp_db_path, timeout=30.0)
        # Set journal mode to DELETE to avoid WAL files
        sqlite_conn.execute("PRAGMA journal_mode=DELETE")
        sqlite_conn.execute("PRAGMA locking_mode=NORMAL")
        sqlite_cursor = sqlite_conn.cursor()

        # Connect to PostgreSQL
        pg_conn = psycopg2.connect(
            host=db_config.get('host', 'localhost'),
            port=db_config.get('port', '5432'),
            database=db_config.get('database', 'comicvine'),
            user=db_config.get('user', 'comicvine'),
            password=db_config.get('password', 'comicvine')
        )
        pg_cursor = pg_conn.cursor()

        # Get all tables from SQLite
        sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in sqlite_cursor.fetchall()]

        print(f"Found {len(tables)} tables in SQLite database: {tables}", file=sys.stderr)

        imported_count = 0

        for table in tables:
            if table == 'sqlite_sequence':
                continue

            # Get table structure
            sqlite_cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in sqlite_cursor.fetchall()]

            # Get all data
            sqlite_cursor.execute(f"SELECT * FROM {table}")
            rows = sqlite_cursor.fetchall()

            if not rows:
                continue

            # Import to PostgreSQL
            print(f"Processing table: {table} ({len(rows)} rows)", file=sys.stderr)

            if table == 'api_cache':
                for row in rows:
                    try:
                        # Map SQLite row to PostgreSQL
                        resource_type = row[1] if len(row) > 1 else None
                        resource_id = row[2] if len(row) > 2 else None
                        response_data = json.loads(row[3]) if len(row) > 3 and row[3] else {}

                        if resource_type and resource_id:
                            pg_cursor.execute("""
                                INSERT INTO api_cache (resource_type, resource_id, response_data, cached_at)
                                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                                ON CONFLICT (resource_type, resource_id) DO NOTHING
                            """, (resource_type, resource_id, json.dumps(response_data)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from {table}: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            elif table == 'cv_issue':
                print(f"  Importing {len(rows)} rows from cv_issue...", file=sys.stderr)
                # Create cv_issue table if it doesn't exist
                pg_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS cv_issue (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)

                for row in rows:
                    try:
                        # Convert row to dict using column names
                        row_dict = dict(zip(columns, row))
                        issue_id = row_dict.get('id') or row_dict.get('cv_id')
                        if issue_id:
                            pg_cursor.execute("""
                                INSERT INTO cv_issue (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (issue_id, json.dumps(row_dict)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from cv_issue: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            elif table == 'cv_volume':
                print(f"  Importing {len(rows)} rows from cv_volume...", file=sys.stderr)
                # Create cv_volume table if it doesn't exist
                pg_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS cv_volume (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)

                for row in rows:
                    try:
                        # Convert row to dict using column names
                        row_dict = dict(zip(columns, row))
                        volume_id = row_dict.get('id') or row_dict.get('cv_id')
                        if volume_id:
                            pg_cursor.execute("""
                                INSERT INTO cv_volume (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (volume_id, json.dumps(row_dict)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from cv_volume: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            else:
                # Skip FTS (Full-Text Search) tables - they're SQLite-specific
                if table.endswith('_fts') or table.endswith('_fts_data') or table.endswith('_fts_docsize') or table.endswith('_fts_config') or table.endswith('_fts_idx'):
                    print(f"  Skipping FTS table: {table}", file=sys.stderr)
                    continue

                # Import all other tables generically
                print(f"  Importing {len(rows)} rows from {table} (generic import)...", file=sys.stderr)

                # Create table with same structure (id + data JSONB)
                pg_cursor.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)

                for row in rows:
                    try:
                        # Convert row to dict using column names
                        row_dict = dict(zip(columns, row))

                        # Try to find an ID column (check common ID column names)
                        row_id = (row_dict.get('id') or
                                 row_dict.get('cv_id') or
                                 row_dict.get(f"{table.replace('cv_', '')}_id") or
                                 row_dict.get('volume_id') or
                                 row_dict.get('issue_id'))

                        if row_id:
                            pg_cursor.execute(f"""
                                INSERT INTO {table} (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (int(row_id), json.dumps(row_dict)))
                            imported_count += 1
                        else:
                            # If no ID found, skip this row
                            if VERBOSE:
                                print(f"    Warning: No ID found for row in {table}, skipping. Columns: {list(row_dict.keys())[:5]}", file=sys.stderr)
                    except Exception as e:
                        print(f"Error importing row from {table}: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

        pg_conn.commit()
        sqlite_conn.close()
        pg_conn.close()

        print(f"Successfully imported {imported_count} records from SQLite database", file=sys.stderr)
        return True

    except Exception as e:
        print(f"Error importing SQLite database: {e}", file=sys.stderr)
        if VERBOSE:
            import traceback
            traceback.print_exc()
        return False
    finally:
        # Clean up temporary file and directory
        try:
            if 'temp_db_path' in locals() and os.path.exists(temp_db_path):
                os.remove(temp_db_path)
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    # Directory might not be empty, try to remove all contents
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            if VERBOSE:
                print(f"Warning: Could not clean up temp files: {e}", file=sys.stderr)


def main():
    global DB_CONFIG, DB_CONN, COMICVINE_API_KEY, VERBOSE

    parser = argparse.ArgumentParser(
        description='ComicVine API Proxy Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start proxy with PostgreSQL database
  python3 comicvine-proxy.py --db-host localhost --db-name comicvine --db-user comicvine --db-password pass --port 8080

  # Import SQLite database on startup
  python3 comicvine-proxy.py --db-host localhost --db-name comicvine --import-sqlite ~/localcv.db --port 8080

  # Start with API key for fallback
  python3 comicvine-proxy.py --db-host localhost --api-key YOUR_KEY --port 8080

  # Verbose mode
  python3 comicvine-proxy.py --db-host localhost --verbose

Environment Variables:
  COMICVINE_API_KEY    ComicVine API key (optional, for fallback)
  DB_HOST              Database host (default: localhost)
  DB_PORT              Database port (default: 5432)
  DB_NAME              Database name (default: comicvine)
  DB_USER              Database user (default: comicvine)
  DB_PASSWORD          Database password (default: comicvine)
        """
    )

    parser.add_argument(
        '--db-host',
        type=str,
        default=os.getenv('DB_HOST', 'localhost'),
        help='Database host (or set DB_HOST env var)'
    )

    parser.add_argument(
        '--db-port',
        type=str,
        default=os.getenv('DB_PORT', '5432'),
        help='Database port (or set DB_PORT env var)'
    )

    parser.add_argument(
        '--db-name',
        type=str,
        default=os.getenv('DB_NAME', 'comicvine'),
        help='Database name (or set DB_NAME env var)'
    )

    parser.add_argument(
        '--db-user',
        type=str,
        default=os.getenv('DB_USER', 'comicvine'),
        help='Database user (or set DB_USER env var)'
    )

    parser.add_argument(
        '--db-password',
        type=str,
        default=os.getenv('DB_PASSWORD', 'comicvine'),
        help='Database password (or set DB_PASSWORD env var)'
    )

    parser.add_argument(
        '--import-sqlite',
        type=str,
        default=None,
        help='Path to SQLite database file to import on startup'
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
    COMICVINE_API_KEY = args.api_key

    # Setup database configuration
    DB_CONFIG = {
        'host': args.db_host,
        'port': args.db_port,
        'database': args.db_name,
        'user': args.db_user,
        'password': args.db_password
    }

    # Import SQLite database if specified
    if args.import_sqlite:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Starting SQLite import from: {args.import_sqlite}", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)
        if not import_sqlite_to_postgres(args.import_sqlite, DB_CONFIG):
            print("\n" + "!"*60, file=sys.stderr)
            print("ERROR: SQLite import failed! Check logs above for details.", file=sys.stderr)
            print("Continuing anyway, but database will be empty...", file=sys.stderr)
            print("!"*60 + "\n", file=sys.stderr)
        else:
            print(f"\n{'='*60}", file=sys.stderr)
            print("SQLite import completed successfully!", file=sys.stderr)
            print(f"{'='*60}\n", file=sys.stderr)

    # Test database connection
    try:
        test_db = ComicVineProxyDB(DB_CONFIG)
        if not test_db.conn:
            print(f"Error: Could not connect to database", file=sys.stderr)
            sys.exit(1)
        test_db.close()
    except Exception as e:
        print(f"Error: Database connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Print startup info
    print(f"ComicVine API Proxy Server")
    print(f"==========================")
    print(f"Database: {args.db_host}:{args.db_port}/{args.db_name}")
    print(f"API Key: {'Configured' if COMICVINE_API_KEY else 'Not configured (cache-only mode)'}")
    print(f"Listening on: http://{args.host}:{args.port}")
    print(f"Proxy URL: http://{args.host}:{args.port}/api/...")
    print(f"\nConfigure Kapowarr to use: http://{args.host}:{args.port}")
    print(f"Press Ctrl+C to stop\n")

    # Start Flask server
    # Use threaded mode for better performance
    try:
        app.run(host=args.host, port=args.port, debug=VERBOSE, threaded=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
    except Exception as e:
        print(f"Error starting server: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

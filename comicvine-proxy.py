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
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime
import requests
from flask import Flask, request, jsonify, Response, render_template
from flask_cors import CORS
import psycopg2
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


def get_base_url() -> str:
    """Base URL for this server, respecting X-Forwarded-* when behind reverse proxy"""
    if request.headers.get('X-Forwarded-Proto') and request.headers.get('X-Forwarded-Host'):
        scheme = request.headers.get('X-Forwarded-Proto', 'http').rstrip('/')
        host = request.headers.get('X-Forwarded-Host', '').split(',')[0].strip()
        return f"{scheme}://{host}".rstrip('/')
    return request.url_root.rstrip('/')


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

            # Create image cache table for storing downloaded images
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS image_cache (
                    url_hash VARCHAR(64) PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    image_data BYTEA NOT NULL,
                    content_type VARCHAR(100) DEFAULT 'image/jpeg',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
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
                    # Ensure issue_data is a dict and normalize to ComicVine API format
                    if isinstance(issue_data, dict):
                        issue_data = dict(issue_data)
                        img = self._normalize_image(issue_data.get('image'))
                        if not self._has_valid_image_url(img) and issue_data.get('image_url'):
                            img = self._image_from_url(issue_data['image_url'])
                        if img is not None:
                            issue_data['image'] = img
                        # Ensure all required fields exist with defaults matching ComicVine API format
                        if 'issue_number' not in issue_data:
                            issue_data['issue_number'] = ''
                        if 'name' not in issue_data:
                            issue_data['name'] = None
                        if 'cover_date' not in issue_data:
                            issue_data['cover_date'] = None
                        if 'store_date' not in issue_data:
                            issue_data['store_date'] = None
                        if 'description' not in issue_data:
                            issue_data['description'] = None
                        if 'volume' not in issue_data:
                            issue_data['volume'] = None
                        elif isinstance(issue_data.get('volume'), dict):
                            # Ensure volume has id field
                            if 'id' not in issue_data['volume']:
                                issue_data['volume']['id'] = None
                        elif isinstance(issue_data.get('volume'), (int, str)):
                            # Convert simple ID to dict format expected by Kapowarr
                            volume_id = issue_data['volume']
                            issue_data['volume'] = {'id': int(volume_id) if volume_id else None}
                        else:
                            # If volume is not a dict, int, or str, set to None
                            issue_data['volume'] = None
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data
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
                    # Ensure results is a dict and normalize to ComicVine API format
                    if isinstance(issue_data, dict):
                        issue_data = dict(issue_data)
                        img = self._normalize_image(issue_data.get('image'))
                        if not self._has_valid_image_url(img):
                            img_url = issue_data.get('image_url') or (result.get('image_url') if isinstance(result, dict) else None)
                            if img_url:
                                img = self._image_from_url(img_url)
                        if img is not None:
                            issue_data['image'] = img
                        # Ensure all required fields exist with defaults matching ComicVine API format
                        if 'issue_number' not in issue_data:
                            issue_data['issue_number'] = ''
                        if 'name' not in issue_data:
                            issue_data['name'] = None
                        if 'cover_date' not in issue_data:
                            issue_data['cover_date'] = None
                        if 'store_date' not in issue_data:
                            issue_data['store_date'] = None
                        if 'description' not in issue_data:
                            issue_data['description'] = None
                        if 'volume' not in issue_data:
                            issue_data['volume'] = None
                        elif isinstance(issue_data.get('volume'), dict):
                            # Ensure volume has id field
                            if 'id' not in issue_data['volume']:
                                issue_data['volume']['id'] = None
                        elif isinstance(issue_data.get('volume'), (int, str)):
                            # Convert simple ID to dict format expected by Kapowarr
                            volume_id = issue_data['volume']
                            issue_data['volume'] = {'id': int(volume_id) if volume_id else None}
                        else:
                            # If volume is not a dict, int, or str, set to None
                            issue_data['volume'] = None
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data
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
                    # Ensure issue_data is a dict and normalize to ComicVine API format
                    if isinstance(issue_data, dict):
                        issue_data = dict(issue_data)
                        img = self._normalize_image(issue_data.get('image'))
                        if not self._has_valid_image_url(img) and issue_data.get('image_url'):
                            img = self._image_from_url(issue_data['image_url'])
                        if img is not None:
                            issue_data['image'] = img
                        # Ensure all required fields exist with defaults matching ComicVine API format
                        if 'issue_number' not in issue_data:
                            issue_data['issue_number'] = ''
                        if 'name' not in issue_data:
                            issue_data['name'] = None
                        if 'cover_date' not in issue_data:
                            issue_data['cover_date'] = None
                        if 'store_date' not in issue_data:
                            issue_data['store_date'] = None
                        if 'description' not in issue_data:
                            issue_data['description'] = None
                        if 'volume' not in issue_data:
                            issue_data['volume'] = None
                        elif isinstance(issue_data.get('volume'), dict):
                            # Ensure volume has id field
                            if 'id' not in issue_data['volume']:
                                issue_data['volume']['id'] = None
                        elif isinstance(issue_data.get('volume'), (int, str)):
                            # Convert simple ID to dict format expected by Kapowarr
                            volume_id = issue_data['volume']
                            issue_data['volume'] = {'id': int(volume_id) if volume_id else None}
                        else:
                            # If volume is not a dict, int, or str, set to None
                            issue_data['volume'] = None
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': issue_data
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
                    # Ensure volume_data is a dict and normalize to ComicVine format
                    if isinstance(volume_data, dict):
                        volume_data = dict(volume_data)
                        img = self._normalize_image(volume_data.get('image'))
                        if img is not None:
                            volume_data['image'] = img
                        # Ensure all required fields exist with defaults matching ComicVine API format
                        # Based on actual ComicVine API response structure
                        if 'deck' not in volume_data:
                            volume_data['deck'] = None
                        if 'description' not in volume_data:
                            volume_data['description'] = None
                        if 'image' not in volume_data:
                            volume_data['image'] = {
                                'icon_url': '',
                                'medium_url': '',
                                'screen_url': '',
                                'screen_large_url': '',
                                'small_url': '',
                                'super_url': '',
                                'thumb_url': '',
                                'tiny_url': '',
                                'original_url': '',
                                'image_tags': ''
                            }
                        elif isinstance(volume_data.get('image'), dict):
                            # Ensure all image sub-fields exist
                            image_defaults = {
                                'icon_url': '',
                                'medium_url': '',
                                'screen_url': '',
                                'screen_large_url': '',
                                'small_url': '',
                                'super_url': '',
                                'thumb_url': '',
                                'tiny_url': '',
                                'original_url': '',
                                'image_tags': ''
                            }
                            # Log original image data for debugging
                            if VERBOSE:
                                print(f"[SOURCE] Original image data for volume {volume_id}: {volume_data.get('image')}", file=sys.stderr, flush=True)
                            for key, default in image_defaults.items():
                                # Only set default if key is missing or value is None
                                # Don't overwrite empty strings - they might be valid (though unlikely)
                                # But if the value is None or missing, set the default
                                if key not in volume_data['image']:
                                    volume_data['image'][key] = default
                                elif volume_data['image'][key] is None:
                                    volume_data['image'][key] = default
                                # If it's an empty string, leave it as is (might be valid or might need to be fetched from API)
                            # Log final image data for debugging
                            if VERBOSE:
                                print(f"[SOURCE] Final image data for volume {volume_id}: {volume_data.get('image')}", file=sys.stderr, flush=True)
                                print(f"[SOURCE] small_url value: '{volume_data['image'].get('small_url')}'", file=sys.stderr, flush=True)
                        if 'count_of_issues' not in volume_data:
                            volume_data['count_of_issues'] = 0
                        if 'site_detail_url' not in volume_data:
                            volume_data['site_detail_url'] = ''
                        if 'aliases' not in volume_data:
                            volume_data['aliases'] = None
                        if 'start_year' not in volume_data:
                            volume_data['start_year'] = None
                        if 'issues' not in volume_data:
                            volume_data['issues'] = []
                        _pub = volume_data.get('publisher')
                        if not _pub or (isinstance(_pub, dict) and not _pub.get('name')):
                            pub_from_issue = self._get_publisher_for_volume_from_issues(volume_id)
                            volume_data['publisher'] = pub_from_issue if pub_from_issue else None
                        elif isinstance(volume_data.get('publisher'), dict):
                            if 'name' not in volume_data['publisher']:
                                volume_data['publisher']['name'] = ''
                            elif volume_data['publisher']['name'] is None:
                                volume_data['publisher']['name'] = ''
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table): volume/{volume_id}", file=sys.stderr)
                        print(f"Volume data keys: {list(volume_data.keys()) if isinstance(volume_data, dict) else 'not a dict'}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data
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
                    # Ensure all required fields exist with defaults
                    if 'deck' not in volume_data:
                        volume_data['deck'] = None
                    if 'description' not in volume_data:
                        volume_data['description'] = None
                    if 'image' not in volume_data:
                        volume_data['image'] = {'small_url': '', 'medium_url': '', 'super_url': ''}
                    elif isinstance(volume_data.get('image'), dict):
                        if 'small_url' not in volume_data['image']:
                            volume_data['image']['small_url'] = ''
                    if 'count_of_issues' not in volume_data:
                        volume_data['count_of_issues'] = 0
                    if 'site_detail_url' not in volume_data:
                        volume_data['site_detail_url'] = ''
                    if 'aliases' not in volume_data:
                        volume_data['aliases'] = None
                    if 'start_year' not in volume_data:
                        volume_data['start_year'] = None
                    _pub = volume_data.get('publisher')
                    if not _pub or (isinstance(_pub, dict) and not _pub.get('name')):
                        pub_from_issue = self._get_publisher_for_volume_from_issues(volume_id)
                        volume_data['publisher'] = pub_from_issue if pub_from_issue else None
                    elif isinstance(volume_data.get('publisher'), dict):
                        if 'name' not in volume_data['publisher']:
                            volume_data['publisher']['name'] = ''
                        elif volume_data['publisher']['name'] is None:
                            volume_data['publisher']['name'] = ''
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table, direct columns): volume/{volume_id}", file=sys.stderr)
                        print(f"Volume data keys: {list(volume_data.keys()) if isinstance(volume_data, dict) else 'not a dict'}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data
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
                    # Ensure volume_data is a dict and has all required fields
                    if isinstance(volume_data, dict):
                        if 'deck' not in volume_data:
                            volume_data['deck'] = None
                        if 'description' not in volume_data:
                            volume_data['description'] = None
                        if 'image' not in volume_data:
                            volume_data['image'] = {'small_url': '', 'medium_url': '', 'super_url': ''}
                        elif isinstance(volume_data.get('image'), dict):
                            if 'small_url' not in volume_data['image']:
                                volume_data['image']['small_url'] = ''
                        if 'count_of_issues' not in volume_data:
                            volume_data['count_of_issues'] = 0
                        if 'site_detail_url' not in volume_data:
                            volume_data['site_detail_url'] = ''
                        if 'aliases' not in volume_data:
                            volume_data['aliases'] = None
                        if 'start_year' not in volume_data:
                            volume_data['start_year'] = None
                        _pub = volume_data.get('publisher')
                        if not _pub or (isinstance(_pub, dict) and not _pub.get('name')):
                            pub_from_issue = self._get_publisher_for_volume_from_issues(str(volume_id_int))
                            volume_data['publisher'] = pub_from_issue if pub_from_issue else None
                        elif isinstance(volume_data.get('publisher'), dict):
                            if 'name' not in volume_data['publisher']:
                                volume_data['publisher']['name'] = ''
                            elif volume_data['publisher']['name'] is None:
                                volume_data['publisher']['name'] = ''
                    if VERBOSE:
                        print(f"Database HIT (cv_volume table, integer ID): volume/{volume_id_int}", file=sys.stderr)
                        print(f"Volume data keys: {list(volume_data.keys()) if isinstance(volume_data, dict) else 'not a dict'}", file=sys.stderr)
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': volume_data
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
        # Map resource types to table names (only tables that actually exist in the database)
        table_map = {
            'issue': 'cv_issue',
            'volume': 'cv_volume',
            'character': 'cv_character',
            'person': 'cv_person',
            'publisher': 'cv_publisher',
            'story_arc': 'cv_story_arc',
            'team': 'cv_team',
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
                        img = self._normalize_image(data.get('image'))
                        if img is not None:
                            data['image'] = img
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': data
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
                        img = self._normalize_image(data.get('image'))
                        if img is not None:
                            data['image'] = img
                    return {
                        'status_code': 1,
                        'error': 'OK',
                        'results': data
                    }
            except:
                pass

        except Exception as e:
            if VERBOSE:
                print(f"Error querying {table_name} from database: {e}", file=sys.stderr)

        return None

    def search(self, query: str, resource_types: Optional[List[str]] = None, limit: int = 50) -> Dict[str, List[Dict]]:
        """Search across resource types in name, title, description, aliases, deck. Returns dict of type -> list of matching items."""
        if not self.conn or not query or len(query.strip()) < 2:
            return {}
        types = resource_types or ['issue', 'volume', 'character', 'publisher', 'person']
        table_map = {
            'issue': 'cv_issue',
            'volume': 'cv_volume',
            'character': 'cv_character',
            'person': 'cv_person',
            'publisher': 'cv_publisher'
        }
        search_term = f"%{query.strip()}%"
        # Search in relevant text fields (name, title, description, aliases, deck)
        search_conditions = [
            "data->>'name' ILIKE %s",
            "data->>'title' ILIKE %s",
            "data->>'description' ILIKE %s",
            "data->>'aliases' ILIKE %s",
            "data->>'deck' ILIKE %s",
            "data->>'issue_number' ILIKE %s",
            "data->'volume'->>'name' ILIKE %s",
            "data->'publisher'->>'name' ILIKE %s",
        ]
        where_clause = " OR ".join(f"({c})" for c in search_conditions)
        params = [search_term] * len(search_conditions) + [limit]
        # Type-specific ordering: volumes by count_of_issues DESC (most issues first), then name
        order_by_map = {
            'volume': "ORDER BY COALESCE(NULLIF(data->>'count_of_issues','')::int, 0) DESC, data->>'name' ASC NULLS LAST, id ASC",
            'issue': "ORDER BY data->>'name' ASC NULLS LAST, COALESCE((data->>'issue_number')::text, '') ASC NULLS LAST, id ASC",
            'character': "ORDER BY COALESCE(NULLIF(data->>'count_of_issue_appearances','')::int, 0) DESC, data->>'name' ASC NULLS LAST, id ASC",
            'publisher': "ORDER BY data->>'name' ASC NULLS LAST, id ASC",
            'person': "ORDER BY COALESCE(NULLIF(data->>'count_of_issue_appearances','')::int, 0) DESC, data->>'name' ASC NULLS LAST, id ASC",
        }
        results = {}
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            for res_type in types:
                table = table_map.get(res_type)
                if not table:
                    continue
                try:
                    cursor.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_schema = 'public' AND table_name = %s
                        )
                    """, (table,))
                    if not cursor.fetchone()['exists']:
                        continue
                    order_sql = order_by_map.get(res_type, "ORDER BY data->>'name' ASC NULLS LAST, id ASC")
                    cursor.execute(f"""
                        SELECT data FROM {table}
                        WHERE {where_clause}
                        {order_sql}
                        LIMIT %s
                    """, params)
                    rows = cursor.fetchall()
                    items = [r['data'] for r in rows if isinstance(r['data'], dict)]
                    if items:
                        results[res_type] = items
                except Exception:
                    continue
        except Exception as e:
            if VERBOSE:
                print(f"Search error: {e}", file=sys.stderr)
        return results

    def get_list_from_db(self, resource_type: str, query_params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """Get list of resources from database table with filtering and sorting"""
        if not self.conn:
            return None

        # Map resource types to table names (only tables that actually exist in the database)
        table_map = {
            'issue': 'cv_issue',
            'volume': 'cv_volume',
            'character': 'cv_character',
            'person': 'cv_person',
            'publisher': 'cv_publisher'
        }

        table_name = table_map.get(resource_type)
        if not table_name:
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
                print(f"[SOURCE] Table {table_name} does not exist", file=sys.stderr, flush=True)
                return None

            # Get limit and offset from query params
            limit = min(int(query_params.get('limit', 100)) if query_params else 100, 100)  # Max 100
            offset = int(query_params.get('offset', 0)) if query_params else 0

            # Build WHERE clause from filters
            where_clauses = []
            filter_params = []

            # Volume: filter to major publishers only when requested (default for browse)
            # cv_volume typically has no publisher - get it from cv_issue (issues have volume+publisher)
            MAJOR_PUBLISHERS = [
                'marvel comics', 'marvel', 'dc comics', 'dc',
                'idw publishing', 'idw', 'skybound', 'image comics', 'image',
                'mirage studios', 'mirage', 'dark horse comics', 'dark horse'
            ]
            if resource_type == 'volume' and query_params:
                major_only = query_params.get('major_publishers_only', 'true')
                if str(major_only).lower() in ('true', '1', 'yes'):
                    placeholders = ', '.join(['%s'] * len(MAJOR_PUBLISHERS))
                    # Try volume's own publisher first; if null, use publisher from cv_issue
                    pub_name_expr = (
                        "LOWER(TRIM(COALESCE("
                        "data->'publisher'->>'name', "
                        "(SELECT p.data->>'name' FROM cv_publisher p "
                        "WHERE p.id = (NULLIF(TRIM(COALESCE(data->'publisher'->>'id','')),''))::int LIMIT 1), "
                        "(SELECT LOWER(TRIM(COALESCE("
                        "  i.data->'publisher'->>'name', "
                        "  (SELECT p2.data->>'name' FROM cv_publisher p2 "
                        "   WHERE p2.id = (NULLIF(TRIM(COALESCE(i.data->'publisher'->>'id','')),''))::int LIMIT 1), ''"
                        "))) FROM cv_issue i WHERE (i.data->'volume'->>'id')::text = cv_volume.id::text "
                        "OR i.data->>'volume' = cv_volume.id::text LIMIT 1), "
                        "''"
                        ")))"
                    )
                    where_clauses.append(f"{pub_name_expr} IN ({placeholders})")
                    filter_params.extend(MAJOR_PUBLISHERS)

            if query_params and 'filter' in query_params:
                filter_str = query_params['filter']
                # Parse filter: field:value or field:value,field:value
                filters = filter_str.split(',')
                for filter_item in filters:
                    if ':' in filter_item:
                        field, value = filter_item.split(':', 1)
                        field = field.strip()
                        value = value.strip()

                        # Build JSONB query for the field
                        # For JSONB, we use: data->>'field' = 'value'
                        # Special handling for nested objects like volume:796
                        # Volume can be stored as {"id": 796} (object) or 796 (number/string)
                        if field == 'volume':
                            # Handle both cases: volume as object with id, or direct ID
                            # Check: data->>'volume' = '796' (if stored as string/number)
                            # OR data->'volume'->>'id' = '796' (if stored as object)
                            # Note: field name must be in the query string, not parameterized
                            where_clauses.append(f"(data->>'{field}' = %s OR (data->'{field}'->>'id')::text = %s)")
                            filter_params.extend([value, value])
                        else:
                            # Field name must be in query string for JSONB operators
                            where_clauses.append(f"data->>'{field}' = %s")
                            filter_params.append(value)

            # Build ORDER BY clause from sort
            # Default: volumes by issue count (desc), others by name
            default_order = {
                'volume': "COALESCE(NULLIF(data->>'count_of_issues','')::int, 0) DESC, data->>'name' ASC NULLS LAST, id ASC",
            }
            order_by = default_order.get(resource_type, "data->>'name' ASC NULLS LAST, id ASC")
            if query_params and 'sort' in query_params:
                sort_str = query_params['sort']
                # Parse sort: field:direction
                if ':' in sort_str:
                    sort_field, sort_dir = sort_str.split(':', 1)
                    sort_field = sort_field.strip()
                    sort_dir = sort_dir.strip().upper()
                    if sort_dir not in ('ASC', 'DESC'):
                        sort_dir = 'ASC'

                    if sort_field in ('count_of_issues', 'count_of_issue'):
                        # Numeric sort for issue count
                        order_by = f"COALESCE(NULLIF(data->>'{sort_field}','')::int, 0) {sort_dir} NULLS LAST, data->>'name' ASC NULLS LAST, id ASC"
                    else:
                        order_by = f"data->>'{sort_field}' {sort_dir} NULLS LAST, id ASC"
                else:
                    sort_field = sort_str.strip()
                    if sort_field in ('count_of_issues', 'count_of_issue'):
                        order_by = f"COALESCE(NULLIF(data->>'{sort_field}','')::int, 0) DESC NULLS LAST, data->>'name' ASC NULLS LAST, id ASC"
                    else:
                        order_by = f"data->>'{sort_field}' ASC NULLS LAST, id ASC"

            # Build the query
            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            # Query with filters and sorting
            query = f"""
                SELECT data FROM {table_name}
                WHERE {where_sql}
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """

            query_params_list = filter_params + [limit, offset]

            if VERBOSE:
                print(f"Executing query: {query}", file=sys.stderr)
                print(f"  Params: {query_params_list}", file=sys.stderr)
            else:
                # Always log query for list endpoints to debug
                print(f"[SOURCE] Executing list query for {resource_type}: {query}", file=sys.stderr, flush=True)
                print(f"[SOURCE] Query params: {query_params_list}", file=sys.stderr, flush=True)

            try:
                cursor.execute(query, query_params_list)
                results = cursor.fetchall()
            except Exception as query_error:
                print(f"[SOURCE] SQL query error: {query_error}", file=sys.stderr, flush=True)
                if VERBOSE:
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                return None

            if not results:
                print(f"[SOURCE] No results found for {resource_type} with filters: {query_params}", file=sys.stderr, flush=True)
                return None

            print(f"[SOURCE] Found {len(results)} results for {resource_type}", file=sys.stderr, flush=True)

            # Convert to list of dicts, normalizing image field (may be JSON string in some DBs)
            items = []
            for row in results:
                data = row.get('data') if hasattr(row, 'get') else (row[0] if row else None)
                if isinstance(data, dict):
                    data = dict(data)
                    img = self._normalize_image(data.get('image'))
                    if img is not None:
                        data['image'] = img
                    items.append(data)
                else:
                    items.append(data)

            # Enrich volumes with publisher from issues when cv_volume has no publisher
            if resource_type == 'volume':
                for item in items:
                    if isinstance(item, dict):
                        _pub = item.get('publisher')
                        if not _pub or (isinstance(_pub, dict) and not _pub.get('name')):
                            vid = str(item.get('id') or item.get('cv_id') or '').split('-')[-1]
                            if vid:
                                pub_from_issue = self._get_publisher_for_volume_from_issues(vid)
                                if pub_from_issue:
                                    item['publisher'] = pub_from_issue

            # Get total count (with same filters)
            count_query = f"""
                SELECT COUNT(*) as count FROM {table_name}
                WHERE {where_sql}
            """
            cursor.execute(count_query, filter_params)
            count_result = cursor.fetchone()
            total_count = count_result['count'] if count_result else len(items)

            # Build ComicVine API response format
            return {
                'status_code': 1,
                'error': 'OK',
                'limit': limit,
                'offset': offset,
                'number_of_page_results': len(items),
                'number_of_total_results': total_count,
                'results': items
            }

        except Exception as e:
            print(f"Error querying list from {table_name}: {e}", file=sys.stderr, flush=True)
            if VERBOSE:
                import traceback
                traceback.print_exc(file=sys.stderr)
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
                    # Remove _source if it exists (from old cached data)
                    cached_data.pop('_source', None)
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

    def _url_to_hash(self, url: str) -> str:
        """Convert URL to consistent hash for cache key"""
        return hashlib.sha256(url.encode()).hexdigest()

    def _image_from_url(self, url: str) -> Optional[dict]:
        """Build ComicVine-style image dict from a single URL string."""
        norm = self._normalize_image_url(url) if url else None
        if norm:
            return {'medium_url': norm, 'small_url': norm, 'original_url': norm}
        return None

    def _normalize_image(self, img: Any) -> Any:
        """Normalize image: if stored as JSON string, parse to dict. Normalize protocol-relative URLs."""
        if img is None:
            return None
        if isinstance(img, dict):
            out = dict(img)
            for key in ('icon_url', 'medium_url', 'screen_url', 'screen_large_url',
                        'small_url', 'super_url', 'thumb_url', 'tiny_url', 'original_url'):
                u = out.get(key)
                if u and isinstance(u, str):
                    norm = self._normalize_image_url(u)
                    if norm:
                        out[key] = norm
            return out
        if isinstance(img, str):
            s = img.strip()
            if s.startswith('{') and ('url' in s or 'medium' in s or 'small' in s):
                try:
                    return self._normalize_image(json.loads(s))
                except (json.JSONDecodeError, TypeError):
                    pass
            norm = self._normalize_image_url(s)
            if norm:
                return {'medium_url': norm, 'small_url': norm, 'original_url': norm}
        return img

    def _normalize_image_url(self, url: str) -> Optional[str]:
        """Normalize image URL: protocol-relative (//) -> https:, validate http(s)."""
        if not url or not isinstance(url, str):
            return None
        url = url.strip()
        if url.startswith('//'):
            url = 'https:' + url
        if url.startswith('http'):
            return url
        return None

    def _extract_image_urls(self, data: Any) -> List[str]:
        """Recursively extract all image URLs from ComicVine data structure"""
        urls = []
        if isinstance(data, dict):
            if 'image' in data and isinstance(data['image'], dict):
                for key in ('icon_url', 'medium_url', 'screen_url', 'screen_large_url',
                           'small_url', 'super_url', 'thumb_url', 'tiny_url', 'original_url'):
                    url = data['image'].get(key)
                    norm = self._normalize_image_url(url) if url else None
                    if norm:
                        urls.append(norm)
            if 'image_url' in data and isinstance(data['image_url'], str):
                norm = self._normalize_image_url(data['image_url'])
                if norm:
                    urls.append(norm)
            for value in data.values():
                urls.extend(self._extract_image_urls(value))
        elif isinstance(data, list):
            for item in data:
                urls.extend(self._extract_image_urls(item))
        return list(set(urls))

    def _has_valid_image_url(self, img: Any) -> bool:
        """Check if image object has at least one valid URL"""
        if not img or not isinstance(img, dict):
            return False
        for key in ('medium_url', 'small_url', 'thumb_url', 'original_url', 'super_url', 'icon_url', 'tiny_url'):
            if self._normalize_image_url(img.get(key)):
                return True
        return False

    def _slugify(self, name: str) -> str:
        """Convert name to ComicVine-style URL slug."""
        if not name or not isinstance(name, str):
            return ''
        s = name.lower().strip()
        s = re.sub(r'[^a-z0-9\s-]', '', s)
        s = re.sub(r'[\s_]+', '-', s)
        s = re.sub(r'-+', '-', s).strip('-')
        return s or 'unnamed'

    def _get_publisher_for_volume_from_issues(self, volume_id: str) -> Optional[dict]:
        """Get publisher from first issue of this volume (when cv_volume has no publisher)."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT EXISTS (SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'cv_issue') as exists
            """)
            if not (cursor.fetchone() or {}).get('exists'):
                return None
            cursor.execute("""
                SELECT data FROM cv_issue
                WHERE (data->'volume'->>'id')::text = %s OR data->>'volume' = %s
                ORDER BY COALESCE(NULLIF(SUBSTRING(data->>'issue_number' FROM '[0-9]+'),'')::int, 999999) ASC
                LIMIT 1
            """, (str(volume_id), str(volume_id)))
            row = cursor.fetchone()
            if row and row.get('data'):
                issue = row['data']
                pub = issue.get('publisher') if isinstance(issue, dict) else None
                if pub and isinstance(pub, dict) and pub.get('name'):
                    return pub
                # Try cv_publisher lookup if issue has publisher id only
                pub_id = (pub.get('id') if isinstance(pub, dict) else None) or (pub if isinstance(pub, (int, str)) else None)
                if pub_id:
                    cursor.execute("SELECT data FROM cv_publisher WHERE id = %s LIMIT 1", (int(pub_id) if pub_id else 0,))
                    p_row = cursor.fetchone()
                    if p_row and p_row.get('data'):
                        return p_row['data']
        except Exception as e:
            if VERBOSE:
                print(f"[DB] Error getting publisher for volume {volume_id}: {e}", file=sys.stderr)
        return None

    def _get_issue_1_for_volume(self, volume_id: str) -> Optional[dict]:
        """Get issue #1 of a volume from DB (for using its cover as volume image)."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT EXISTS (SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'cv_issue') as exists
            """)
            if not (cursor.fetchone() or {}).get('exists'):
                return None
            select_cols = "data, image_url" if getattr(self, 'issue_columns', None) and 'image_url' in self.issue_columns else "data"
            cursor.execute(f"""
                SELECT {select_cols} FROM cv_issue
                WHERE (data->'volume'->>'id')::text = %s
                   OR data->>'volume' = %s
                ORDER BY COALESCE(
                    NULLIF(substring(data->>'issue_number' from '[0-9]+'), '')::int,
                    999999
                ) ASC NULLS LAST, id ASC
                LIMIT 1
            """, (str(volume_id), str(volume_id)))
            row = cursor.fetchone()
            if row and row.get('data'):
                issue_1 = row['data'] if isinstance(row['data'], dict) else None
                img_url = (issue_1.get('image_url') if issue_1 else None) or row.get('image_url')
                if issue_1 and not self._has_valid_image_url(issue_1.get('image')) and img_url:
                    img = self._image_from_url(img_url)
                    if img:
                        issue_1 = dict(issue_1)
                        issue_1['image'] = img
                return issue_1
        except Exception as e:
            if VERBOSE:
                print(f"[IMAGE] Error getting issue #1 for volume {volume_id}: {e}", file=sys.stderr)
        return None

    def _fetch_image_from_comicvine_page(self, resource_type: str, resource_id: str, item: dict) -> Optional[dict]:
        """Fetch image URL from ComicVine public HTML page (no API key needed)."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        if resource_type == 'volume':
            issue_1 = self._get_issue_1_for_volume(resource_id)
            if issue_1:
                img = self._normalize_image(issue_1.get('image'))
                if self._has_valid_image_url(img):
                    print(f"[IMAGE] Volume {resource_id}: using issue #1 cover from DB", file=sys.stderr, flush=True)
                    return img
                issue_id = str(issue_1.get('id') or issue_1.get('cv_id') or '').split('-')[-1]
                if issue_id and issue_id != 'None':
                    issue_img = self._fetch_image_from_comicvine_page('issue', issue_id, issue_1)
                    if issue_img:
                        print(f"[IMAGE] Volume {resource_id}: using issue #1 cover from scrape", file=sys.stderr, flush=True)
                        return issue_img
            else:
                print(f"[IMAGE] Volume {resource_id}: no issue #1 in DB, trying volume page as fallback", file=sys.stderr, flush=True)

        type_prefixes = {'issue': '4000', 'volume': '4050', 'character': '4005',
                         'person': '4040', 'publisher': '4010'}
        prefix = type_prefixes.get(resource_type)
        if not prefix:
            print(f"[IMAGE] Scrape: no prefix for {resource_type}", file=sys.stderr, flush=True)
            return None
        slug = ''
        if isinstance(item.get('site_detail_url'), str) and item['site_detail_url']:
            m = re.search(r'comicvine\.gamespot\.com/([^/]+)/' + re.escape(prefix) + r'-' + re.escape(str(resource_id)), item['site_detail_url'])
            if m:
                slug = m.group(1)
        if not slug:
            name = item.get('name') or item.get('title') or ''
            slug = self._slugify(name) or 'unnamed'
        url = f"{COMICVINE_BASE_URL}/{slug}/{prefix}-{resource_id}/"
        print(f"[IMAGE] Scraping {resource_type}/{resource_id} from {url}", file=sys.stderr, flush=True)
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            if not m:
                m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
            if m:
                img_url = m.group(1).strip()
                if self._normalize_image_url(img_url):
                    print(f"[IMAGE] Scrape OK {resource_type}/{resource_id}: got image URL", file=sys.stderr, flush=True)
                    return {'medium_url': img_url, 'small_url': img_url, 'original_url': img_url}
            print(f"[IMAGE] Scrape: no og:image in HTML for {resource_type}/{resource_id}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[IMAGE] Scrape failed for {resource_type}/{resource_id}: {e}", file=sys.stderr, flush=True)
        return None

    def ensure_resource_has_images(self, resource_type: str, resource_id: str, data: Any, base_url: str) -> Any:
        """
        If resource has empty image URLs, fetch from ComicVine (API or public page), download images, store in DB.
        Returns data with image URLs (local /images/hash when cached, or ComicVine URL).
        """
        if not data or resource_type not in ('issue', 'volume', 'character', 'publisher', 'person'):
            return self._replace_image_urls_with_local(data, base_url)
        item = data.get('results') if isinstance(data, dict) else data
        if not isinstance(item, dict):
            return self._replace_image_urls_with_local(data, base_url)
        img = self._normalize_image(item.get('image'))
        if not self._has_valid_image_url(img) and item.get('image_url'):
            img = self._image_from_url(item['image_url'])
        if img is not None:
            item['image'] = img
        if self._has_valid_image_url(img):
            return self._replace_image_urls_with_local(data, base_url)
        print(f"[IMAGE] Missing image for {resource_type}/{resource_id}, attempting fetch...", file=sys.stderr, flush=True)
        api_img = None
        if resource_type == 'volume':
            api_img = self._fetch_image_from_comicvine_page('volume', resource_id, item)
        else:
            if COMICVINE_API_KEY:
                api_response = fetch_from_comicvine(resource_type, resource_id, {'field_list': 'id,name,image'})
                if api_response and api_response.get('results'):
                    api_img = api_response['results'].get('image')
            if not self._has_valid_image_url(api_img):
                api_img = self._fetch_image_from_comicvine_page(resource_type, resource_id, item)
        if not self._has_valid_image_url(api_img):
            print(f"[IMAGE] No image found for {resource_type}/{resource_id}", file=sys.stderr, flush=True)
            return self._replace_image_urls_with_local(data, base_url)
        print(f"[IMAGE] Downloading and storing image for {resource_type}/{resource_id}", file=sys.stderr, flush=True)
        self._merge_image_and_store(resource_type, resource_id, item, api_img)
        item['image'] = api_img
        return self._replace_image_urls_with_local(data, base_url)

    def _merge_image_and_store(self, resource_type: str, resource_id: str, existing_data: dict, image_data: dict):
        """Merge image into existing record, download images, update DB."""
        table_map = {'issue': 'cv_issue', 'volume': 'cv_volume', 'character': 'cv_character',
                     'person': 'cv_person', 'publisher': 'cv_publisher'}
        table = table_map.get(resource_type)
        if not table or not self.conn:
            return
        try:
            merged = dict(existing_data)
            merged['image'] = image_data
            self._download_and_store_images(merged)
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(f"SELECT data FROM {table} WHERE id = %s", (int(resource_id),))
            row = cursor.fetchone()
            if row:
                raw = row.get('data')
                if isinstance(raw, str):
                    try:
                        current = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        current = dict(existing_data)
                elif isinstance(raw, dict):
                    current = dict(raw)
                else:
                    current = dict(existing_data)
                current['image'] = image_data
                cursor.execute(f"UPDATE {table} SET data = %s WHERE id = %s",
                              (json.dumps(current), int(resource_id)))
                self.conn.commit()
                if VERBOSE:
                    print(f"Updated {resource_type}/{resource_id} with image data", file=sys.stderr)
        except Exception as e:
            if VERBOSE:
                print(f"Error merging image: {e}", file=sys.stderr)
            if self.conn:
                self.conn.rollback()

    def _download_and_store_images(self, data: Dict[str, Any]):
        """Download images from URLs in data and store in image_cache"""
        if not self.conn:
            return
        urls = self._extract_image_urls(data)
        if urls:
            print(f"[IMAGE] Downloading {len(urls)} image(s) to cache", file=sys.stderr, flush=True)
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; ComicVine-Proxy/1.0)',
            'Referer': 'https://comicvine.gamespot.com/',
        }
        for url in urls:
            try:
                url_hash = self._url_to_hash(url)
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT url_hash FROM image_cache WHERE url_hash = %s",
                    (url_hash,)
                )
                if cursor.fetchone():
                    continue  # Already cached
                resp = requests.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                if ';' in content_type:
                    content_type = content_type.split(';')[0].strip()
                cursor.execute("""
                    INSERT INTO image_cache (url_hash, source_url, image_data, content_type)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (url_hash) DO NOTHING
                """, (url_hash, url, psycopg2.Binary(resp.content), content_type))
                self.conn.commit()
                print(f"[IMAGE] Stored image {url_hash[:12]}... ({len(resp.content)} bytes)", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[IMAGE] Failed to download {url[:60]}...: {e}", file=sys.stderr, flush=True)
                if self.conn:
                    self.conn.rollback()

    def has_image(self, url_hash: str) -> bool:
        """Check if image exists in cache without fetching full data."""
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM image_cache WHERE url_hash = %s LIMIT 1",
                (url_hash,)
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    def get_image(self, url_hash: str) -> Optional[Tuple[bytes, str]]:
        """Get image data from cache. Returns (image_bytes, content_type) or None."""
        if not self.conn:
            return None
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "SELECT image_data, content_type FROM image_cache WHERE url_hash = %s",
                (url_hash,)
            )
            row = cursor.fetchone()
            if row:
                return (bytes(row['image_data']), row['content_type'] or 'image/jpeg')
        except Exception as e:
            if VERBOSE:
                print(f"Error getting image: {e}", file=sys.stderr)
        return None

    def _replace_image_urls_with_local(self, data: Any, base_url: str) -> Any:
        """Replace ComicVine image URLs with local proxy URLs where we have them cached"""
        if isinstance(data, dict):
            result = dict(data)
            if 'image' in result and isinstance(result['image'], dict):
                new_image = dict(result['image'])
                for key in ('icon_url', 'medium_url', 'screen_url', 'screen_large_url',
                           'small_url', 'super_url', 'thumb_url', 'tiny_url', 'original_url'):
                    url = new_image.get(key)
                    norm = self._normalize_image_url(url) if url else None
                    if norm:
                        url_hash = self._url_to_hash(norm)
                        if self.has_image(url_hash):
                            new_image[key] = base_url.rstrip('/') + '/images/' + url_hash
                result['image'] = new_image
            for k, v in result.items():
                if k != 'image':
                    result[k] = self._replace_image_urls_with_local(v, base_url)
            return result
        elif isinstance(data, list):
            return [self._replace_image_urls_with_local(item, base_url) for item in data]
        return data

    def cache_response(self, resource_type: str, resource_id: str, response_data: Dict[str, Any]):
        """Store API response in the correct table based on resource type"""
        if not self.conn:
            # Try to reconnect
            self.conn = self._get_connection()
            if not self.conn:
                return

        try:
            # Map resource types to table names (only tables that actually exist in the database)
            table_map = {
                'issue': 'cv_issue',
                'volume': 'cv_volume',
                'character': 'cv_character',
                'person': 'cv_person',
                'publisher': 'cv_publisher'
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

            # Download and store images from the cached data
            if isinstance(actual_data, dict):
                self._download_and_store_images(actual_data)

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
    print(f"[SOURCE] ===== REQUEST RECEIVED: {full_path} =====", file=sys.stderr, flush=True)
    print(f"[SOURCE] Request args: {dict(request.args)}", file=sys.stderr, flush=True)

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
    if not DB_CONFIG:
        print(f"[SOURCE] WARNING: DB_CONFIG is None - database not configured!", file=sys.stderr, flush=True)

    proxy_db = ComicVineProxyDB(DB_CONFIG) if DB_CONFIG else None

    if proxy_db:
        if proxy_db.conn:
            print(f"[SOURCE] Database connection: OK", file=sys.stderr, flush=True)
        else:
            print(f"[SOURCE] WARNING: Database connection failed - proxy_db.conn is None", file=sys.stderr, flush=True)
    else:
        print(f"[SOURCE] WARNING: proxy_db is None - cannot check database", file=sys.stderr, flush=True)

    # For detail endpoints, try to get from database tables first
    if not is_list and resource_id and proxy_db and proxy_db.conn:
        print(f"[SOURCE] Checking database for detail endpoint: {resource_type}/{resource_id}", file=sys.stderr, flush=True)
        db_result = proxy_db.get_resource_from_db(resource_type, resource_id)
        if db_result:
            print(f"[SOURCE] Database HIT (direct table): {resource_type}/{resource_id}", file=sys.stderr, flush=True)
            db_result.pop('_source', None)
            base_url = get_base_url()
            db_result = proxy_db.ensure_resource_has_images(resource_type, resource_id, db_result, base_url)

            if False and resource_type == 'volume' and 'results' in db_result:
                print(f"[SOURCE] Volume image fallback check STARTED for {resource_id}", file=sys.stderr, flush=True)
                volume_results = db_result['results']
                image_data = volume_results.get('image', {})
                if not isinstance(image_data, dict):
                    image_data = {}
                small_url = image_data.get('small_url', '') if image_data else ''
                print(f"[SOURCE] Volume {resource_id} - Full results keys: {list(volume_results.keys())}", file=sys.stderr, flush=True)
                print(f"[SOURCE] Volume {resource_id} image data type: {type(image_data)}, value: {image_data}", file=sys.stderr, flush=True)
                print(f"[SOURCE] Volume {resource_id} image.small_url from DB: '{small_url}' (type: {type(small_url)})", file=sys.stderr, flush=True)

                # If image URLs are empty or missing, try to fetch from ComicVine API to get the URLs
                # Check if small_url is empty, None, or missing - be very permissive
                small_url_value = image_data.get('small_url', '') if isinstance(image_data, dict) else ''

                # Explicit check: if small_url is empty string, None, or missing, trigger fallback
                # Empty string is falsy, so `not small_url_value` will catch it
                needs_fallback = (
                    not image_data or
                    not isinstance(image_data, dict) or
                    not small_url_value or
                    (isinstance(small_url_value, str) and len(small_url_value.strip()) == 0)
                )

                if needs_fallback:
                    print(f"[SOURCE] Volume {resource_id} - FALLBACK TRIGGERED - image_data: {bool(image_data)}, is_dict: {isinstance(image_data, dict)}, small_url: '{small_url_value}'", file=sys.stderr, flush=True)
                    print(f"[SOURCE] Volume {resource_id} has empty/missing image URLs, fetching from ComicVine API to get image data", file=sys.stderr, flush=True)
                    # Ensure we request the image field when fetching from API
                    fallback_params = dict(query_params) if query_params else {}
                    # Always include image in field_list for fallback
                    if 'field_list' in fallback_params:
                        # Add image to existing field_list if not already present
                        field_list = fallback_params['field_list'].split(',')
                        if 'image' not in field_list:
                            field_list.append('image')
                        fallback_params['field_list'] = ','.join(field_list)
                    else:
                        # Request image field along with other common fields
                        fallback_params['field_list'] = 'id,name,image,description,deck,start_year,count_of_issues,site_detail_url,aliases,publisher,issues'

                    print(f"[SOURCE] Fetching from ComicVine API with params: {fallback_params}", file=sys.stderr, flush=True)
                    api_response = fetch_from_comicvine(resource_type, resource_id, fallback_params)

                    if api_response and 'results' in api_response:
                        api_image = api_response['results'].get('image', {})
                        print(f"[SOURCE] API response image data: {api_image}", file=sys.stderr, flush=True)

                        if isinstance(api_image, dict) and api_image.get('small_url'):
                            # Update the database result with image URLs from API
                            db_result['results']['image'] = api_image
                            print(f"[SOURCE] Updated volume {resource_id} with image URLs from API: '{api_image.get('small_url')}'", file=sys.stderr, flush=True)
                            print(f"[SOURCE] Final db_result['results']['image'] after update: {db_result['results'].get('image')}", file=sys.stderr, flush=True)

                            # Update the database cache with the complete data
                            try:
                                proxy_db.cache_response(resource_type, resource_id, api_response)
                                print(f"[SOURCE] Updated database cache for volume {resource_id} with image data", file=sys.stderr, flush=True)
                            except Exception as cache_error:
                                print(f"[SOURCE] Warning: Failed to update cache: {cache_error}", file=sys.stderr, flush=True)
                                import traceback
                                traceback.print_exc(file=sys.stderr)
                        else:
                            print(f"[SOURCE] Warning: API response for volume {resource_id} also has empty image URLs. Image data: {api_image}", file=sys.stderr, flush=True)
                    else:
                        print(f"[SOURCE] Warning: Failed to fetch image data from ComicVine API for volume {resource_id}. Response: {api_response}", file=sys.stderr, flush=True)

            # Before returning, verify image data is present (for volumes)
            if resource_type == 'volume' and 'results' in db_result:
                final_image = db_result['results'].get('image', {})
                final_small_url = final_image.get('small_url', '') if isinstance(final_image, dict) else ''
                print(f"[SOURCE] Final response check - Volume {resource_id} image.small_url: '{final_small_url}'", file=sys.stderr, flush=True)

            response = jsonify(db_result)
            response.headers['X-Data-Source'] = 'local_database_table'
            return response
        else:
            print(f"[SOURCE] Database MISS: {resource_type}/{resource_id} not found in database", file=sys.stderr, flush=True)

    # For list endpoints, try to query database first (with SQL filtering)
    if is_list and proxy_db and proxy_db.conn:
        print(f"[SOURCE] List endpoint detected: {resource_type}", file=sys.stderr, flush=True)
        print(f"[SOURCE] Query params: {query_params}", file=sys.stderr, flush=True)

        # Try to get from database - SQL can handle filters and sorting
        db_list_result = proxy_db.get_list_from_db(resource_type, query_params)
        if db_list_result:
            print(f"[SOURCE] Database HIT (list from table with SQL filtering): {resource_type}", file=sys.stderr, flush=True)
            base_url = get_base_url()
            items = db_list_result.get('results') or []
            for i, item in enumerate(items[:24]):
                if isinstance(item, dict) and item.get('id'):
                    rid = str(item['id'])
                    db_list_result['results'][i] = proxy_db.ensure_resource_has_images(
                        resource_type, rid, {'results': item}, base_url
                    ).get('results', item)
            db_list_result = proxy_db._replace_image_urls_with_local(db_list_result, base_url)
            response = jsonify(db_list_result)
            response.headers['X-Data-Source'] = 'local_database_table'
            return response
        else:
            print(f"[SOURCE] Database MISS (list): {resource_type} - no data found, trying API", file=sys.stderr, flush=True)

        # Fall through to API fetch if database doesn't have data
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

        # Make a copy to avoid modifying the original
        if isinstance(api_response, dict):
            # Deep copy to ensure we have a mutable dict
            import copy
            api_response = copy.deepcopy(api_response)
            # Remove _source if it exists (from old cached data)
            api_response.pop('_source', None)

        # Cache the response if we have a database connection
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


@app.route('/images/<url_hash>', methods=['GET'])
def serve_image(url_hash: str):
    """Serve cached image from database"""
    if not DB_CONFIG:
        return jsonify({'error': 'Database not configured'}), 503
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    result = proxy_db.get_image(url_hash)
    if result:
        image_data, content_type = result
        return Response(image_data, mimetype=content_type)
    return jsonify({'error': 'Image not found'}), 404


@app.route('/proxy-image', methods=['GET'])
def proxy_image():
    """Proxy external images (e.g. ComicVine) to avoid CORS and hotlinking issues"""
    url = request.args.get('url')
    if not url or not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; ComicVine-Proxy/1.0)',
            'Accept': 'image/*',
            'Referer': 'https://comicvine.gamespot.com/',
        }, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        if ';' in content_type:
            content_type = content_type.split(';')[0].strip()
        return Response(resp.content, mimetype=content_type)
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 502


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
            '/health': 'Health check',
            '/web': 'Web UI for browsing database'
        },
        'usage': 'Configure your application to use this proxy URL instead of comicvine.gamespot.com'
    })


# ============== Web UI Routes ==============

@app.route('/web')
@app.route('/web/')
def web_ui():
    """Serve the Web UI for browsing database content"""
    return render_template('webui.html')


@app.route('/web/api/browse/<resource_type>')
def web_api_browse(resource_type: str):
    """Browse resources by type (publishers, volumes, characters, issues, people)"""
    valid_types = {'publisher', 'volume', 'character', 'issue', 'person',
                   'publishers', 'volumes', 'characters', 'issues', 'people'}
    if not DB_CONFIG or resource_type not in valid_types:
        return jsonify({'error': 'Invalid resource type'}), 400
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    if not proxy_db.conn:
        return jsonify({'error': 'Database not available'}), 503
    singular = {'publishers': 'publisher', 'volumes': 'volume', 'characters': 'character',
                'issues': 'issue', 'people': 'person'}.get(resource_type, resource_type.rstrip('s'))
    default_sort = 'count_of_issues:desc' if singular == 'volume' else 'name:asc'
    # Explicitly pass major_publishers_only for volumes (default true) so filter is always applied
    params = {
        'limit': request.args.get('limit', '24'),
        'offset': request.args.get('offset', '0'),
        'sort': request.args.get('sort', default_sort),
        **{k: v for k, v in request.args.items() if k in ('filter', 'sort', 'major_publishers_only')}
    }
    if singular == 'volume' and 'major_publishers_only' not in params:
        params['major_publishers_only'] = 'true'
    result = proxy_db.get_list_from_db(singular, params)
    if not result:
        return jsonify({'results': [], 'number_of_total_results': 0})
    base_url = get_base_url()
    items = result.get('results') or []
    print(f"[IMAGE] Browse {resource_type}: {len(items)} items to process", file=sys.stderr, flush=True)
    for i, item in enumerate(items):
        rid = (item.get('id') or item.get('cv_id')) if isinstance(item, dict) else None
        if isinstance(item, dict) and rid is not None:
            rid = str(rid).split('-')[-1]
            result['results'][i] = proxy_db.ensure_resource_has_images(
                singular, rid, {'results': item}, base_url
            ).get('results', item)
    result = proxy_db._replace_image_urls_with_local(result, base_url)
    return jsonify(result)


@app.route('/web/api/search')
def web_api_search():
    """Search across all resource types"""
    if not DB_CONFIG:
        return jsonify({'error': 'Database not configured'}), 503
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'results': {}})
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    if not proxy_db.conn:
        return jsonify({'error': 'Database not available'}), 503
    types = request.args.get('types', 'issue,volume,character,publisher,person').split(',')
    results = proxy_db.search(q, [t.strip() for t in types if t.strip()], limit=30)
    base_url = get_base_url()
    for res_type in results:
        out = []
        for item in results[res_type]:
            rid = (item.get('id') or item.get('cv_id')) if isinstance(item, dict) else None
            if isinstance(item, dict) and rid is not None:
                rid = str(rid).split('-')[-1]
                ensured = proxy_db.ensure_resource_has_images(
                    res_type, rid, {'results': item}, base_url
                )
                item = ensured.get('results', item)
            out.append(item)
        results[res_type] = out
    return jsonify({'results': results})


@app.route('/web/api/debug/volume/<int:vol_id>')
def web_api_debug_volume(vol_id: int):
    """Debug: return a volume's publisher data (from volume + from first issue)"""
    if not DB_CONFIG:
        return jsonify({'error': 'No DB'}), 503
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    if not proxy_db.conn:
        return jsonify({'error': 'No connection'}), 503
    try:
        cursor = proxy_db.conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id, data FROM cv_volume WHERE id = %s LIMIT 1", (vol_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'error': f'Volume {vol_id} not found'})
        d = row['data']
        pub = d.get('publisher') if isinstance(d, dict) else None
        pub_name = (pub.get('name') if isinstance(pub, dict) else None) or (pub if isinstance(pub, str) else None)
        # Get publisher from first issue of this volume
        cursor.execute("""
            SELECT data FROM cv_issue
            WHERE (data->'volume'->>'id')::text = %s OR data->>'volume' = %s
            ORDER BY COALESCE(NULLIF(SUBSTRING(data->>'issue_number' FROM '[0-9]+'),'')::int, 999999) ASC
            LIMIT 1
        """, (str(vol_id), str(vol_id)))
        issue_row = cursor.fetchone()
        issue_pub = None
        issue_pub_name = None
        if issue_row and issue_row.get('data'):
            i = issue_row['data']
            issue_pub = i.get('publisher') if isinstance(i, dict) else None
            issue_pub_name = (issue_pub.get('name') if isinstance(issue_pub, dict) else None) or (issue_pub if isinstance(issue_pub, str) else None)
        return jsonify({
            'id': vol_id,
            'name': d.get('name') if isinstance(d, dict) else None,
            'volume_publisher_raw': pub,
            'volume_publisher_name': pub_name,
            'from_issue_publisher_raw': issue_pub,
            'from_issue_publisher_name': issue_pub_name,
            'effective_for_filter': (pub_name or issue_pub_name or '').lower().strip(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/web/api/debug/sample')
def web_api_debug_sample():
    """Debug: return first volume with full structure to diagnose image flow"""
    if not DB_CONFIG:
        return jsonify({'error': 'No DB'}), 503
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    if not proxy_db.conn:
        return jsonify({'error': 'No connection'}), 503
    try:
        cursor = proxy_db.conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id, data FROM cv_volume LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return jsonify({'error': 'No volumes'})
        item = row['data']
        rid = str(row['id'])
        base_url = get_base_url()
        ensured = proxy_db.ensure_resource_has_images('volume', rid, {'results': item}, base_url)
        after = ensured.get('results', {}) if isinstance(ensured.get('results'), dict) else {}
        return jsonify({
            'raw_image': item.get('image') if isinstance(item, dict) else None,
            'raw_image_type': type(item.get('image')).__name__ if isinstance(item, dict) else None,
            'after_ensure': after.get('image'),
            'base_url': base_url,
            'comicvine_api_key_set': bool(COMICVINE_API_KEY),
            'item_keys': list(item.keys()) if isinstance(item, dict) else None,
            'item_name': item.get('name') if isinstance(item, dict) else None,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/web/api/<resource_type>/<resource_id>')
def web_api_detail(resource_type: str, resource_id: str):
    """Get detail for a single resource"""
    valid_types = {'publisher', 'volume', 'character', 'issue', 'person', 'story_arc', 'team'}
    if not DB_CONFIG or resource_type not in valid_types:
        return jsonify({'error': 'Invalid resource type'}), 400
    proxy_db = ComicVineProxyDB(DB_CONFIG)
    if not proxy_db.conn:
        return jsonify({'error': 'Database not available'}), 503
    result = proxy_db.get_resource_from_db(resource_type, resource_id)
    if not result:
        return jsonify({'error': 'Not found'}), 404
    base_url = get_base_url()
    result = proxy_db.ensure_resource_has_images(resource_type, resource_id, result, base_url)
    return jsonify(result)


def check_if_import_needed(db_config: Dict[str, str]) -> bool:
    """Check if database tables have data - if yes, skip import"""
    try:
        pg_conn = psycopg2.connect(
            host=db_config.get('host', 'localhost'),
            port=db_config.get('port', '5432'),
            database=db_config.get('database', 'comicvine'),
            user=db_config.get('user', 'comicvine'),
            password=db_config.get('password', 'comicvine')
        )
        pg_cursor = pg_conn.cursor()

        # Check main tables that should have data
        tables_to_check = ['cv_issue', 'cv_volume', 'cv_character', 'cv_person', 'cv_publisher']

        for table in tables_to_check:
            pg_cursor.execute(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                )
            """, (table,))
            table_exists = pg_cursor.fetchone()[0]

            if table_exists:
                pg_cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = pg_cursor.fetchone()[0]
                if count > 0:
                    print(f"Table {table} has {count} records - import not needed", file=sys.stderr)
                    pg_conn.close()
                    return False

        pg_conn.close()
        print("No data found in main tables - import needed", file=sys.stderr)
        return True
    except Exception as e:
        print(f"Error checking if import needed: {e}", file=sys.stderr)
        # If we can't check, assume we need to import
        return True


def import_sqlite_to_postgres(sqlite_path: str, db_config: Dict[str, str]):
    """Import data from SQLite database to PostgreSQL"""
    import shutil
    import tempfile

    # Check if import is needed
    if not check_if_import_needed(db_config):
        print("Database already has data - skipping import", file=sys.stderr)
        return True

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

            elif table == 'cv_person':
                print(f"  Importing {len(rows)} rows from cv_person...", file=sys.stderr)
                # Create cv_person table if it doesn't exist
                pg_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS cv_person (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)

                for row in rows:
                    try:
                        # Convert row to dict using column names
                        row_dict = dict(zip(columns, row))
                        person_id = row_dict.get('id') or row_dict.get('cv_id')
                        if person_id:
                            pg_cursor.execute("""
                                INSERT INTO cv_person (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (person_id, json.dumps(row_dict)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from cv_person: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            elif table == 'cv_publisher':
                print(f"  Importing {len(rows)} rows from cv_publisher...", file=sys.stderr)
                # Create cv_publisher table if it doesn't exist
                pg_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS cv_publisher (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)

                for row in rows:
                    try:
                        # Convert row to dict using column names
                        row_dict = dict(zip(columns, row))
                        publisher_id = row_dict.get('id') or row_dict.get('cv_id')
                        if publisher_id:
                            pg_cursor.execute("""
                                INSERT INTO cv_publisher (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (publisher_id, json.dumps(row_dict)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from cv_publisher: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            elif table == 'cv_character':
                print(f"  Importing {len(rows)} rows from cv_character...", file=sys.stderr)
                pg_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS cv_character (
                        id INTEGER PRIMARY KEY,
                        data JSONB
                    )
                """)
                for row in rows:
                    try:
                        row_dict = dict(zip(columns, row))
                        char_id = row_dict.get('id') or row_dict.get('cv_id')
                        if char_id:
                            pg_cursor.execute("""
                                INSERT INTO cv_character (id, data)
                                VALUES (%s, %s)
                                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                            """, (char_id, json.dumps(row_dict)))
                            imported_count += 1
                    except Exception as e:
                        print(f"Error importing row from cv_character: {e}", file=sys.stderr)
                        if VERBOSE:
                            import traceback
                            traceback.print_exc(file=sys.stderr)
                        continue

            else:
                # Skip FTS (Full-Text Search) tables - they're SQLite-specific
                if table.endswith('_fts') or table.endswith('_fts_data') or table.endswith('_fts_docsize') or table.endswith('_fts_config') or table.endswith('_fts_idx'):
                    print(f"  Skipping FTS table: {table}", file=sys.stderr)
                    continue

                # Skip sqlite_stat1 (SQLite statistics table)
                if table == 'sqlite_stat1':
                    print(f"  Skipping SQLite system table: {table}", file=sys.stderr)
                    continue

                # Import other tables generically (cv_sync_metadata, comic_files, comic_covers, etc.)
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

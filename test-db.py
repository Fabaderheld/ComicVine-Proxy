#!/usr/bin/env python3
"""
Test script to check the database directly for issue/volume data
"""

import os
import sys
import json
import psycopg2
from psycopg2.extras import RealDictCursor

# Database configuration (from environment or defaults)
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'comicvine')
DB_USER = os.getenv('DB_USER', 'comicvine')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'comicvine')

def connect_db():
    """Connect to PostgreSQL database"""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

def check_tables(conn):
    """Check what tables exist"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    tables = [row[0] for row in cursor.fetchall()]
    print(f"\n=== Available Tables ===")
    for table in tables:
        print(f"  - {table}")
    return tables

def check_cv_issue(conn, issue_id):
    """Check cv_issue table for a specific issue"""
    print(f"\n=== Checking cv_issue table for issue ID: {issue_id} ===")

    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Check if table exists
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = 'cv_issue'
        ) as exists
    """)
    result = cursor.fetchone()
    table_exists = result['exists'] if result else False

    if not table_exists:
        print("  ✗ cv_issue table does not exist")
        return

    # Get table structure
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'cv_issue'
        ORDER BY ordinal_position
    """)
    columns = cursor.fetchall()
    print(f"\n  Table structure:")
    for col in columns:
        print(f"    - {col['column_name']}: {col['data_type']}")

    # Try to find the issue
    print(f"\n  Searching for issue ID: {issue_id}")

    # Try structure 1: id, data (JSONB)
    try:
        cursor.execute("SELECT id, data FROM cv_issue WHERE id = %s LIMIT 1", (issue_id,))
        result = cursor.fetchone()
        if result:
            print(f"  ✓ Found in cv_issue (structure: id, data)")
            print(f"    ID: {result['id']}")
            if isinstance(result['data'], dict):
                print(f"    Data keys: {list(result['data'].keys())[:10]}...")
                if 'id' in result['data']:
                    print(f"    Data ID: {result['data']['id']}")
                if 'name' in result['data']:
                    print(f"    Data Name: {result['data']['name']}")
            return result
    except Exception as e:
        print(f"    Error with structure 1: {e}")

    # Try structure 2: direct columns
    try:
        cursor.execute("SELECT * FROM cv_issue WHERE id = %s LIMIT 1", (issue_id,))
        result = cursor.fetchone()
        if result:
            print(f"  ✓ Found in cv_issue (structure: direct columns)")
            print(f"    Columns: {list(result.keys())[:10]}...")
            return result
    except Exception as e:
        print(f"    Error with structure 2: {e}")

    # Try with integer
    try:
        issue_id_int = int(issue_id)
        cursor.execute("SELECT id, data FROM cv_issue WHERE id = %s LIMIT 1", (issue_id_int,))
        result = cursor.fetchone()
        if result:
            print(f"  ✓ Found in cv_issue (integer ID)")
            return result
    except (ValueError, Exception) as e:
        pass

    print(f"  ✗ Issue {issue_id} not found in cv_issue table")

    # Show sample of what's in the table
    try:
        cursor.execute("SELECT id FROM cv_issue LIMIT 5")
        sample_ids = [row['id'] for row in cursor.fetchall()]
        if sample_ids:
            print(f"\n  Sample issue IDs in table: {sample_ids}")
    except Exception as e:
        print(f"  Error getting sample IDs: {e}")

    # Count total issues
    try:
        cursor.execute("SELECT COUNT(*) as count FROM cv_issue")
        result = cursor.fetchone()
        count = result['count'] if result else 0
        print(f"  Total issues in cv_issue table: {count}")
    except Exception as e:
        print(f"  Error counting issues: {e}")

def check_api_cache(conn, resource_type, resource_id):
    """Check api_cache table for a specific resource"""
    print(f"\n=== Checking api_cache table for {resource_type}/{resource_id} ===")

    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Check if table exists
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = 'api_cache'
        ) as exists
    """)
    result = cursor.fetchone()
    table_exists = result['exists'] if result else False

    if not table_exists:
        print("  ✗ api_cache table does not exist")
        return

    # Try to find the cached response
    cursor.execute("""
        SELECT resource_type, resource_id, cached_at, response_data
        FROM api_cache
        WHERE resource_type = %s AND resource_id = %s
        LIMIT 1
    """, (resource_type, str(resource_id)))

    result = cursor.fetchone()
    if result:
        print(f"  ✓ Found in api_cache")
        print(f"    Resource Type: {result['resource_type']}")
        print(f"    Resource ID: {result['resource_id']}")
        print(f"    Cached At: {result['cached_at']}")
        if isinstance(result['response_data'], dict):
            print(f"    Response keys: {list(result['response_data'].keys())[:10]}...")
            if '_source' in result['response_data']:
                print(f"    _source: {result['response_data']['_source']}")
        return result
    else:
        print(f"  ✗ {resource_type}/{resource_id} not found in api_cache")

        # Show sample of what's in the cache
        cursor.execute("""
            SELECT resource_type, resource_id, cached_at
            FROM api_cache
            WHERE resource_type = %s
            LIMIT 5
        """, (resource_type,))
        samples = cursor.fetchall()
        if samples:
            print(f"\n  Sample cached {resource_type} resources:")
            for sample in samples:
                print(f"    - {sample['resource_type']}/{sample['resource_id']} (cached: {sample['cached_at']})")

        # Count total cached resources
        cursor.execute("SELECT COUNT(*) as count FROM api_cache WHERE resource_type = %s", (resource_type,))
        result = cursor.fetchone()
        count = result['count'] if result else 0
        print(f"  Total cached {resource_type} resources: {count}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test-db.py <issue_id> [resource_type]")
        print("Example: python3 test-db.py 10813")
        print("Example: python3 test-db.py 10813 issue")
        sys.exit(1)

    resource_id = sys.argv[1]
    resource_type = sys.argv[2] if len(sys.argv) > 2 else 'issue'

    print(f"Testing database connection...")
    print(f"  Host: {DB_HOST}:{DB_PORT}")
    print(f"  Database: {DB_NAME}")
    print(f"  User: {DB_USER}")

    conn = connect_db()
    print("✓ Connected to database")

    # Check what tables exist
    tables = check_tables(conn)

    # Check cv_issue table
    if resource_type == 'issue':
        check_cv_issue(conn, resource_id)

    # Check api_cache table
    check_api_cache(conn, resource_type, resource_id)

    conn.close()
    print("\n=== Done ===")

if __name__ == '__main__':
    main()

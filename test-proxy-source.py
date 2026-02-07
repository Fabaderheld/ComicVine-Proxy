#!/usr/bin/env python3
"""
Test script to verify ComicVine proxy data source behavior.

This script tests:
1. Whether data comes from local database tables (cv_issue, cv_volume, etc.)
2. Whether data comes from ComicVine API when not in local DB
3. The _source field in JSON responses
4. The X-Data-Source header
5. Caching behavior (storing in correct tables)
"""

import sys
import json
import requests
import argparse
from typing import Dict, Optional, Tuple

# Default proxy URL
DEFAULT_PROXY_URL = "http://localhost:8080"
DEFAULT_API_KEY = "161aace7d28ba709ebda09bb1a5c870f58541865"

# Test cases
TEST_CASES = [
    {
        "name": "Test Issue from Local DB",
        "endpoint": "/api/issue/4000-10813",
        "expected_source": "local_database_table",
        "description": "Should return issue from cv_issue table"
    },
    {
        "name": "Test Volume from Local DB",
        "endpoint": "/api/volume/4050-1",
        "expected_source": "local_database_table",
        "description": "Should return volume from cv_volume table"
    },
    {
        "name": "Test Issue Not in Local DB",
        "endpoint": "/api/issue/4000-99999999",
        "expected_source": "comicvine_api",
        "description": "Should fetch from ComicVine API when not in local DB"
    },
    {
        "name": "Test Volume Not in Local DB",
        "endpoint": "/api/volume/4050-99999999",
        "expected_source": "comicvine_api",
        "description": "Should fetch from ComicVine API when not in local DB"
    },
    {
        "name": "Test Person Resource",
        "endpoint": "/api/person/4040-1",
        "expected_source": ["local_database_table", "comicvine_api"],
        "description": "Should check cv_person table first, then API"
    },
    {
        "name": "Test Publisher Resource",
        "endpoint": "/api/publisher/4010-1",
        "expected_source": ["local_database_table", "comicvine_api"],
        "description": "Should check cv_publisher table first, then API"
    },
    {
        "name": "Test Issue List Endpoint",
        "endpoint": "/api/issues",
        "expected_source": "comicvine_api",
        "description": "List endpoints should always fetch from API (not cached)"
    },
]


def test_endpoint(proxy_url: str, api_key: str, endpoint: str, expected_source: str | list, verbose: bool = False) -> Tuple[bool, Dict]:
    """
    Test a single endpoint and verify the data source.

    Returns:
        (success: bool, result: dict)
    """
    url = f"{proxy_url}{endpoint}"
    params = {"api_key": api_key, "format": "json"}

    try:
        if verbose:
            print(f"  Testing: {url}", file=sys.stderr)

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()

        # Check header
        header_source = response.headers.get('X-Data-Source', 'unknown')

        # Check JSON response
        try:
            json_data = response.json()
            json_source = json_data.get('_source', 'unknown')
        except:
            json_source = 'unknown'
            json_data = {}

        # Determine if test passed
        if isinstance(expected_source, list):
            passed = header_source in expected_source and json_source in expected_source
        else:
            passed = header_source == expected_source and json_source == expected_source

        result = {
            "passed": passed,
            "status_code": response.status_code,
            "header_source": header_source,
            "json_source": json_source,
            "expected": expected_source,
            "url": url,
            "has_results": 'results' in json_data
        }

        return passed, result

    except requests.exceptions.RequestException as e:
        return False, {
            "passed": False,
            "error": str(e),
            "url": url
        }
    except Exception as e:
        return False, {
            "passed": False,
            "error": f"Unexpected error: {e}",
            "url": url
        }


def test_caching(proxy_url: str, api_key: str, endpoint: str, verbose: bool = False) -> Tuple[bool, Dict]:
    """
    Test that API responses are cached in the correct table.
    Makes two requests - first should hit API, second should hit cache.
    """
    url = f"{proxy_url}{endpoint}"
    params = {"api_key": api_key, "format": "json"}

    try:
        # First request - should hit API
        if verbose:
            print(f"  First request (should hit API): {url}", file=sys.stderr)

        response1 = requests.get(url, params=params, timeout=30)
        response1.raise_for_status()
        source1 = response1.headers.get('X-Data-Source', 'unknown')

        # Small delay to ensure cache write completes
        import time
        time.sleep(0.5)

        # Second request - should hit cache (if caching works)
        if verbose:
            print(f"  Second request (should hit cache): {url}", file=sys.stderr)

        response2 = requests.get(url, params=params, timeout=30)
        response2.raise_for_status()
        source2 = response2.headers.get('X-Data-Source', 'unknown')

        # Check if second request used cache
        # Note: If data is in local DB table, both will show local_database_table
        # If data was fetched from API, second should show local_database_table (cached)
        cached = (source1 == 'comicvine_api' and source2 == 'local_database_table') or \
                 (source1 == 'local_database_table' and source2 == 'local_database_table')

        result = {
            "passed": cached,
            "first_source": source1,
            "second_source": source2,
            "url": url,
            "cached": cached
        }

        return cached, result

    except Exception as e:
        return False, {
            "passed": False,
            "error": str(e),
            "url": url
        }


def main():
    parser = argparse.ArgumentParser(description="Test ComicVine proxy data source behavior")
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY_URL,
                       help=f"Proxy URL (default: {DEFAULT_PROXY_URL})")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY,
                       help="ComicVine API key")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Verbose output")
    parser.add_argument("--test-caching", action="store_true",
                       help="Test caching behavior")
    parser.add_argument("--test-id", type=int,
                       help="Run only a specific test case by index (0-based)")

    args = parser.parse_args()

    print("=" * 70)
    print("ComicVine Proxy Data Source Test Suite")
    print("=" * 70)
    print(f"Proxy URL: {args.proxy_url}")
    print(f"API Key: {args.api_key[:20]}...")
    print()

    # Run test cases
    passed = 0
    failed = 0
    results = []

    test_cases = TEST_CASES
    if args.test_id is not None:
        if 0 <= args.test_id < len(test_cases):
            test_cases = [test_cases[args.test_id]]
        else:
            print(f"Error: Test ID {args.test_id} out of range (0-{len(TEST_CASES)-1})")
            sys.exit(1)

    for i, test_case in enumerate(test_cases):
        print(f"Test {i+1}/{len(test_cases)}: {test_case['name']}")
        print(f"  Description: {test_case['description']}")
        print(f"  Endpoint: {test_case['endpoint']}")

        success, result = test_endpoint(
            args.proxy_url,
            args.api_key,
            test_case['endpoint'],
            test_case['expected_source'],
            args.verbose
        )

        if success:
            print(f"  ✓ PASSED")
            print(f"    Header: X-Data-Source = {result['header_source']}")
            print(f"    JSON: _source = {result['json_source']}")
            passed += 1
        else:
            print(f"  ✗ FAILED")
            if 'error' in result:
                print(f"    Error: {result['error']}")
            else:
                print(f"    Expected: {test_case['expected_source']}")
                print(f"    Header: X-Data-Source = {result.get('header_source', 'unknown')}")
                print(f"    JSON: _source = {result.get('json_source', 'unknown')}")
            failed += 1

        results.append(result)
        print()

    # Test caching if requested
    if args.test_caching:
        print("=" * 70)
        print("Caching Behavior Test")
        print("=" * 70)

        # Test with an issue that's likely not in the DB
        test_endpoint = "/api/issue/4000-99999998"
        print(f"Testing caching for: {test_endpoint}")

        success, result = test_caching(
            args.proxy_url,
            args.api_key,
            test_endpoint,
            args.verbose
        )

        if success:
            print(f"  ✓ PASSED - Caching works correctly")
            print(f"    First request: {result['first_source']}")
            print(f"    Second request: {result['second_source']}")
        else:
            print(f"  ✗ FAILED - Caching may not be working")
            print(f"    First request: {result.get('first_source', 'unknown')}")
            print(f"    Second request: {result.get('second_source', 'unknown')}")
            if 'error' in result:
                print(f"    Error: {result['error']}")
        print()

    # Summary
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Total tests: {len(test_cases)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print()

    if failed > 0:
        print("Failed tests:")
        for i, (test_case, result) in enumerate(zip(test_cases, results)):
            if not result.get('passed', False):
                print(f"  {i+1}. {test_case['name']}")
                if 'error' in result:
                    print(f"     Error: {result['error']}")
        print()

    # Exit code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

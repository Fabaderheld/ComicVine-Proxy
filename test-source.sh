#!/bin/bash
# Test script to check data source (local DB vs ComicVine API)

PROXY_URL="${1:-http://localhost:8080}"
TEST_ISSUE_ID="${2:-10813}"
TEST_VOLUME_ID="${3:-1}"

echo "=== Testing Data Source ==="
echo "Proxy URL: $PROXY_URL"
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

test_endpoint() {
    local endpoint=$1
    local name=$2

    echo -e "${BLUE}Testing: $name${NC}"
    echo "  Endpoint: $endpoint"

    # Get response with headers
    response=$(curl -s -i "$endpoint")
    status_code=$(echo "$response" | head -1 | grep -oP '\d{3}')
    body=$(echo "$response" | sed -n '/^\r$/,$p' | tail -n +2)

    if [ -z "$status_code" ] || [ "$status_code" != "200" ]; then
        echo -e "  ${RED}✗ Failed (HTTP ${status_code:-unknown})${NC}"
        echo "$body" | head -3
        echo ""
        return 1
    fi

    # Check for X-Data-Source header
    source_header=$(echo "$response" | grep -i "X-Data-Source:" | cut -d' ' -f2 | tr -d '\r')

    # Check for _source field in JSON
    source_json=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('_source', 'not_found'))" 2>/dev/null || echo "parse_error")

    # Determine source
    if [ -n "$source_header" ]; then
        source="$source_header"
    elif [ "$source_json" != "not_found" ] && [ "$source_json" != "parse_error" ]; then
        source="$source_json"
    else
        source="unknown"
    fi

    case "$source" in
        "local_database_table")
            echo -e "  ${GREEN}✓ Source: Local Database Table (cv_issue/cv_volume)${NC}"
            ;;
        "api_cache")
            echo -e "  ${YELLOW}✓ Source: API Cache (previously fetched from ComicVine)${NC}"
            ;;
        "comicvine_api")
            echo -e "  ${RED}✓ Source: ComicVine API (live fetch)${NC}"
            ;;
        *)
            echo -e "  ${YELLOW}? Source: Unknown${NC}"
            echo "    Header: $source_header"
            echo "    JSON: $source_json"
            ;;
    esac

    # Show response time
    time_taken=$(curl -s -o /dev/null -w "%{time_total}" "$endpoint")
    echo "  Response time: ${time_taken}s"

    # Show headers
    echo "  Headers:"
    echo "$response" | grep -i "X-Data-Source:" | sed 's/^/    /' || echo "    (no X-Data-Source header)"

    # Show sample data
    echo "  Sample JSON:"
    echo "$body" | python3 -m json.tool 2>/dev/null | head -10 | sed 's/^/    /' || echo "$body" | head -5 | sed 's/^/    /'
    echo ""
}

echo "1. Testing Issue Endpoint"
test_endpoint "$PROXY_URL/api/issue/4000-$TEST_ISSUE_ID?api_key=test" "Issue $TEST_ISSUE_ID"

echo "2. Testing Volume Endpoint"
test_endpoint "$PROXY_URL/api/volume/4050-$TEST_VOLUME_ID?api_key=test" "Volume $TEST_VOLUME_ID"

echo "3. Testing Health Endpoint"
test_endpoint "$PROXY_URL/health" "Health Check"

echo "=== Source Legend ==="
echo -e "${GREEN}local_database_table${NC} = Data from your imported SQLite database (fastest, no API calls)"
echo -e "${YELLOW}api_cache${NC} = Data from cached ComicVine API responses (fast, previously fetched)"
echo -e "${RED}comicvine_api${NC} = Data fetched live from ComicVine API (slowest, uses API quota)"
echo ""

echo "=== Tips ==="
echo "• Check the X-Data-Source header or _source JSON field"
echo "• If you see 'comicvine_api', the data wasn't in your local database"
echo "• If you see 'api_cache', it was previously fetched and cached"
echo "• If you see 'local_database_table', it came directly from your SQLite import"
echo "• Run with --verbose to see detailed logs: docker-compose logs -f proxy"
echo ""

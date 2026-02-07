#!/bin/bash
# Test script for ComicVine API Proxy

PROXY_URL="${1:-http://localhost:8080}"

echo "Testing ComicVine API Proxy at $PROXY_URL"
echo "=========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Health check
echo -e "${YELLOW}Test 1: Health Check${NC}"
echo "curl $PROXY_URL/health"
echo ""
response=$(curl -s "$PROXY_URL/health")
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
echo ""
echo "---"
echo ""

# Test 2: Root endpoint
echo -e "${YELLOW}Test 2: Root Endpoint${NC}"
echo "curl $PROXY_URL/"
echo ""
response=$(curl -s "$PROXY_URL/")
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
echo ""
echo "---"
echo ""

# Test 3: Get a specific issue (example: issue 4000-1)
echo -e "${YELLOW}Test 3: Get Issue Detail${NC}"
echo "curl \"$PROXY_URL/api/issue/4000-1?api_key=test\""
echo ""
response=$(curl -s "$PROXY_URL/api/issue/4000-1?api_key=test")
status_code=$(curl -s -o /dev/null -w "%{http_code}" "$PROXY_URL/api/issue/4000-1?api_key=test")
if [ "$status_code" = "200" ]; then
    echo -e "${GREEN}✓ Status: $status_code${NC}"
    echo "$response" | python3 -m json.tool 2>/dev/null | head -30 || echo "$response" | head -30
else
    echo -e "${RED}✗ Status: $status_code${NC}"
    echo "$response" | head -5
fi
echo ""
echo "---"
echo ""

# Test 4: Get a specific volume (example: volume 4050-1)
echo -e "${YELLOW}Test 4: Get Volume Detail${NC}"
echo "curl \"$PROXY_URL/api/volume/4050-1?api_key=test\""
echo ""
response=$(curl -s "$PROXY_URL/api/volume/4050-1?api_key=test")
status_code=$(curl -s -o /dev/null -w "%{http_code}" "$PROXY_URL/api/volume/4050-1?api_key=test")
if [ "$status_code" = "200" ]; then
    echo -e "${GREEN}✓ Status: $status_code${NC}"
    echo "$response" | python3 -m json.tool 2>/dev/null | head -30 || echo "$response" | head -30
else
    echo -e "${RED}✗ Status: $status_code${NC}"
    echo "$response" | head -5
fi
echo ""
echo "---"
echo ""

# Test 5: List volumes (with limit)
echo -e "${YELLOW}Test 5: List Volumes (limited)${NC}"
echo "curl \"$PROXY_URL/api/volumes?api_key=test&limit=5\""
echo ""
response=$(curl -s "$PROXY_URL/api/volumes?api_key=test&limit=5")
status_code=$(curl -s -o /dev/null -w "%{http_code}" "$PROXY_URL/api/volumes?api_key=test&limit=5")
if [ "$status_code" = "200" ]; then
    echo -e "${GREEN}✓ Status: $status_code${NC}"
    echo "$response" | python3 -m json.tool 2>/dev/null | head -40 || echo "$response" | head -40
else
    echo -e "${RED}✗ Status: $status_code${NC}"
    echo "$response" | head -5
fi
echo ""
echo "---"
echo ""

# Test 6: List issues (with limit)
echo -e "${YELLOW}Test 6: List Issues (limited)${NC}"
echo "curl \"$PROXY_URL/api/issues?api_key=test&limit=5\""
echo ""
response=$(curl -s "$PROXY_URL/api/issues?api_key=test&limit=5")
status_code=$(curl -s -o /dev/null -w "%{http_code}" "$PROXY_URL/api/issues?api_key=test&limit=5")
if [ "$status_code" = "200" ]; then
    echo -e "${GREEN}✓ Status: $status_code${NC}"
    echo "$response" | python3 -m json.tool 2>/dev/null | head -40 || echo "$response" | head -40
else
    echo -e "${RED}✗ Status: $status_code${NC}"
    echo "$response" | head -5
fi
echo ""
echo "=========================================="
echo "Testing complete!"

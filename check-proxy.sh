#!/bin/bash
# Diagnostic script to check proxy status

echo "=== ComicVine Proxy Diagnostics ==="
echo ""

# Check if container is running
echo "1. Checking if containers are running..."
if command -v docker &> /dev/null; then
    docker ps --filter "name=comicvine" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
else
    echo "   Docker command not found"
fi
echo ""

# Check if port is listening
echo "2. Checking if port 8080 is listening..."
if command -v netstat &> /dev/null; then
    netstat -tlnp 2>/dev/null | grep :8080 || echo "   Port 8080 is not listening"
elif command -v ss &> /dev/null; then
    ss -tlnp 2>/dev/null | grep :8080 || echo "   Port 8080 is not listening"
else
    echo "   Cannot check port (netstat/ss not available)"
fi
echo ""

# Test basic connectivity
echo "3. Testing basic connectivity..."
if curl -s --max-time 2 http://localhost:8080/health > /dev/null 2>&1; then
    echo "   ✓ Proxy is responding"
    curl -s http://localhost:8080/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8080/health
else
    echo "   ✗ Proxy is not responding"
    echo "   Trying to get error details..."
    curl -v http://localhost:8080/health 2>&1 | head -20
fi
echo ""

# Check container logs if docker is available
if command -v docker &> /dev/null; then
    echo "4. Recent container logs (last 20 lines)..."
    docker logs comicvine-proxy --tail 20 2>&1 || echo "   Could not get logs"
    echo ""
fi

echo "=== End Diagnostics ==="

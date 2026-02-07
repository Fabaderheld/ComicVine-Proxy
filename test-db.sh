#!/bin/bash
# Wrapper script to test database from inside the container or locally

ISSUE_ID="${1:-10813}"
RESOURCE_TYPE="${2:-issue}"

echo "Testing database for ${RESOURCE_TYPE} ID: ${ISSUE_ID}"
echo ""

# Check if we're running inside Docker or need to use docker-compose exec
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    # Running inside container
    python3 test-db.py "$ISSUE_ID" "$RESOURCE_TYPE"
else
    # Running locally - use docker-compose exec
    echo "Running test inside proxy container..."
    docker-compose exec -T proxy python3 test-db.py "$ISSUE_ID" "$RESOURCE_TYPE"
fi

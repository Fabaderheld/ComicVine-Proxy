#!/bin/bash
set -e

# Default values
DB_HOST=${DB_HOST:-db}
DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-comicvine}
DB_USER=${DB_USER:-comicvine}
DB_PASSWORD=${DB_PASSWORD:-comicvine}
PROXY_HOST=${PROXY_HOST:-0.0.0.0}
PROXY_PORT=${PROXY_PORT:-8080}

# Build command arguments
ARGS=(
    "--db-host" "$DB_HOST"
    "--db-port" "$DB_PORT"
    "--db-name" "$DB_NAME"
    "--db-user" "$DB_USER"
    "--db-password" "$DB_PASSWORD"
    "--host" "$PROXY_HOST"
    "--port" "$PROXY_PORT"
)

# Add import-sqlite if file exists
if [ -n "$IMPORT_SQLITE" ] && [ -f "$IMPORT_SQLITE" ]; then
    echo "Using SQLite file from IMPORT_SQLITE env: $IMPORT_SQLITE"
    ARGS+=("--import-sqlite" "$IMPORT_SQLITE")
elif [ -d /app/import ]; then
    # List all .db files in the import directory
    echo "Checking for SQLite files in /app/import..."
    ls -la /app/import/ 2>/dev/null || echo "  Directory exists but may be empty"

    SQLITE_FILE=$(find /app/import -name "*.db" -type f 2>/dev/null | head -1)
    if [ -n "$SQLITE_FILE" ] && [ -f "$SQLITE_FILE" ]; then
        echo "Found SQLite file: $SQLITE_FILE"
        echo "  File size: $(stat -c%s "$SQLITE_FILE" 2>/dev/null || echo 'unknown') bytes"
        ARGS+=("--import-sqlite" "$SQLITE_FILE")
    else
        echo "No SQLite .db files found in /app/import"
    fi
fi

# Add API key if provided
if [ -n "$COMICVINE_API_KEY" ]; then
    ARGS+=("--api-key" "$COMICVINE_API_KEY")
fi

# Add verbose if set
if [ "$VERBOSE" = "true" ] || [ "$VERBOSE" = "1" ]; then
    ARGS+=("--verbose")
fi

# Wait for database to be ready
echo "Waiting for database to be ready..."
until PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c '\q' 2>/dev/null; do
    echo "Database is unavailable - sleeping"
    sleep 1
done

echo "Database is ready!"

# Run the application
exec python comicvine-proxy.py "${ARGS[@]}"

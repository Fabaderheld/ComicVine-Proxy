# ComicVine API Proxy

A transparent HTTP proxy server that intercepts ComicVine API calls and provides intelligent caching using a PostgreSQL database. This proxy reduces API rate limiting issues and speeds up responses by serving cached data when available.

## Features

- ✅ **Transparent Proxy**: Works as a drop-in replacement for `comicvine.gamespot.com`
- ✅ **PostgreSQL Database**: Uses PostgreSQL/MariaDB for reliable, scalable caching
- ✅ **Docker Support**: Fully containerized with docker-compose
- ✅ **SQLite Import**: Automatically import existing SQLite databases on startup
- ✅ **Automatic Caching**: Stores API responses in the database for future use
- ✅ **Image Storage**: Downloads and stores images from ComicVine URLs in the database
- ✅ **Web UI**: Browse and search your cached database at `/web`
- ✅ **Full API Support**: Handles all ComicVine API endpoints (issues, volumes, characters, etc.)
- ✅ **Query Parameter Support**: Preserves filters, sorting, pagination, and field lists
- ✅ **Fallback to Real API**: Automatically fetches from ComicVine if data isn't cached

## Quick Start with Docker

### Using Docker Compose (Recommended)

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/ComicVine-Proxy.git
   cd ComicVine-Proxy
   ```

2. **Create environment file**
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

3. **Import SQLite database (optional)**
   ```bash
   # Place your SQLite database file in sqlite_import/ directory
   mkdir -p sqlite_import
   cp ~/path/to/localcv.db sqlite_import/
   ```

4. **Start services**
   ```bash
   docker-compose up -d
   ```

The proxy will be available at `http://localhost:8080`

### Manual Docker Build

```bash
# Build the image
docker build -t comicvine-proxy .

# Run with PostgreSQL
docker run -d \
  --name comicvine-proxy \
  -p 8080:8080 \
  -e DB_HOST=your-db-host \
  -e DB_NAME=comicvine \
  -e DB_USER=comicvine \
  -e DB_PASSWORD=your-password \
  -e COMICVINE_API_KEY=your-api-key \
  comicvine-proxy
```

## Installation (Without Docker)

### Prerequisites

- Python 3.11+
- PostgreSQL 12+ or MariaDB 10.5+

### Steps

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set up PostgreSQL database**
   ```sql
   CREATE DATABASE comicvine;
   CREATE USER comicvine WITH PASSWORD 'your-password';
   GRANT ALL PRIVILEGES ON DATABASE comicvine TO comicvine;
   ```

3. **Run the proxy**
   ```bash
   python3 comicvine-proxy.py \
     --db-host localhost \
     --db-name comicvine \
     --db-user comicvine \
     --db-password your-password \
     --port 8080
   ```

## SQLite Import

To import an existing SQLite database:

```bash
python3 comicvine-proxy.py \
  --db-host localhost \
  --db-name comicvine \
  --import-sqlite ~/path/to/localcv.db \
  --port 8080
```

Or with Docker Compose, place the SQLite file in `sqlite_import/` directory and it will be automatically imported on first startup.

## Configuration

### Environment Variables

```bash
# Database Configuration
DB_HOST=localhost          # Database host
DB_PORT=5432              # Database port
DB_NAME=comicvine         # Database name
DB_USER=comicvine         # Database user
DB_PASSWORD=comicvine     # Database password

# Proxy Configuration
PROXY_PORT=8080           # Proxy port

# ComicVine API Key (optional, for fallback)
COMICVINE_API_KEY=        # Your ComicVine API key
```

### Command Line Options

```
--db-host HOST           Database host (default: localhost)
--db-port PORT           Database port (default: 5432)
--db-name NAME           Database name (default: comicvine)
--db-user USER           Database user (default: comicvine)
--db-password PASSWORD   Database password (default: comicvine)
--import-sqlite PATH     Path to SQLite database file to import
--api-key KEY            ComicVine API key (optional, for fallback)
--port PORT              Port to listen on (default: 8080)
--host HOST              Host to bind to (default: 127.0.0.1)
--verbose                Enable verbose logging
```

## How It Works

1. **Request comes in**: `/api/issue/4000-12345`
2. **Check cache**: Looks in PostgreSQL database for `issue/12345`
3. **If found**: Returns cached response immediately
4. **If not found**: Fetches from ComicVine API
5. **Cache result**: Stores in PostgreSQL database for next time
6. **Return response**: Same format as ComicVine API

## Supported Endpoints

The proxy supports all ComicVine API endpoints according to the [official documentation](https://comicvine.gamespot.com/api/documentation):

### Detail Endpoints
- `/api/issue/{prefix}-{id}`
- `/api/volume/{prefix}-{id}`
- `/api/character/{prefix}-{id}`
- And many more...

### List Endpoints
- `/api/issues`
- `/api/volumes`
- `/api/characters`
- And many more...

### Query Parameters
All ComicVine query parameters are supported:
- `filter` - Filter results by field values
- `sort` - Sort results by field
- `limit` - Number of results per page
- `offset` - Pagination offset
- `field_list` - Specify which fields to return
- `format` - Response format (json/xml/jsonp)

## Making It Transparent

Since applications like Kapowarr may be hardcoded to use `comicvine.gamespot.com`, you have several options:

### Option 1: Hosts File (Simple)
Add to `/etc/hosts` (requires sudo):
```
127.0.0.1 comicvine.gamespot.com
```

Then run the proxy on port 80 (requires sudo):
```bash
sudo python3 comicvine-proxy.py --db-host localhost --port 80
```

### Option 2: Application Configuration
If your application supports it, configure it to use `http://localhost:8080` instead of `https://comicvine.gamespot.com`.

### Option 3: Reverse Proxy
Use nginx or similar to forward `comicvine.gamespot.com` requests to your local proxy.

## Database Schema

The proxy creates an `api_cache` table and an `image_cache` table in PostgreSQL:

```sql
CREATE TABLE api_cache (
    id SERIAL PRIMARY KEY,
    resource_type VARCHAR(50) NOT NULL,
    resource_id VARCHAR(255) NOT NULL,
    response_data JSONB NOT NULL,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(resource_type, resource_id)
);

CREATE INDEX idx_resource_lookup ON api_cache(resource_type, resource_id);

CREATE TABLE image_cache (
    url_hash VARCHAR(64) PRIMARY KEY,
    source_url TEXT NOT NULL,
    image_data BYTEA NOT NULL,
    content_type VARCHAR(100) DEFAULT 'image/jpeg',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Web UI

A simple Web UI is available at `/web` for browsing and searching your cached database:

- **Browse** by Publishers, Volumes, Characters, Issues, or People
- **Search** across all resource types
- **View details** for issues, volumes, characters, publishers, and people

Open `http://localhost:8080/web` in your browser after starting the proxy.

## Image Storage

When the proxy caches API responses (from ComicVine or your database), it automatically:
- Downloads images from ComicVine URLs
- Stores them in the `image_cache` database table
- Serves cached images at `/images/<hash>` for faster loading

API responses are updated to use local proxy URLs when images are available in the cache.

## Health Check

Check if the proxy is running:
```bash
curl http://localhost:8080/health
```

## Docker Compose Services

- **db**: PostgreSQL database container
- **proxy**: ComicVine API proxy server

Data is persisted in a Docker volume named `postgres_data`.

## Testing

### Test Data Source Behavior

The proxy includes a test script to verify that data is being served from the correct source (local database vs ComicVine API):

```bash
# Run all tests
python3 test-proxy-source.py

# Run with verbose output
python3 test-proxy-source.py --verbose

# Test specific test case (0-based index)
python3 test-proxy-source.py --test-id 0

# Test caching behavior
python3 test-proxy-source.py --test-caching

# Use custom proxy URL
python3 test-proxy-source.py --proxy-url http://localhost:8080 --api-key YOUR_API_KEY
```

The test script verifies:
- ✅ Data comes from local database tables (`cv_issue`, `cv_volume`, etc.) when available
- ✅ Data comes from ComicVine API when not in local database
- ✅ `_source` field is correctly set in JSON responses
- ✅ `X-Data-Source` header is correctly set
- ✅ Caching behavior (API responses are stored in correct tables)

### Test Database Content

Check what's in your PostgreSQL database:

```bash
# Run inside Docker container
docker-compose exec proxy python3 test-db.py

# Or run locally (requires database connection)
python3 test-db.py --db-host localhost --db-name comicvine --db-user comicvine --db-password comicvine
```

### Test Source Indicator

Quick test to see where data is coming from:

```bash
# Test an issue
curl -s "http://localhost:8080/api/issue/4000-10813?api_key=YOUR_API_KEY" | python3 -c "import sys, json; d=json.load(sys.stdin); print('Source:', d.get('_source', 'unknown'))"

# Check header
curl -I "http://localhost:8080/api/issue/4000-10813?api_key=YOUR_API_KEY" | grep -i "X-Data-Source"
```

## Troubleshooting

### Database Connection Issues

```bash
# Check database is running
docker-compose ps

# Check database logs
docker-compose logs db

# Test database connection
docker-compose exec db psql -U comicvine -d comicvine
```

### Import Issues

If SQLite import fails:
1. Check the SQLite file path is correct
2. Ensure the file is readable
3. Check database connection is working
4. Review verbose logs: `docker-compose logs proxy`

## License

MIT License - see LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

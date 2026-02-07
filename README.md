# ComicVine API Proxy

A transparent HTTP proxy server that intercepts ComicVine API calls and provides intelligent caching using a local SQLite database. This proxy reduces API rate limiting issues and speeds up responses by serving cached data when available.

## Features

- ✅ **Transparent Proxy**: Works as a drop-in replacement for `comicvine.gamespot.com`
- ✅ **Local Database First**: Checks your SQLite database before making API calls
- ✅ **Automatic Caching**: Stores API responses in the database for future use
- ✅ **Full API Support**: Handles all ComicVine API endpoints (issues, volumes, characters, etc.)
- ✅ **Query Parameter Support**: Preserves filters, sorting, pagination, and field lists
- ✅ **Fallback to Real API**: Automatically fetches from ComicVine if data isn't cached

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/ComicVine-Proxy.git
cd ComicVine-Proxy

# Install dependencies
pip install -r requirements.txt
```

## Usage

### Basic Usage

```bash
# Start the proxy server
python3 comicvine-proxy.py --db ~/path/to/localcv.db --port 8080
```

### With API Key (for fallback)

```bash
# Using command line argument
python3 comicvine-proxy.py --db ~/localcv.db --api-key YOUR_API_KEY --port 8080

# Or using environment variable
export COMICVINE_API_KEY=your_api_key
python3 comicvine-proxy.py --db ~/localcv.db --port 8080
```

### Verbose Mode

```bash
python3 comicvine-proxy.py --db ~/localcv.db --verbose
```

### Command Line Options

```
--db PATH          Path to local ComicVine SQLite database (required)
--api-key KEY      ComicVine API key (optional, for fallback)
--port PORT        Port to listen on (default: 8080)
--host HOST        Host to bind to (default: 127.0.0.1)
--verbose          Enable verbose logging
```

## How It Works

1. **Request comes in**: `/api/issue/4000-12345`
2. **Check cache**: Looks in database for `issue/12345`
3. **If found**: Returns cached response immediately
4. **If not found**: Fetches from ComicVine API
5. **Cache result**: Stores in database for next time
6. **Return response**: Same format as ComicVine API

## Supported Endpoints

The proxy supports all ComicVine API endpoints according to the [official documentation](https://comicvine.gamespot.com/api/documentation):

### Detail Endpoints
- `/api/issue/{prefix}-{id}`
- `/api/volume/{prefix}-{id}`
- `/api/character/{prefix}-{id}`
- `/api/concept/{prefix}-{id}`
- `/api/object/{prefix}-{id}`
- `/api/origin/{prefix}-{id}`
- `/api/person/{prefix}-{id}`
- `/api/power/{prefix}-{id}`
- `/api/story_arc/{prefix}-{id}`
- `/api/team/{prefix}-{id}`
- `/api/location/{prefix}-{id}`
- `/api/video/{prefix}-{id}`
- `/api/publisher/{prefix}-{id}`
- `/api/series/{prefix}-{id}`
- `/api/episode/{prefix}-{id}`

### List Endpoints
- `/api/issues`
- `/api/volumes`
- `/api/characters`
- `/api/concepts`
- `/api/objects`
- `/api/origins`
- `/api/people`
- `/api/powers`
- `/api/story_arcs`
- `/api/teams`
- `/api/locations`
- `/api/videos`
- `/api/publishers`
- `/api/series`
- `/api/episodes`

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

### Option 1: System Proxy
Configure your system to use the proxy for ComicVine requests.

### Option 2: Hosts File (Simple)
Add to `/etc/hosts` (requires sudo):
```
127.0.0.1 comicvine.gamespot.com
```

Then run the proxy on port 80 (requires sudo):
```bash
sudo python3 comicvine-proxy.py --db ~/localcv.db --port 80
```

### Option 3: Application Configuration
If your application supports it, configure it to use `http://localhost:8080` instead of `https://comicvine.gamespot.com`.

## Database Schema

The proxy creates an `api_cache` table in your database:

```sql
CREATE TABLE api_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_data TEXT NOT NULL,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(resource_type, resource_id)
);
```

## Health Check

Check if the proxy is running:
```bash
curl http://localhost:8080/health
```

## License

MIT License - see LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

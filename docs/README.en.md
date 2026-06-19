<h1 align="center">Grok2API</h1>

<p align="center">
  <strong>An OpenAI-compatible Grok gateway maintained for the current grok.com / console.x.ai behavior</strong>
</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white"></a>
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white"></a>
  <a href="../pyproject.toml"><img alt="Version" src="https://img.shields.io/badge/version-2.0.9-111827"></a>
  <a href="../LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
  <a href="../README.md"><img alt="Chinese" src="https://img.shields.io/badge/中文-2563EB?logo=bookstack&logoColor=white"></a>
</p>

> [!IMPORTANT]
> The original upstream repository has been archived and is no longer maintained.

> [!NOTE]
> This project is for learning, research, and self-hosted gateway use. Follow xAI / Grok terms and local laws. Account cookies, Cloudflare clearance, and API keys are sensitive credentials.

## What This Is

Grok2API wraps Grok Web, console.x.ai, Imagine, and media APIs behind OpenAI / Anthropic-compatible HTTP endpoints. It is built for self-hosted deployments that need account pools, unified auth, streaming output, image/video proxying, Admin management, and WebUI pages.

## Features

- OpenAI-compatible APIs: `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/images/edits`, `/v1/videos`.
- Anthropic-compatible API: `/v1/messages`.
- Account pools: basic / lite / super / heavy, with local, Redis, MySQL, and PostgreSQL storage backends.
- Quota and feedback: grok.com text modes use upstream quota; console.x.ai has independent runtime rate-limit parsing and cooldown.
- Media: text-to-image, image editing, text-to-video, image-to-video, local image/video cache and proxy URLs.
- Web products: Admin, Web Chat, Masonry image UI, and ChatKit voice UI.
- Proxy and Cloudflare support: direct, single proxy, proxy pool, manual clearance, and FlareSolverr.

## Quick Start

### Docker

```bash
docker run -d \
  --name grok2api \
  -p 8000:8000 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/piexian/grok2api:latest
```

| Image | Notes |
| :-- | :-- |
| `ghcr.io/piexian/grok2api:latest` | Current latest, pointing to the 2.0.9 line |
| `ghcr.io/piexian/grok2api:2.0.9` | Fixed version tag |

### Docker Compose

```bash
git clone https://github.com/piexian/grok2api
cd grok2api
cp .env.example .env
docker compose up -d
```

### Local

```bash
git clone https://github.com/piexian/grok2api
cd grok2api
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

Minimum first-run settings:

| Setting | Purpose |
| :-- | :-- |
| `app.api_key` | `/v1/*` API authentication |
| `app.app_key` | Admin password |
| `app.app_url` | Public base URL for local image/video proxy links |
| `app.webui_enabled` / `app.webui_key` | WebUI switch and password |

## Web Pages

| Page | Path |
| :-- | :-- |
| Admin login | `/admin/login` |
| Account management | `/admin/account` |
| Config management | `/admin/config` |
| Cache management | `/admin/cache` |
| WebUI login | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry image UI | `/webui/masonry` |
| ChatKit voice UI | `/webui/chatkit` |

## Models

Use `GET /v1/models` to inspect models available to the currently configured account pools. Super-only models are hidden when no super account is available.

### Chat

| Model | Upstream path | mode / model | Tier |
| :-- | :-- | :-- | :-- |
| `grok-4.3-fast` | grok.com app-chat | `fast` | basic |
| `grok-4.3-auto` | grok.com app-chat | `auto` | super+ |
| `grok-4.3-expert` | grok.com app-chat | `expert` | super+ |
| `grok-4.3-heavy` | grok.com app-chat | `heavy` | heavy |
| `grok-4.3` | console.x.ai `/v1/responses` | `grok-4.3` | basic |
| `grok-build-0.1` | console.x.ai `/v1/responses` | `grok-build-0.1` | basic |
| `grok-4.20-0309-non-reasoning` | console.x.ai `/v1/responses` | same | basic |
| `grok-4.20-0309-reasoning` | console.x.ai `/v1/responses` | same | basic |
| `grok-4.20-multi-agent-0309` | console.x.ai `/v1/responses` | same | basic |

Console notes:

- console.x.ai uses Grok SSO cookies but has its own rate limits.
- Request parameters are normalized per console model for upstream compatibility.
- 429 responses are cooled down per console model, separately from grok.com Chat.

### Image

| Model | Upstream path | Tier | Notes |
| :-- | :-- | :-- | :-- |
| `grok-imagine-image-lite` | grok.com app-chat | basic | no precise aspect-ratio control |
| `grok-imagine-image` | Imagine WebSocket | super+ | speed mode |
| `grok-imagine-image-pro` | Imagine WebSocket | super+ | quality/pro mode |

### Image Edit

| Model | Upstream path | Tier |
| :-- | :-- | :-- |
| `grok-imagine-image-edit` | grok.com app-chat edit flow | super+ |

### Video

| Model | Upstream path | Tier |
| :-- | :-- | :-- |
| `grok-imagine-video` | grok.com media API | super+ |

## API Examples

The examples use `http://localhost:8000`.

### List Models

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $GROK2API_API_KEY"
```

### Chat Completions

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-auto",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Explain quantum tunneling in three sentences"}
    ]
  }'
```

### Responses

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3",
    "input": "Search and summarize today's AI news",
    "stream": true
  }'
```

### Anthropic Messages

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-auto",
    "max_tokens": 1024,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Write a FastAPI health-check example"}
    ]
  }'
```

### Images

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "A cat floating in space, cinematic",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

Image parameters:

| Field | Description |
| :-- | :-- |
| `model` | `grok-imagine-image-lite`, `grok-imagine-image`, or `grok-imagine-image-pro` |
| `n` | `1-4` for lite, `1-10` for other image models |
| `size` | `1280x720`, `720x1280`, `1792x1024`, `1024x1792`, `1024x1024` |
| `response_format` | `url` or `b64_json` |

### Image Edit

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-image-edit" \
  -F "prompt=Make this image sharper" \
  -F "image[]=@/path/to/image.png" \
  -F "n=1" \
  -F "size=1024x1024" \
  -F "response_format=url"
```

### Videos

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=A neon rainy street at night, cinematic slow tracking shot" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

Query and download:

```bash
curl http://localhost:8000/v1/videos/<video_id> \
  -H "Authorization: Bearer $GROK2API_API_KEY"

curl -L http://localhost:8000/v1/videos/<video_id>/content \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -o result.mp4
```

Video parameters:

| Field | Description |
| :-- | :-- |
| `seconds` | `6`, `10`, `12`, `16`, `20` |
| `size` | `720x1280`, `1280x720`, `1024x1024`, `1024x1792`, `1792x1024` |
| `resolution_name` | `480p` or `720p` |
| `preset` | `fun`, `normal`, `spicy`, `custom` |
| `input_reference[]` | Optional image-to-video reference images; only the first 7 are used |

## Configuration

Config sources are merged in this order:

| Source | Notes |
| :-- | :-- |
| Environment variables | Startup overrides; supports `GROK_` prefix |
| `${DATA_DIR}/config.toml` | Runtime config saved by Admin |
| `config.defaults.toml` | First-run defaults |

Common environment variables:

| Variable | Default | Description |
| :-- | :-- | :-- |
| `SERVER_HOST` | `0.0.0.0` | Bind host |
| `SERVER_PORT` | `8000` | Bind port |
| `DATA_DIR` | `./data` | Account DB, config, and local media cache |
| `LOG_DIR` | `./logs` | Log directory |
| `ACCOUNT_STORAGE` | `local` | `local`, `redis`, `mysql`, or `postgresql` |
| `GROK_APP_API_KEY` | empty | Overrides `app.api_key` |
| `GROK_APP_APP_KEY` | `grok2api` | Overrides Admin password |
| `GROK_APP_APP_URL` | empty | Public app URL |

Important config groups:

| Group | Keys |
| :-- | :-- |
| `app` | `app_key`, `app_url`, `api_key`, `webui_enabled`, `webui_key` |
| `features` | `stream`, `thinking`, `memory`, `show_search_sources`, `image_format`, `video_format` |
| `proxy.egress` | `mode`, `proxy_url`, `proxy_pool`, `resource_proxy_url`, `skip_ssl_verify` |
| `proxy.clearance` | `mode`, `cf_cookies`, `user_agent`, `browser`, `flaresolverr_url` |
| `account.refresh` | `enabled`, refresh intervals, concurrency, on-demand interval |
| `account.selection` | `max_inflight` |
| `cache.local` | image/video cache limits |

Media output formats:

| Setting | Values |
| :-- | :-- |
| `features.image_format` | `grok_url`, `local_url`, `grok_md`, `local_md`, `base64` |
| `features.video_format` | `grok_url`, `local_url`, `grok_html`, `local_html` |
| `features.imagine_public_image_proxy` | If `true`, Imagine public images are downloaded and returned through local proxy URLs |

## Development

```bash
uv run --frozen python -m unittest tests.test_release_smoke tests.test_console_reasoning_effort
```

Model registry check:

```bash
uv run --frozen python -c 'from app.control.model.registry import list_enabled; print([m.model_name for m in list_enabled()])'
```

## License

MIT. See [LICENSE](../LICENSE).

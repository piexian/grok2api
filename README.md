<h1 align="center">Grok2API</h1>

<p align="center">
  <strong>面向当前 grok.com / console.x.ai 的 Grok OpenAI 兼容网关</strong>
</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white"></a>
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white"></a>
  <a href="pyproject.toml"><img alt="Version" src="https://img.shields.io/badge/version-2.0.12-111827"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
  <a href="docs/README.en.md"><img alt="English" src="https://img.shields.io/badge/English-2563EB?logo=bookstack&logoColor=white"></a>
</p>

> [!IMPORTANT]
> 原上游仓库已经归档并停止维护。

> [!NOTE]
> 本项目仅供学习、研究和自托管网关场景使用。请遵守 xAI / Grok 服务条款及所在地法律法规。账号、Cookie、Cloudflare clearance 与 API key 都属于敏感信息，请自行妥善保管。

## 项目定位

Grok2API 将 Grok Web、console.x.ai、Imagine 和相关媒体接口统一封装成 OpenAI / Anthropic 兼容 API。它适合需要多账号池、统一鉴权、流式输出、图片/视频代理、Admin 管理和 WebUI 的自托管场景。

## 主要能力

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`。
- Anthropic 兼容接口：`/v1/messages`。
- 多账号池：basic / lite / super / heavy 分层选择，支持本地、Redis、MySQL、PostgreSQL 存储。
- 配额与失败反馈：grok.com 文本模型使用上游 quota；console.x.ai 使用独立运行时限速和冷却，不与 grok.com Chat 混算。
- 媒体能力：文生图、图片编辑、文生视频、图生视频、图片/视频本地缓存和代理链接。
- Web 产品：Admin 后台、Web Chat、Masonry 生图页、ChatKit 语音页面。
- 代理与 Cloudflare：支持 direct、单代理、代理池、手动 clearance、FlareSolverr。

## 快速部署

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

| 镜像 | 说明 |
| :-- | :-- |
| `ghcr.io/piexian/grok2api:latest` | 当前 latest，指向 2.0.12 系列 |
| `ghcr.io/piexian/grok2api:2.0.12` | 固定版本标签 |

### Docker Compose

```bash
git clone https://github.com/piexian/grok2api
cd grok2api
cp .env.example .env
docker compose up -d
```

### 本地运行

```bash
git clone https://github.com/piexian/grok2api
cd grok2api
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

首次启动后至少配置：

| 配置项 | 用途 |
| :-- | :-- |
| `app.api_key` | `/v1/*` API 鉴权 |
| `app.app_key` | Admin 后台密码 |
| `app.app_url` | 本地图片/视频代理链接的外部访问地址 |
| `app.webui_enabled` / `app.webui_key` | WebUI 开关和访问密码 |

## 页面入口

| 页面 | 路径 |
| :-- | :-- |
| Admin 登录 | `/admin/login` |
| 账号管理 | `/admin/account` |
| 配置管理 | `/admin/config` |
| 缓存管理 | `/admin/cache` |
| WebUI 登录 | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry 生图 | `/webui/masonry` |
| ChatKit 语音 | `/webui/chatkit` |

## 模型表

可通过 `GET /v1/models` 查看当前可用模型。接口会按账号池过滤；没有 super 账号时不会返回 super+ 模型。

### Chat

| 模型名 | 上游路径 | mode / model | 账号层级 |
| :-- | :-- | :-- | :-- |
| `grok-4.3-fast` | grok.com app-chat | `fast` | basic |
| `grok-4.3-auto` | grok.com app-chat | `auto` | super+ |
| `grok-4.3-expert` | grok.com app-chat | `expert` | super+ |
| `grok-4.3-heavy` | grok.com app-chat | `heavy` | heavy |
| `grok-4.3` | console.x.ai `/v1/responses` | `grok-4.3` | basic |
| `grok-build-0.1` | console.x.ai `/v1/responses` | `grok-build-0.1` | basic |
| `grok-4.20-0309-non-reasoning` | console.x.ai `/v1/responses` | 同名 | basic |
| `grok-4.20-0309-reasoning` | console.x.ai `/v1/responses` | 同名 | basic |
| `grok-4.20-multi-agent-0309` | console.x.ai `/v1/responses` | 同名 | basic |

console.x.ai 说明：

- 该路径使用 grok.com SSO Cookie，但限速来自 console.x.ai。
- 不同 console 模型的请求参数会按上游兼容性自动处理。
- 429 会按 console 模型独立冷却，不与 grok.com Chat 混算。

### Image

| 模型名 | 上游路径 | 账号层级 | 备注 |
| :-- | :-- | :-- | :-- |
| `grok-imagine-image-lite` | grok.com app-chat | basic | 无精确画幅控制 |
| `grok-imagine-image` | Imagine WebSocket | super+ | speed mode |
| `grok-imagine-image-pro` | Imagine WebSocket | super+ | quality/pro mode |

### Image Edit

| 模型名 | 上游路径 | 账号层级 |
| :-- | :-- | :-- |
| `grok-imagine-image-edit` | grok.com app-chat edit flow | super+ |

### Video

| 模型名 | 上游路径 | 账号层级 |
| :-- | :-- | :-- |
| `grok-imagine-video` | grok.com media API | super+ |

## API 示例

以下示例默认服务地址为 `http://localhost:8000`。

### 列出模型

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
      {"role": "user", "content": "用三句话解释量子隧穿"}
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
    "input": "搜索并总结今天的 AI 新闻",
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
      {"role": "user", "content": "写一个 FastAPI 健康检查示例"}
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
    "prompt": "一只在太空漂浮的猫，电影感",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

图像参数：

| 字段 | 说明 |
| :-- | :-- |
| `model` | `grok-imagine-image-lite`、`grok-imagine-image`、`grok-imagine-image-pro` |
| `n` | lite 为 `1-4`，其他图像模型为 `1-10` |
| `size` | `1280x720`、`720x1280`、`1792x1024`、`1024x1792`、`1024x1024` |
| `response_format` | `url` 或 `b64_json` |

### Image Edit

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-image-edit" \
  -F "prompt=把这张图变清晰一些" \
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
  -F "prompt=霓虹雨夜街头，电影感慢镜头追拍" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

查询与下载：

```bash
curl http://localhost:8000/v1/videos/<video_id> \
  -H "Authorization: Bearer $GROK2API_API_KEY"

curl -L http://localhost:8000/v1/videos/<video_id>/content \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -o result.mp4
```

视频参数：

| 字段 | 说明 |
| :-- | :-- |
| `seconds` | `6`、`10`、`12`、`16`、`20` |
| `size` | `720x1280`、`1280x720`、`1024x1024`、`1024x1792`、`1792x1024` |
| `resolution_name` | `480p` 或 `720p` |
| `preset` | `fun`、`normal`、`spicy`、`custom` |
| `input_reference[]` | 可选图生视频参考图，最多使用前 7 张 |

## 配置

配置来源按优先级合并：

| 来源 | 说明 |
| :-- | :-- |
| 环境变量 | 启动时注入，支持 `GROK_` 前缀覆盖 |
| `${DATA_DIR}/config.toml` | Admin 保存后的运行时配置 |
| `config.defaults.toml` | 首次初始化模板 |

常用环境变量：

| 变量 | 默认值 | 说明 |
| :-- | :-- | :-- |
| `SERVER_HOST` | `0.0.0.0` | 监听地址 |
| `SERVER_PORT` | `8000` | 监听端口 |
| `DATA_DIR` | `./data` | 账号库、配置和本地媒体缓存目录 |
| `LOG_DIR` | `./logs` | 日志目录 |
| `ACCOUNT_STORAGE` | `local` | `local`、`redis`、`mysql`、`postgresql` |
| `GROK_APP_API_KEY` | 空 | 覆盖 `app.api_key` |
| `GROK_APP_APP_KEY` | `grok2api` | 覆盖 Admin 密码 |
| `GROK_APP_APP_URL` | 空 | 外部访问地址 |

关键配置分组：

| 分组 | 关键项 |
| :-- | :-- |
| `app` | `app_key`、`app_url`、`api_key`、`webui_enabled`、`webui_key` |
| `features` | `stream`、`thinking`、`memory`、`show_search_sources`、`image_format`、`video_format` |
| `proxy.egress` | `mode`、`proxy_url`、`proxy_pool`、`resource_proxy_url`、`skip_ssl_verify` |
| `proxy.clearance` | `mode`、`cf_cookies`、`user_agent`、`browser`、`flaresolverr_url` |
| `account.refresh` | `enabled`、刷新间隔、并发、on-demand 最小间隔 |
| `account.selection` | `max_inflight` |
| `cache.local` | 图片/视频本地缓存上限 |

图片和视频返回格式：

| 配置项 | 可选值 |
| :-- | :-- |
| `features.image_format` | `grok_url`、`local_url`、`grok_md`、`local_md`、`base64` |
| `features.video_format` | `grok_url`、`local_url`、`grok_html`、`local_html` |
| `features.imagine_public_image_proxy` | `true` 时将 Imagine public 图下载到本地代理 |

## 开发

```bash
uv run --frozen python -m unittest tests.test_release_smoke tests.test_console_reasoning_effort
```

常用检查：

```bash
uv run --frozen python -c 'from app.control.model.registry import list_enabled; print([m.model_name for m in list_enabled()])'
```

## License

MIT. See [LICENSE](LICENSE).

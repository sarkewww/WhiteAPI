# WhiteAPI

聚合音乐 API — 用一个统一接口对接多个音乐平台（网易云 / QQ 音乐 / 酷狗 / 酷我 / Spotify），提供搜索、播放直链、歌词、歌单、专辑能力，方便第三方项目集成。

> 目前为 Python 原型阶段，后续考虑迁移到 Node.js。

## 特性

- **统一接口**：不同平台的搜索/直链/歌词都收敛到同一套 REST API
- **Provider 插件化**：新增平台只需实现 `MusicProvider` 基类并注册
- **并发聚合**：`provider=all` 时并发查询所有已注册平台
- **异步高性能**：FastAPI + httpx.AsyncClient

## 支持的平台

| name | 平台 | 说明 |
|------|------|------|
| `netease` | 网易云音乐 | eapi 加密，支持无损/母带等音质 |
| `qq` | QQ音乐 | musicu.fcg 接口，支持音质降级 |
| `kugou` | 酷狗音乐 | gateway 签名接口，支持 128/320/flac/Hi-Res 降级 |
| `kuwo` | 酷我音乐 | 搜索/直链/歌词，支持 128/192/320 音质 |
| `spotify` | Spotify | 官方 Web API，需配置 client-credentials；仅提供 30s 试听 |

## 快速开始

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5000
```

访问交互文档：http://localhost:5000/docs

## 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 + 已注册平台 |
| GET | `/providers` | 平台列表 |
| GET | `/search` | 统一搜索 |
| GET | `/song/url` | 获取播放/下载直链 |
| GET | `/song/detail` | 歌曲详情 |
| GET | `/lyric` | 歌词（LRC，含翻译） |
| GET | `/playlist` | 歌单详情 |
| GET | `/album` | 专辑详情 |

## 接口详情

### 1. 搜索 `/search`

搜索关键词，跨平台聚合或指定单平台。

```
GET /search?keyword=想去海边&provider=all&limit=10&search_type=1
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| keyword | string | 必填 | 搜索关键词 |
| provider | string | `all` | `all`=并发所有平台，或指定 `netease`/`qq`/`kugou`/`kuwo`/`spotify` |
| limit | int | 10 | 每平台返回数量 (1-100) |
| search_type | int | 1 | 1=单曲 10=专辑 100=歌手 1000=歌单 |
| offset | int | 0 | 分页偏移 |

```json
{
  "status": 200, "success": true, "message": "ok",
  "data": {
    "keyword": "想去海边", "total": 2,
    "songs": [
      {"source": "netease", "song_id": "1330348068", "name": "想去海边",
       "artists": "夏日入侵企画", "album": "夏日入侵企画", "duration": 255000,
       "pic_url": "https://p3.music.126.net/...jpg?param=300y300", "playable": true}
    ]
  }
}
```

### 2. 播放/下载直链 `/song/url`

```
GET /song/url?song_id=1330348068&provider=netease&level=lossless
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| song_id | string | 必填 | 歌曲 ID（来自搜索结果的 song_id） |
| provider | string | 必填 | 平台名 |
| level | string | `lossless` | 音质：`standard`/`exhigh`/`lossless`/`hires` 等 |

```json
{
  "status": 200, "success": true, "message": "ok",
  "data": {
    "source": "netease", "song_id": "1330348068",
    "url": "http://m801.music.126.net/...", "level": "standard",
    "quality_name": "标准", "br": 128000, "size": 4096000,
    "type": "mp3", "expired": false
  }
}
```

> 注意：网易云 VIP 歌曲直链需登录 cookie（见下方 Cookie 配置）。

### 3. 歌曲详情 `/song/detail`

```
GET /song/detail?song_id=1330348068&provider=netease
```

### 4. 歌词 `/lyric`

```
GET /lyric?song_id=1330348068&provider=netease
```

返回 `lyric`（原文 LRC）与 `tlyric`（翻译 LRC）。

### 5. 歌单 `/playlist`

```
GET /playlist?playlist_id=3778678&provider=netease
```

### 6. 专辑 `/album`

```
GET /album?album_id=32311&provider=netease
```

## Cookie 配置（可选）

VIP/收费歌曲直链需要登录态。创建 provider 时传入 cookie 字符串即可：

```python
from app.providers import get_provider
p = get_provider("netease", cookies="MUSIC_U=xxx;os=pc;appver=8.9.70;")
```

- 网易云关键 cookie：`MUSIC_U`
- QQ 音乐关键 cookie：`p_uin` / `p_skey`
- 酷狗关键 cookie：`token` / `userid`（VIP 无损需要）
- 酷我关键 cookie：`Hm_Iuvt_*`（部分接口需签名）

## Spotify 配置

Spotify 走官方 Web API，需要 OAuth2 client-credentials。设置环境变量后生效：

```bash
export SPOTIFY_CLIENT_ID=xxx
export SPOTIFY_CLIENT_SECRET=xxx
```

> 限制：Spotify 公开 API **不提供完整播放直链**，`/song/url` 只返回 30 秒 `preview_url`（无预览时 `expired=true`）。未配置凭证时 provider 仍可注册，但调用会抛错。

## 开发

```bash
# 新增平台 provider
# 1. 创建 app/providers/<name>/provider.py
# 2. 继承 MusicProvider 实现统一方法
# 3. 用 @register_provider 装饰器注册
```

```python
from app.providers import register_provider
from app.providers.base import MusicProvider

@register_provider
class KuGouProvider(MusicProvider):
    name = "kugou"
    display_name = "酷狗音乐"
    # 实现 search / get_song_url / get_song_detail / get_lyric ...
```

## License

MIT

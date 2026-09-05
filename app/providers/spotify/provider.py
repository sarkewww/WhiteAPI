"""Spotify Provider

直接以 httpx 实现 Spotify Web API (https://api.spotify.com/v1)，端点知识移植自
spotipy 参考库（docs/SpotifyApi），不引入 spotipy 依赖。

认证：OAuth2 client-credentials 流程，需要环境变量
    SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
access token 缓存在内存中，过期前 60 秒自动刷新（与 spotipy 一致）。
缺少凭证时在调用时抛 RuntimeError，provider 的导入与注册不受影响。

平台限制：
    - 公共 API 不提供完整播放直链，仅有 30 秒试听 preview_url；
      level/quality 参数（standard/exhigh/lossless/hires）接受但忽略。
    - 歌词接口 /v1/tracks/{id}/lyrics 对大量曲目返回 404，此时返回空 Lyric。
"""

from __future__ import annotations

import base64
import html
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import register_provider
from app.providers.base import MusicProvider


class SpotifyAPIException(Exception):
    """Spotify API 异常"""


NO_CREDENTIALS_ERROR = "Spotify 需要配置 SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET 环境变量"

# ID / URI / URL → base62 ID（移植自 spotipy 的 _regex_spotify_uri / _regex_spotify_url）
_RE_URI = re.compile(
    r"^spotify:(?:track|artist|album|playlist|show|episode|audiobook):([0-9A-Za-z]+)")
_RE_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[A-Za-z]{2}/)?"
    r"(?:track|artist|album|playlist|user|show|episode|audiobook)/([0-9A-Za-z]+)")


@register_provider
class SpotifyProvider(MusicProvider):
    """Spotify Provider 实现"""

    name = "spotify"
    display_name = "Spotify"

    API_BASE = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    # 统一 search_type → Spotify type：1=单曲 10=专辑 100=歌手 1000=歌单
    SEARCH_TYPE_MAP = {1: "track", 10: "album", 100: "artist", 1000: "playlist"}

    def __init__(self, cookies: str = ""):
        # cookies 未使用：Spotify 走 OAuth2 client-credentials，不用登录态；为兼容基类签名而保留
        super().__init__(cookies)
        self._client: Optional[httpx.AsyncClient] = None
        self._access_token = ""
        self._token_expires_at = 0.0

    @property
    def client(self) -> httpx.AsyncClient:
        """惰性创建异步客户端"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self):
        """关闭异步客户端"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # 认证与基础请求
    # ------------------------------------------------------------------ #

    @staticmethod
    def _credentials() -> tuple[str, str]:
        """从环境变量读取凭证，缺失时抛 RuntimeError"""
        client_id = (os.environ.get("SPOTIFY_CLIENT_ID") or "").strip()
        client_secret = (os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()
        if not client_id or not client_secret:
            raise RuntimeError(NO_CREDENTIALS_ERROR)
        return client_id, client_secret

    async def _get_access_token(self) -> str:
        """client-credentials 换取 token：内存缓存，过期前 60 秒自动刷新"""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        client_id, client_secret = self._credentials()
        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
        try:
            resp = await self.client.post(
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as e:
            raise SpotifyAPIException(f"Spotify 认证失败: {e}") from e
        token = payload.get("access_token") or ""
        if not token:
            raise SpotifyAPIException("Spotify 认证失败: 响应缺少 access_token")
        self._access_token = token
        self._token_expires_at = time.time() + int(payload.get("expires_in") or 3600)
        return token

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET /v1 端点（path 也可为完整分页 URL）；404 返回 None，其余错误抛异常"""
        token = await self._get_access_token()
        url = path if path.startswith("http") else f"{self.API_BASE}/{path.lstrip('/')}"
        try:
            resp = await self.client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            raise SpotifyAPIException(f"Spotify 请求失败: {e}") from e

    async def _get_all_items(self, path: str, params: dict, max_items: int) -> list[dict]:
        """跟随分页 next 链接收集 items（最多 max_items 条）"""
        items: list[dict] = []
        page = await self._get(path, params=params)
        while page:
            items.extend(page.get("items") or [])
            nxt = page.get("next")
            if not nxt or len(items) >= max_items:
                break
            page = await self._get(nxt)
        return items[:max_items]

    # ------------------------------------------------------------------ #
    # 数据映射辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_id(value: str) -> str:
        """接受裸 ID / spotify:track:ID URI / open.spotify.com URL，返回 base62 ID"""
        value = (value or "").strip()
        m = _RE_URI.match(value) or _RE_URL.match(value)
        return m.group(1) if m else value

    @staticmethod
    def _artist_names(artists: list) -> str:
        """artists 数组 → 'A/B/C' 字符串"""
        return "/".join(a.get("name", "") for a in artists or [] if a.get("name"))

    @staticmethod
    def _largest_image(images: list) -> str:
        """images 数组取面积最大的一张封面 URL"""
        best_url, best_area = "", -1
        for im in images or []:
            url = im.get("url") or ""
            area = int(im.get("width") or 0) * int(im.get("height") or 0)
            if url and area > best_area:
                best_url, best_area = url, area
        return best_url

    @staticmethod
    def _release_to_ms(release_date: str) -> int:
        """release_date（YYYY / YYYY-MM / YYYY-MM-DD）→ UTC 毫秒时间戳"""
        if not release_date:
            return 0
        try:
            parts = [int(p) for p in release_date.split("-")[:3]]
            parts += [1] * (3 - len(parts))
            dt = datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
        except ValueError:
            return 0
        return int(dt.timestamp() * 1000)

    def _build_track_song(self, raw: dict, fallback_pic: str = "",
                          album_name: str = "") -> Song:
        """Spotify track 对象 → 统一 Song（duration_ms 已是毫秒，直接沿用曲目时长）"""
        album = raw.get("album") or {}
        return Song(
            source=self.name,
            song_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            artists=self._artist_names(raw.get("artists")),
            album=album.get("name", "") or album_name,
            duration=int(raw.get("duration_ms") or 0),
            pic_url=self._largest_image(album.get("images")) or fallback_pic,
            playable=bool(raw.get("is_playable", True)),
        )

    # ------------------------------------------------------------------ #
    # MusicProvider 抽象方法
    # ------------------------------------------------------------------ #

    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """统一搜索：GET /v1/search?type=track|album|playlist|artist

        search_type 映射：1=单曲→track，10=专辑→album，1000=歌单→playlist，
        100=歌手→artist。非单曲结果统一映射为 Song（song_id 为对应实体 ID，
        专辑 duration 为专辑总时长，歌手/歌单为 0），便于聚合列表展示。
        Spotify 单次 limit 上限 50，超出自动截断。
        """
        sp_type = self.SEARCH_TYPE_MAP.get(int(search_type), "track")
        params = {
            "q": keyword,
            "type": sp_type,
            "limit": min(max(int(limit), 1), 50),
            "offset": max(int(offset), 0),
        }
        data = await self._get("search", params=params) or {}
        page = data.get(sp_type + "s") or {}
        songs: list[Song] = []
        for item in page.get("items") or []:
            if sp_type == "track":
                songs.append(self._build_track_song(item))
            elif sp_type == "album":
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    artists=self._artist_names(item.get("artists")),
                    album=item.get("name", ""),
                    duration=int(item.get("total_duration_ms") or 0),
                    pic_url=self._largest_image(item.get("images")),
                    playable=True,
                ))
            elif sp_type == "playlist":
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    artists=(item.get("owner") or {}).get("display_name") or "",
                    album="",
                    duration=0,
                    pic_url=self._largest_image(item.get("images")),
                    playable=True,
                ))
            else:  # artist
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    artists=item.get("name", ""),
                    album="",
                    duration=0,
                    pic_url=self._largest_image(item.get("images")),
                    playable=True,
                ))
        return SearchResult(source=self.name, keyword=keyword,
                            total=int(page.get("total") or len(songs)), songs=songs)

    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放链接：GET /v1/tracks/{id}

        Spotify 公共 API 不提供完整音频流，仅有 30 秒试听 preview_url；
        level（standard/exhigh/lossless/hires）与 quality 参数接受但忽略，
        绝不伪造完整直链。无试听时返回 url=""、expired=True。
        """
        tid = self._extract_id(song_id)
        data = await self._get(f"tracks/{tid}")
        if not data:
            raise SpotifyAPIException(f"Spotify 歌曲不存在: {song_id}")
        preview = data.get("preview_url") or ""
        if preview:
            return SongUrl(source=self.name, song_id=tid, url=preview,
                           level="preview", quality_name="试听",
                           br=0, size=0, type="mp3", expired=False)
        return SongUrl(source=self.name, song_id=tid, url="",
                       level="preview",
                       quality_name="无试听片段，完整播放需要 Spotify Premium",
                       br=0, size=0, type="mp3", expired=True)

    async def get_song_detail(self, song_id: str) -> Song:
        """歌曲详情：GET /v1/tracks/{id}"""
        tid = self._extract_id(song_id)
        data = await self._get(f"tracks/{tid}")
        if not data:
            raise SpotifyAPIException(f"Spotify 歌曲不存在: {song_id}")
        return self._build_track_song(data)

    async def get_lyric(self, song_id: str) -> Lyric:
        """歌词：GET /v1/tracks/{id}/lyrics

        大量曲目无歌词（404），此时返回空 Lyric 而不抛异常。
        LINE_SYNC 行转 LRC 时间戳；UNSYNCED 返回纯文本行；
        tlyric 恒为空（公共 API 不提供翻译歌词）。
        """
        tid = self._extract_id(song_id)
        data = await self._get(f"tracks/{tid}/lyrics")
        if not data:
            return Lyric(source=self.name, song_id=tid, lyric="", tlyric="")
        obj = data.get("lyrics") or {}
        if obj.get("syncType") in ("UNAVAILABLE", "NO_LYRICS"):
            return Lyric(source=self.name, song_id=tid, lyric="", tlyric="")
        return Lyric(source=self.name, song_id=tid,
                     lyric=self._lines_to_lrc(obj.get("lines")), tlyric="")

    @staticmethod
    def _lines_to_lrc(lines: list) -> str:
        """Spotify lyrics.lines → LRC 文本；无时间戳的行按纯文本输出"""
        out: list[str] = []
        for line in lines or []:
            words = f"{line.get('prefix') or ''}{line.get('words') or ''}".strip()
            if not words:
                continue
            try:
                ms = int(line.get("startTimeMilliseconds"))
            except (TypeError, ValueError):
                out.append(words)
                continue
            out.append(f"[{ms // 60000:02d}:{ms % 60000 // 1000:02d}."
                       f"{ms % 1000 // 10:02d}]{words}")
        return "\n".join(out)

    # ------------------------------------------------------------------ #
    # 歌单 / 专辑（Spotify 两者均支持）
    # ------------------------------------------------------------------ #

    async def get_playlist(self, playlist_id: str, limit: int = 50) -> Playlist:
        """歌单详情：GET /v1/playlists/{id} + /items（分页，limit 为最多取回曲目数）"""
        pid = self._extract_id(playlist_id)
        data = await self._get(f"playlists/{pid}")
        if not data:
            raise SpotifyAPIException(f"Spotify 歌单不存在: {playlist_id}")
        meta = data.get("tracks") or {}
        total = int(meta.get("total") or 0)
        cap = min(max(int(limit), 1), max(total, 1))
        raw_items = await self._get_all_items(
            f"playlists/{pid}/items",
            {"limit": min(100, cap), "additional_types": "track"}, cap)
        tracks = [self._build_track_song(it["track"])
                  for it in raw_items if it.get("track")]
        desc = re.sub(r"<[^>]+>", "", html.unescape(data.get("description") or ""))
        return Playlist(
            source=self.name,
            playlist_id=str(data.get("id", pid)),
            name=data.get("name", ""),
            cover_url=self._largest_image(data.get("images")),
            creator=(data.get("owner") or {}).get("display_name") or "",
            description=desc.strip(),
            tracks=tracks,
        )

    async def get_album(self, album_id: str) -> Album:
        """专辑详情：GET /v1/albums/{id}（内嵌曲目超过 50 首时补拉 /tracks 分页）"""
        aid = self._extract_id(album_id)
        data = await self._get(f"albums/{aid}")
        if not data:
            raise SpotifyAPIException(f"Spotify 专辑不存在: {album_id}")
        pic = self._largest_image(data.get("images"))
        meta = data.get("tracks") or {}
        total = int(meta.get("total") or 0)
        raw = [t for t in (meta.get("items") or []) if t]
        if total > len(raw):
            raw = await self._get_all_items(
                f"albums/{aid}/tracks", {"limit": 50}, total)
        songs = [self._build_track_song(t, fallback_pic=pic,
                                        album_name=data.get("name", ""))
                 for t in raw if t]
        return Album(
            source=self.name,
            album_id=str(data.get("id", aid)),
            name=data.get("name", ""),
            cover_url=pic,
            artist=self._artist_names(data.get("artists")),
            publish_time=self._release_to_ms(data.get("release_date") or ""),
            songs=songs,
        )

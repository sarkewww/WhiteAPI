"""网易云音乐 Provider

基于 eapi 加密接口实现网易云音乐的搜索、歌曲直链、详情、歌词、
歌单与专辑获取，输出 WhiteAPI 统一数据模型。

实现要点:
- 使用 httpx.AsyncClient 异步请求（替代 requests）
- eapi 加密参数由 crypto.CryptoUtils.encrypt_params 完成
- 歌曲直链支持 standard/exhigh/lossless/hires/sky 等音质等级
"""

from __future__ import annotations

import asyncio
import json
import threading
from random import randrange
from typing import Any, Dict, List, Optional

import httpx

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import register_provider
from app.providers.base import MusicProvider
from app.providers.netease.crypto import CryptoUtils, get_pic_url

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36 Chrome/91.0.4472.164 NeteaseMusicDesktop/2.10.2.200154'
REFERER = 'https://music.163.com/'

# API 地址
SONG_URL_V1 = "https://interface3.music.163.com/eapi/song/enhance/player/url/v1"
SONG_DETAIL_V3 = "https://interface3.music.163.com/api/v3/song/detail"
LYRIC_API = "https://interface3.music.163.com/api/song/lyric"
SEARCH_API = 'https://music.163.com/api/cloudsearch/pc'
PLAYLIST_DETAIL_API = 'https://music.163.com/api/v6/playlist/detail'
ALBUM_DETAIL_API = 'https://music.163.com/api/v1/album/'

# 默认固定 cookie
DEFAULT_COOKIES = {
    "os": "pc",
    "appver": "",
    "osver": "",
    "deviceId": "pyncm!",
}

# 音质等级 -> 中文名
QUALITY_NAMES = {
    "standard": "标准",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
    "sky": "沉浸环绕声",
    "jyeffect": "高清环绕声",
    "jymaster": "超清母带",
    "dolby": "杜比全景声",
}


class NeteaseAPIException(Exception):
    """网易云 API 异常"""


def parse_cookie_string(cookie_string: str) -> Dict[str, str]:
    """解析 cookie 字符串为字典

    兼容用分号(;)或换行(\n)分隔的 cookie 串。
    """
    if not cookie_string or not cookie_string.strip():
        return {}

    cookie_string = cookie_string.strip()
    if ';' in cookie_string:
        pairs = cookie_string.split(';')
    elif '\n' in cookie_string:
        pairs = cookie_string.split('\n')
    else:
        pairs = [cookie_string]

    cookies: Dict[str, str] = {}
    for pair in pairs:
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        key, value = pair.split('=', 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            cookies[key] = value
    return cookies


@register_provider
class NeteaseProvider(MusicProvider):
    """网易云音乐 Provider"""

    name = "netease"
    display_name = "网易云音乐"

    # 共享异步客户端（懒加载，线程安全）
    _client: Optional[httpx.AsyncClient] = None
    _client_lock = threading.Lock()

    def __init__(self, cookies: str = ""):
        super().__init__(cookies=cookies)
        self._cookies = parse_cookie_string(cookies)
        self._request_cookies: Dict[str, str] = {**DEFAULT_COOKIES, **self._cookies}

    # ---------- 内部工具 ----------

    def _get_client(self) -> httpx.AsyncClient:
        """懒加载共享 AsyncClient（不绑定 cookie，cookie 每次请求单独传）"""
        if NeteaseProvider._client is None:
            with NeteaseProvider._client_lock:
                if NeteaseProvider._client is None:
                    NeteaseProvider._client = httpx.AsyncClient(
                        timeout=30,
                        headers={"User-Agent": USER_AGENT, "Referer": REFERER},
                    )
        return NeteaseProvider._client

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise NeteaseAPIException(f"解析响应数据失败: {e}")

    @staticmethod
    def _check_code(result: Dict[str, Any], action: str) -> None:
        if result.get("code") != 200:
            raise NeteaseAPIException(
                f"{action}失败: {result.get('message', '未知错误')}"
            )

    async def _post_form(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """POST form 表单请求并解析 JSON"""
        try:
            resp = await self._get_client().post(url, data=data, cookies=self._request_cookies)
            resp.raise_for_status()
            return self._parse_json(resp.text)
        except httpx.HTTPError as e:
            raise NeteaseAPIException(f"HTTP请求失败: {e}")

    async def _post_eapi(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST eapi 加密请求并解析 JSON"""
        params = CryptoUtils.encrypt_params(url, payload)
        try:
            resp = await self._get_client().post(url, data={"params": params}, cookies=self._request_cookies)
            resp.raise_for_status()
            return self._parse_json(resp.text)
        except httpx.HTTPError as e:
            raise NeteaseAPIException(f"HTTP请求失败: {e}")

    @staticmethod
    def _song_from_raw(raw: Dict[str, Any]) -> Song:
        """把网易云原始歌曲数据转换为统一 Song 模型"""
        artists = "/".join(a.get("name", "") for a in (raw.get("ar") or []))
        album = raw.get("al") or {}
        pic = album.get("picUrl") or ""
        if not pic and album.get("pic"):
            pic = get_pic_url(album.get("pic"))
        return Song(
            source="netease",
            song_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            artists=artists,
            album=album.get("name", ""),
            duration=int(raw.get("dt", 0) or 0),
            pic_url=pic,
            playable=True,
        )

    @staticmethod
    def _search_item_to_song(item: Dict[str, Any], search_type: int) -> Song:
        """把不同类型搜索结果的原始项转换为统一 Song 模型"""
        search_type = str(search_type)
        if search_type == "1":  # 单曲
            return NeteaseProvider._song_from_raw(item)
        elif search_type == "10":  # 专辑
            artist = item.get("artist") or {}
            return Song(
                source="netease",
                song_id=str(item.get("id", "")),
                name=item.get("name", ""),
                artists=artist.get("name", ""),
                album=item.get("company", ""),
                pic_url=item.get("picUrl") or item.get("pic") or "",
            )
        elif search_type == "100":  # 歌手
            alias = item.get("alias") or []
            return Song(
                source="netease",
                song_id=str(item.get("id", "")),
                name=item.get("name", ""),
                artists="/".join(alias),
                album=f"作品 {item.get('albumSize', 0)} 张",
                pic_url=item.get("img1v1Url") or item.get("picUrl") or "",
            )
        elif search_type == "1000":  # 歌单
            creator = item.get("creator") or {}
            return Song(
                source="netease",
                song_id=str(item.get("id", "")),
                name=item.get("name", ""),
                artists=creator.get("nickname", ""),
                album=f"{item.get('trackCount', 0)} 首",
                pic_url=item.get("coverImgUrl", ""),
            )
        return Song(
            source="netease",
            song_id=str(item.get("id", "")),
            name=item.get("name", ""),
        )

    # ---------- 抽象方法实现 ----------

    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """统一搜索"""
        data = {"s": keyword, "type": search_type, "limit": limit, "offset": offset}
        result = await self._post_form(SEARCH_API, data)
        self._check_code(result, "搜索")
        raw = result.get("result", {})

        search_type_s = str(search_type)
        if search_type_s == "1":
            items = raw.get("songs", [])
            total = raw.get("songCount", len(items))
        elif search_type_s == "10":
            items = raw.get("albums", [])
            total = raw.get("albumCount", len(items))
        elif search_type_s == "100":
            items = raw.get("artists", [])
            total = raw.get("artistCount", len(items))
        elif search_type_s == "1000":
            items = raw.get("playlists", [])
            total = raw.get("playlistCount", len(items))
        else:
            items = []
            total = 0

        songs = [self._search_item_to_song(item, search_type) for item in items]
        return SearchResult(
            source="netease", keyword=keyword, total=total, songs=songs
        )

    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放/下载直链"""
        config = {**DEFAULT_COOKIES, "requestId": str(randrange(20000000, 30000000))}
        payload: Dict[str, Any] = {
            "ids": [int(song_id)],
            "level": level,
            "encodeType": "flac",
            "header": json.dumps(config),
        }
        if level == "sky":
            payload["immerseType"] = "c51"

        result = await self._post_eapi(SONG_URL_V1, payload)
        self._check_code(result, "获取歌曲URL")

        data_list = result.get("data") or []
        if not data_list:
            raise NeteaseAPIException("获取歌曲URL失败: 无数据")

        item = data_list[0]
        url = item.get("url") or ""
        actual_level = item.get("level") or level
        return SongUrl(
            source="netease",
            song_id=str(song_id),
            url=url,
            level=actual_level,
            quality_name=QUALITY_NAMES.get(actual_level, ""),
            br=int(item.get("br", 0) or 0),
            size=int(item.get("size", 0) or 0),
            type=item.get("type") or "mp3",
            expired=not bool(url),
        )

    async def get_song_detail(self, song_id: str) -> Song:
        """获取歌曲详情"""
        data = {"c": json.dumps([{"id": int(song_id), "v": 0}])}
        result = await self._post_form(SONG_DETAIL_V3, data)
        self._check_code(result, "获取歌曲详情")
        songs = result.get("songs") or []
        if not songs:
            raise NeteaseAPIException("获取歌曲详情失败: 未找到歌曲")
        return self._song_from_raw(songs[0])

    async def get_lyric(self, song_id: str) -> Lyric:
        """获取歌词"""
        data = {
            "id": song_id,
            "cp": "false",
            "tv": "0",
            "lv": "0",
            "rv": "0",
            "kv": "0",
            "yv": "0",
            "ytv": "0",
            "yrv": "0",
        }
        result = await self._post_form(LYRIC_API, data)
        self._check_code(result, "获取歌词")
        lrc = result.get("lrc") or {}
        tlyric = result.get("tlyric") or {}
        return Lyric(
            source="netease",
            song_id=str(song_id),
            lyric=lrc.get("lyric", ""),
            tlyric=tlyric.get("lyric", ""),
        )

    async def get_playlist(self, playlist_id: str) -> Playlist:
        """获取歌单详情"""
        result = await self._post_form(PLAYLIST_DETAIL_API, {"id": int(playlist_id)})
        self._check_code(result, "获取歌单详情")

        playlist = result.get("playlist", {})
        creator = playlist.get("creator") or {}
        track_ids = [str(t["id"]) for t in playlist.get("trackIds", [])]

        # 分批（每批100）并发获取歌曲详情
        songs: List[Song] = []
        if track_ids:
            batches = [track_ids[i:i + 100] for i in range(0, len(track_ids), 100)]

            async def _fetch_batch(batch: List[str]) -> List[Song]:
                try:
                    data = {
                        "c": json.dumps(
                            [{"id": int(sid), "v": 0} for sid in batch]
                        )
                    }
                    resp = await self._post_form(SONG_DETAIL_V3, data)
                    return [self._song_from_raw(s) for s in resp.get("songs", [])]
                except NeteaseAPIException:
                    return []

            batch_results = await asyncio.gather(*[_fetch_batch(b) for b in batches])
            song_map: Dict[str, Song] = {}
            for batch in batch_results:
                for s in batch:
                    song_map[s.song_id] = s
            # 按歌单原始顺序排列
            songs = [song_map[sid] for sid in track_ids if sid in song_map]

        return Playlist(
            source="netease",
            playlist_id=str(playlist_id),
            name=playlist.get("name", ""),
            cover_url=playlist.get("coverImgUrl", ""),
            creator=creator.get("nickname", ""),
            description=playlist.get("description", ""),
            tracks=songs,
        )

    async def get_album(self, album_id: str) -> Album:
        """获取专辑详情"""
        url = f"{ALBUM_DETAIL_API}{album_id}"
        try:
            resp = await self._get_client().get(url, cookies=self._request_cookies)
            resp.raise_for_status()
            result = self._parse_json(resp.text)
        except httpx.HTTPError as e:
            raise NeteaseAPIException(f"HTTP请求失败: {e}")

        self._check_code(result, "获取专辑详情")
        album = result.get("album", {})
        artist = album.get("artist") or {}
        songs = [self._song_from_raw(s) for s in result.get("songs", [])]

        cover = get_pic_url(album.get("pic")) or album.get("picUrl", "")
        return Album(
            source="netease",
            album_id=str(album_id),
            name=album.get("name", ""),
            cover_url=cover,
            artist=artist.get("name", ""),
            publish_time=int(album.get("publishTime", 0) or 0),
            songs=songs,
        )

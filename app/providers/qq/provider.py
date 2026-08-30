"""QQ音乐 Provider

基于 QQ 音乐官方接口 (musicu.fcg / cgi-bin) 实现，移植自 Netease_url 项目。
使用 httpx.AsyncClient 异步请求。
"""

from __future__ import annotations

import base64
import json
import random
import re
from typing import Optional

import httpx

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import register_provider
from app.providers.base import MusicProvider


class QQAPIException(Exception):
    """QQ音乐 API 异常"""


@register_provider
class QQProvider(MusicProvider):
    """QQ音乐 Provider 实现"""

    name = "qq"
    display_name = "QQ音乐"

    # 音质降级顺序：master → atmos_2 → hires → flac → 320 → 128
    DEGRADE_ORDER = ["master", "atmos_2", "hires", "flac", "320", "128"]

    # 统一 level 参数 → QQ 内部音质代码
    LEVEL_MAP = {
        "standard": "128",
        "high": "320",
        "exhigh": "320",
        "lossless": "flac",
        "flac": "flac",
        "hires": "hires",
        "master": "master",
        "sky": "atmos_2",
        "jyeffect": "atmos_51",
        "jymaster": "master",
        "dolby": "dolby",
    }

    # 中文音质名 → QQ 内部音质代码
    QUALITY_MAP = {
        "标准": "128",
        "HQ高品质": "320",
        "SQ无损品质": "flac",
        "臻品母带3.0": "master",
        "臻品全景声2.0": "atmos_2",
        "臻品音质2.0": "atmos_51",
        "Hi-Res": "hires",
        "OGG极致": "ogg_640",
        "OGG高品质": "ogg_320",
        "OGG标准": "ogg_192",
        "AAC高音质": "aac_256",
        "AAC高品质": "aac_192",
        "AAC标准": "aac_128",
        "APE无损": "ape",
        "DTS环绕声": "dts",
        "杜比全景声": "dolby",
    }

    REVERSE_QUALITY_MAP = {
        "128": "标准",
        "320": "HQ高品质",
        "flac": "SQ无损品质",
        "master": "臻品母带3.0",
        "atmos_2": "臻品全景声2.0",
        "atmos_51": "臻品音质2.0",
        "hires": "Hi-Res",
        "ogg_640": "OGG极致",
        "ogg_320": "OGG高品质",
        "ogg_192": "OGG标准",
        "aac_256": "AAC高音质",
        "aac_192": "AAC高品质",
        "aac_128": "AAC标准",
        "aac_96": "AAC标准",
        "ape": "APE无损",
        "dts": "DTS环绕声",
        "dolby": "杜比全景声",
    }

    # 音质代码 → 文件名规则（前缀 + 扩展名 + 码率说明）
    FILE_CONFIG = {
        "128": {"s": "M500", "e": ".mp3", "bitrate": "128kbps"},
        "320": {"s": "M800", "e": ".mp3", "bitrate": "320kbps"},
        "flac": {"s": "F000", "e": ".flac", "bitrate": "FLAC"},
        "master": {"s": "AI00", "e": ".flac", "bitrate": "Master"},
        "atmos_2": {"s": "Q000", "e": ".flac", "bitrate": "Atmos 2"},
        "atmos_51": {"s": "Q001", "e": ".flac", "bitrate": "Atmos 5.1"},
        "ogg_640": {"s": "O801", "e": ".ogg", "bitrate": "640kbps"},
        "ogg_320": {"s": "O800", "e": ".ogg", "bitrate": "320kbps"},
        "ogg_192": {"s": "O600", "e": ".ogg", "bitrate": "192kbps"},
        "ogg_96": {"s": "O400", "e": ".ogg", "bitrate": "96kbps"},
        "aac_320": {"s": "C800", "e": ".m4a", "bitrate": "320kbps"},
        "aac_256": {"s": "C700", "e": ".m4a", "bitrate": "256kbps"},
        "aac_192": {"s": "C600", "e": ".m4a", "bitrate": "192kbps"},
        "aac_128": {"s": "C500", "e": ".m4a", "bitrate": "128kbps"},
        "aac_96": {"s": "C400", "e": ".m4a", "bitrate": "96kbps"},
        "aac_64": {"s": "C300", "e": ".m4a", "bitrate": "64kbps"},
        "aac_48": {"s": "C200", "e": ".m4a", "bitrate": "48kbps"},
        "aac_24": {"s": "C100", "e": ".m4a", "bitrate": "24kbps"},
        "ape": {"s": "A000", "e": ".ape", "bitrate": "APE"},
        "dts": {"s": "D000", "e": ".dts", "bitrate": "DTS"},
        "dolby": {"s": "RS01", "e": ".flac", "bitrate": "Dolby Atmos"},
        "hires": {"s": "SQ00", "e": ".flac", "bitrate": "Hi-Res"},
    }

    def __init__(self, cookies: str = ""):
        super().__init__(cookies)
        self.base_url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
        self.song_url = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
        self.lyric_url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
        self.album_url = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
        self.playlist_url = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_playlist_cp.fcg"
        self.search_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        self.guid = "10000"
        self.uin = "0"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }
        self.cookie_dict: dict = self._parse_cookie_string(cookies)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        """惰性创建异步客户端"""
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self.headers, timeout=30)
        return self._client

    async def close(self):
        """关闭异步客户端"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_cookie_string(cookie_string: str) -> dict:
        """解析 Cookie 字符串为字典（参考 cookie_manager.parse_cookie_string）"""
        if not cookie_string or not cookie_string.strip():
            return {}
        cookies = {}
        sep = ";" if ";" in cookie_string else "\n"
        for pair in cookie_string.split(sep):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value:
                cookies[key] = value
        return cookies

    def _resolve_songmid(self, song_id: str) -> str:
        """从 URL 或 ID 中提取 songmid"""
        if "y.qq.com" in song_id:
            if "/songDetail/" in song_id:
                m = re.search(r"/songDetail/([^/?]+)", song_id)
                if m:
                    return m.group(1)
            if "id=" in song_id:
                m = re.search(r"id=(\w+)", song_id)
                if m:
                    return m.group(1)
        return song_id.strip()

    def _extract_dissid(self, value: str) -> str:
        """提取歌单 ID"""
        m = re.search(r"playlist/(\d+)", value)
        if m:
            return m.group(1)
        m = re.search(r"id=(\d+)", value)
        if m:
            return m.group(1)
        return value.strip()

    def _extract_albummid(self, value: str) -> str:
        """提取专辑 ID"""
        m = re.search(r"albumDetail/(\w+)", value)
        if m:
            return m.group(1)
        m = re.search(r"albummid=(\w+)", value)
        if m:
            return m.group(1)
        return value.strip()

    @staticmethod
    def _decode_base64(value: str) -> str:
        """解码 base64 文本（歌词），失败返回空串"""
        if not value:
            return ""
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _bitrate_to_br(bitrate: str) -> int:
        """从码率描述字符串提取数值，如 '320kbps' -> 320"""
        m = re.search(r"(\d+)", bitrate or "")
        return int(m.group(1)) if m else 0

    def _resolve_quality(self, level: str, quality: str) -> str:
        """将统一 level/中文音质解析为 QQ 内部音质代码"""
        if quality:
            if quality in self.QUALITY_MAP:
                return self.QUALITY_MAP[quality]
            if quality in self.FILE_CONFIG:
                return quality
        if level in self.FILE_CONFIG:
            return level
        if level in self.LEVEL_MAP:
            return self.LEVEL_MAP[level]
        return "flac"

    async def _request(self, url: str, post_fields: Optional[str] = None,
                       params: Optional[dict] = None) -> dict:
        """异步请求封装：POST(form) 或 GET(params)，带 Cookie"""
        headers = dict(self.headers)
        if self.cookie_dict:
            headers["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookie_dict.items()
            )
        try:
            if post_fields is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                resp = await self.client.post(url, content=post_fields, headers=headers)
            else:
                resp = await self.client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            raise QQAPIException(f"QQ音乐请求失败: {e}") from e

    # ------------------------------------------------------------------ #
    # 播放链接
    # ------------------------------------------------------------------ #

    async def _get_music_url(self, songmid: str, file_type: str) -> Optional[dict]:
        """获取单个音质的播放链接（vkey.GetVkeyServer / CgiGetVkey）"""
        if file_type not in self.FILE_CONFIG:
            raise QQAPIException(f"无效音质: {file_type}")
        file_info = self.FILE_CONFIG[file_type]
        file_name = f"{file_info['s']}{songmid}{songmid}{file_info['e']}"

        req_data = {
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": [file_name],
                    "guid": self.guid,
                    "songmid": [songmid],
                    "songtype": [0],
                    "uin": self.uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "loginUin": self.uin,
            "comm": {"uin": self.uin, "format": "json", "ct": 24, "cv": 0},
        }

        data = await self._request(self.base_url, post_fields=json.dumps(req_data))
        mid_info = (data.get("req_1", {}).get("data", {})
                    .get("midurlinfo", [{}]) or [{}])
        purl = (mid_info[0] or {}).get("purl", "")
        if not purl:
            return None

        sips = data.get("req_1", {}).get("data", {}).get("sip", []) or []
        prefix = sips[0] if sips else ""
        if len(sips) > 1:
            prefix = sips[1]
        music_url = (prefix + purl).replace("http://", "https://")
        return {"url": music_url, "bitrate": file_info["bitrate"]}

    async def _degrade_quality(self, songmid: str, quality_code: str):
        """按降级顺序逐级尝试获取播放链接"""
        order = self.DEGRADE_ORDER
        start = order.index(quality_code) if quality_code in order else 0
        for code in order[start:]:
            result = await self._get_music_url(songmid, code)
            if result and result.get("url"):
                return result, code, (code != quality_code)
        return None, None, False

    # ------------------------------------------------------------------ #
    # MusicProvider 抽象方法
    # ------------------------------------------------------------------ #

    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """搜索：1=单曲 10=专辑 2=歌词 1004=MV，其余默认单曲"""
        type_map = {1: 0, 10: 8, 2: 7, 1004: 12}
        t = type_map.get(int(search_type), 0)
        page = offset // max(limit, 1) + 1
        params = {"w": keyword, "p": page, "n": limit, "format": "json", "t": t}

        data = await self._request(self.search_url, params=params)
        if data.get("code") != 0:
            raise QQAPIException(f"QQ搜索失败: {keyword}")

        data_key = {0: "song", 8: "album", 7: "lyric", 12: "mv"}.get(t, "song")
        items = data.get("data", {}).get(data_key, {}).get("list", []) or []

        songs = []
        if t == 0:
            for item in items:
                singers = "/".join(
                    s.get("name", "") for s in item.get("singer", []) or []
                )
                album_mid = item.get("albummid", "")
                pic = (f"https://y.qq.com/music/photo_new/T002R300x300M000"
                       f"{album_mid}.jpg") if album_mid else ""
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("songmid", "")),
                    name=item.get("songname", ""),
                    artists=singers,
                    album=item.get("albumname", ""),
                    duration=(item.get("interval", 0) or 0) * 1000,
                    pic_url=pic,
                    playable=True,
                ))
        elif t == 8:
            for item in items:
                singers = "/".join(
                    s.get("name", "") for s in item.get("singer_list", []) or []
                )
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("albumMID", "")),
                    name=item.get("albumName", ""),
                    artists=singers,
                    album=item.get("singerName", ""),
                    duration=0,
                    pic_url=item.get("albumPic", ""),
                    playable=True,
                ))

        total = data.get("data", {}).get(data_key, {}).get("total", len(songs))
        return SearchResult(source=self.name, keyword=keyword,
                            total=total or len(songs), songs=songs)

    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放链接，带音质降级"""
        songmid = self._resolve_songmid(song_id)
        quality_code = self._resolve_quality(level, quality)
        result, actual_code, _degraded = await self._degrade_quality(songmid, quality_code)
        if not result:
            raise QQAPIException(f"无法获取QQ播放链接: {song_id}")
        file_info = self.FILE_CONFIG[actual_code]
        return SongUrl(
            source=self.name,
            song_id=songmid,
            url=result["url"],
            level=actual_code,
            quality_name=self.REVERSE_QUALITY_MAP.get(actual_code, actual_code),
            br=self._bitrate_to_br(file_info["bitrate"]),
            size=0,
            type=file_info["e"].lstrip("."),
            expired=False,
        )

    async def get_song_detail(self, song_id: str) -> Song:
        """获取歌曲详情"""
        songmid = self._resolve_songmid(song_id)
        sid = 0
        mid = songmid
        if songmid.isdigit():
            sid = int(songmid)
            mid = ""
        params = {"platform": "yqq", "format": "json"}
        if sid:
            params["songid"] = sid
        else:
            params["songmid"] = mid

        data = await self._request(self.song_url, params=params)
        songs = data.get("data") or []
        if not songs:
            raise QQAPIException(f"QQ歌曲不存在: {song_id}")
        return self._build_song(songs[0], source_mid=songmid)

    async def get_lyric(self, song_id: str) -> Lyric:
        """获取歌词（PlayLyricInfo，base64 解码）"""
        songmid = self._resolve_songmid(song_id)
        sid = int(songmid) if songmid.isdigit() else await self._get_songid(songmid)

        payload = {
            "music.musichallSong.PlayLyricInfo.GetPlayLyricInfo": {
                "module": "music.musichallSong.PlayLyricInfo",
                "method": "GetPlayLyricInfo",
                "param": {
                    "trans_t": 0,
                    "roma_t": 0,
                    "crypt": 0,
                    "lrc_t": 0,
                    "interval": 208,
                    "trans": 1,
                    "ct": 6,
                    "songID": sid,
                },
            },
            "comm": {"ct": "6", "cv": "80600"},
        }
        data = await self._request(self.base_url, post_fields=json.dumps(payload))
        lyric_data = (data.get("music.musichallSong.PlayLyricInfo.GetPlayLyricInfo",
                               {}).get("data", {}) or {})
        lyric = self._decode_base64(lyric_data.get("lyric", ""))
        tlyric = self._decode_base64(lyric_data.get("trans", ""))
        return Lyric(source=self.name, song_id=songmid,
                     lyric=lyric, tlyric=tlyric)

    async def get_playlist(self, playlist_id: str, limit: int = 50) -> Playlist:
        """获取歌单详情"""
        pid = self._extract_dissid(playlist_id)
        params = {"id": pid, "format": "json", "type": 1, "p": 1, "n": limit}
        data = await self._request(self.playlist_url, params=params)
        cdlist = data.get("data", {}).get("cdlist", []) or []
        if not cdlist:
            raise QQAPIException(f"QQ歌单不存在: {playlist_id}")
        cd = cdlist[0]
        tracks = [self._build_playlist_song(s) for s in cd.get("songlist", []) or []]
        return Playlist(
            source=self.name,
            playlist_id=str(cd.get("disstid", pid)),
            name=cd.get("dissname", ""),
            cover_url=cd.get("logo", ""),
            creator=cd.get("nickname", ""),
            description=cd.get("desc", ""),
            tracks=tracks,
        )

    async def get_album(self, album_id: str) -> Album:
        """获取专辑详情"""
        album_mid = self._extract_albummid(album_id)
        data = await self._request(self.album_url,
                                   params={"albummid": album_mid, "format": "json"})
        if data.get("code") != 0:
            raise QQAPIException(f"QQ专辑不存在: {album_id}")
        info = data.get("data", {}) or {}
        mid = info.get("mid", album_mid)
        pic = f"https://y.qq.com/music/photo_new/T002R300x300M000{mid}.jpg" if mid else ""
        songs = []
        for s in info.get("list", []) or []:
            singers = "/".join(sg.get("name", "") for sg in s.get("singer", []) or [])
            songs.append(Song(
                source=self.name,
                song_id=str(s.get("songmid", "")),
                name=s.get("songname", ""),
                artists=singers,
                album=info.get("name", ""),
                duration=(s.get("interval", 0) or 0) * 1000,
                pic_url=pic,
                playable=True,
            ))
        return Album(
            source=self.name,
            album_id=str(mid),
            name=info.get("name", ""),
            cover_url=pic,
            artist=info.get("singername", ""),
            publish_time=0,
            songs=songs,
        )

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _build_song(self, raw: dict, source_mid: str = "") -> Song:
        """将 QQ 歌曲原始数据转换为统一 Song 模型"""
        album_info = raw.get("album", {}) or {}
        singers = raw.get("singer", []) or []
        singer_names = "/".join(s.get("name", "") for s in singers)
        album_mid = album_info.get("mid", "")
        pic = (f"https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg"
               if album_mid else "")
        interval = raw.get("interval", 0) or 0
        return Song(
            source=self.name,
            song_id=str(raw.get("mid", source_mid) or source_mid),
            name=raw.get("name", ""),
            artists=singer_names,
            album=album_info.get("name", ""),
            duration=interval * 1000,
            pic_url=pic,
            playable=True,
        )

    def _build_playlist_song(self, raw: dict) -> Song:
        """将歌单内歌曲原始数据转换为统一 Song 模型"""
        singers = "/".join(sg.get("name", "") for sg in raw.get("singer", []) or [])
        album_mid = raw.get("albummid", "")
        pic = (f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
               if album_mid else "")
        return Song(
            source=self.name,
            song_id=str(raw.get("songmid", "")),
            name=raw.get("songname", ""),
            artists=singers,
            album=raw.get("albumname", ""),
            duration=(raw.get("interval", 0) or 0) * 1000,
            pic_url=pic,
            playable=True,
        )

    async def _get_songid(self, songmid: str) -> int:
        """通过 songmid 获取数字 songid（歌词接口需要）"""
        data = await self._request(self.song_url,
                                   params={"songmid": songmid, "platform": "yqq",
                                           "format": "json"})
        songs = data.get("data") or []
        if not songs:
            raise QQAPIException(f"无法获取QQ歌曲ID: {songmid}")
        return int(songs[0].get("id", 0) or 0)

    # ------------------------------------------------------------------ #
    # 扫码登录（可选）
    # ------------------------------------------------------------------ #

    async def get_qr_code(self) -> Optional[dict]:
        """生成登录二维码，返回 qrsig 与 base64 图片"""
        t = random.randint(0, 9999999) / 10000000
        url = (f"https://xui.ptlogin2.qq.com/ssl/ptqrshow"
               f"?appid=716027609&e=2&l=M&s=3&d=72&v=4&t={t}"
               f"&daid=383&pt_3rd_aid=100497308"
               f"&u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 Chrome/149.0.0.0",
                "Referer": ("https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
                            "?appid=716027609&daid=383"),
            }
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
            qrsig = ""
            for k, v in resp.cookies.items():
                if k == "qrsig":
                    qrsig = v
            if not qrsig:
                m = re.search(r"qrsig=([^;]+)", resp.headers.get("set-cookie", ""))
                if m:
                    qrsig = m.group(1)
            if not qrsig:
                return None
            self.cookie_dict["qrsig"] = qrsig
            return {
                "qrsig": qrsig,
                "image": "data:image/png;base64," + base64.b64encode(resp.content).decode(),
            }
        except httpx.HTTPError as e:
            raise QQAPIException(f"获取QQ二维码失败: {e}") from e

    async def check_qr_login(self, qrsig: str):
        """轮询二维码登录状态，返回 (code, msg, cookies, callback_url)"""
        h = 0
        for c in qrsig:
            h = ((h << 5) + h + ord(c)) & 0xFFFFFFFF
        ptqrtoken = h & 0x7FFFFFFF
        import time as _time
        ts = int(_time.time() * 1000)
        url = (f"https://xui.ptlogin2.qq.com/ssl/ptqrlogin"
               f"?u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump"
               f"&ptqrtoken={ptqrtoken}&ptredirect=0&h=1&t=1&g=1&from_ui=1"
               f"&ptlang=2052&action=0-0-{ts}&js_ver=26030415&js_type=1"
               f"&login_sig=&pt_uistyle=40&aid=716027609&daid=383&pt_3rd_aid=100497308")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 Chrome/149.0.0.0",
                "Referer": "https://xui.ptlogin2.qq.com/",
            }
            cookies = {k: v for k, v in self.cookie_dict.items() if v}
            resp = await self.client.get(url, headers=headers, cookies=cookies)
            resp.raise_for_status()
            text = resp.text
            m = re.search(r"ptuiCB\('(\d+)','(\d+)','([^']*)','([^']*)','([^']*)'", text)
            if not m:
                return -1, "解析失败", {}, ""
            code = int(m.group(1))
            msg = m.group(5)
            cb_url = m.group(3)
            cookie_dict = dict(self.cookie_dict)
            for k, v in resp.cookies.items():
                if v:
                    cookie_dict[k] = v
            return code, msg, cookie_dict, cb_url
        except httpx.HTTPError as e:
            raise QQAPIException(f"检查QQ登录状态失败: {e}") from e

"""酷我音乐 (KuWo) Provider

移植自 egg.js 参考实现 docs/KuWoMusicApi（search / playUrl / musicInfo / lrc /
playListInfo / albumInfo），使用 httpx.AsyncClient 异步请求。

要点：
- song_id 为酷我数字 rid（如 "556654321"），兼容 "MUSIC_123" 前缀与 www.kuwo.cn 链接。
- www.kuwo.cn 的 /api、/openapi 接口需要 ``Secret`` 请求头：
  先 GET 首页拿到服务端下发的 ``Hm_Iuvt_*`` cookie，再以 cookie 名密钥、
  cookie 值为明文做混淆哈希（移植自参考仓库 app/utils/secret.js 的 h()，
  并精确模拟 JS 的 parseInt/Number.toString 浮点截断语义）。
- 免费歌曲无需任何 cookie；VIP 歌曲可通过基类 cookies 参数传入登录态
  （如 kw_token / kw_uid 等），会原样合并进请求头。
- 单曲搜索优先免签名 search.kuwo.cn/r.s（官方曲库全量），失败回退签名 openapi；
  专辑/歌单/歌手搜索走签名 api/www。
- 播放直链走音质降级：rsflac(Hi-Res) → flac → 320 → 192 → 128，
  全部失败时回退 antiserver.kuwo.cn/anti.s（免签名，仅 mp3）。
  playUrl 对匿名请求会静默降质，实际等级按 CDN 文件名/扩展名纠正。
- 歌词走 openapi/v1/www/lyric/getlyric（lrclist JSON，失败回退 m.kuwo.cn），转标准 LRC。
"""

from __future__ import annotations

import json
import random
import re
import uuid
from datetime import datetime
from typing import Optional

import httpx
from urllib.parse import unquote

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import register_provider
from app.providers.base import MusicProvider


class KuWoAPIException(Exception):
    """酷我音乐 API 异常"""


def _js_parse_int(s: str) -> float:
    """模拟 JS parseInt：读取前导数字，超长数字串按 double 截断。"""
    m = re.match(r"[+-]?\d+", s.lstrip())
    if not m:
        return 0.0
    return float(m.group())


def _num_to_str(x: float) -> str:
    """模拟 JS Number.prototype.toString()（>=1e21 用科学计数法）。"""
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    return repr(x)


def kuwo_secret(value: str, key: str) -> str:
    """酷我 Web Secret 签名（移植自参考仓库 secret.js 的 h(t, e)）。

    value: Hm_Iuvt_* cookie 的值；key: cookie 名（作为密钥）。
    """
    if not key:
        raise KuWoAPIException("酷我签名缺少密钥")
    n = "".join(str(ord(c)) for c in key)
    r = len(n) // 5

    def char_at(s: str, i: int) -> str:  # JS charAt 越界返回空串
        return s[i] if 0 <= i < len(s) else ""

    o = int(char_at(n, r) + char_at(n, 2 * r) + char_at(n, 3 * r)
            + char_at(n, 4 * r) + char_at(n, 5 * r))
    addend = -(-len(key) // 2)  # Math.ceil
    c = 2 ** 31 - 1
    if o < 2:
        raise KuWoAPIException("酷我签名密钥无效")
    d = round(1e9 * random.random()) % 10 ** 8
    n += str(d)
    while len(n) > 10:
        n = _num_to_str(_js_parse_int(n[:10]) + _js_parse_int(n[10:]))
    n = (o * int(n) + addend) % c
    out = []
    for ch in value:
        h = ord(ch) ^ int(n / c * 255)
        out.append(f"{h:02x}")
        n = (o * n + addend) % c
    return "".join(out) + format(d, "08x")


@register_provider
class KuWoProvider(MusicProvider):
    """酷我音乐 Provider 实现"""

    name = "kuwo"
    display_name = "酷我音乐"

    # 音质降级顺序：Hi-Res → FLAC → 320 → 192 → 128
    DEGRADE_ORDER = ["rsflac", "flac", "320", "192", "128"]

    # 统一 level 参数 → 酷我内部音质代码
    LEVEL_MAP = {
        "standard": "128",
        "high": "192",
        "exhigh": "320",
        "lossless": "flac",
        "flac": "flac",
        "hires": "rsflac",
        "master": "rsflac",
    }

    # 音质代码 → playUrl 接口 br 参数
    BR_PARAM = {
        "128": "128kmp3",
        "192": "192kmp3",
        "320": "320kmp3",
        "flac": "flac",
        "rsflac": "rsflac",
    }

    # 音质代码 → 中文名 / 码率(bps) / 容器格式
    QUALITY_NAMES = {
        "128": "标准", "192": "HQ高品质", "320": "超高品质",
        "flac": "SQ无损", "rsflac": "Hi-Res无损",
    }
    QUALITY_BR = {"128": 128000, "192": 192000, "320": 320000,
                  "flac": 900000, "rsflac": 2000000}
    QUALITY_TYPE = {"128": "mp3", "192": "mp3", "320": "mp3",
                    "flac": "flac", "rsflac": "flac"}

    # CDN 文件名前缀 → 实际音质代码（playUrl 会静默降质，需按 URL 判定真实等级）
    FILE_PREFIX = {
        "M500": "128", "M600": "192", "M800": "320",
        "F000": "flac", "F010": "flac", "F600": "flac",
        "RS00": "rsflac", "AI00": "rsflac",
    }

    def __init__(self, cookies: str = ""):
        super().__init__(cookies)
        self.home_url = "http://www.kuwo.cn/"
        self.search_music_url = f"{self.home_url}openapi/v1/www/search/searchMusicBykeyWord"
        self.search_rs_url = "http://search.kuwo.cn/r.s"
        self.search_album_url = f"{self.home_url}api/www/search/searchAlbumBykeyWord"
        self.search_playlist_url = f"{self.home_url}api/www/search/searchPlayListBykeyWord"
        self.search_artist_url = f"{self.home_url}api/www/search/searchArtistBykeyWord"
        self.play_url = f"{self.home_url}api/v1/www/music/playUrl"
        self.play_fallback_url = "http://antiserver.kuwo.cn/anti.s"
        self.music_info_url = f"{self.home_url}api/www/music/musicInfo"
        self.lyric_url = f"{self.home_url}openapi/v1/www/lyric/getlyric"
        self.lyric_fallback_url = "http://m.kuwo.cn/newh5/singles/songinfoandlrc"
        self.playlist_url = f"{self.home_url}api/www/playlist/playListInfo"
        self.album_url = f"{self.home_url}api/www/album/albumInfo"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "http://www.kuwo.cn/",
        }
        self.cookie_dict: dict = self._parse_cookie_string(cookies)
        self._hm_key: str = ""
        self._hm_val: str = ""
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        """惰性创建异步客户端"""
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self.headers, timeout=30,
                                             follow_redirects=True)
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
        """解析 Cookie 字符串为字典"""
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

    @staticmethod
    def _strip_jsonp(text: str) -> dict:
        """稳健解析 JSONP/JSON：剥离 callback(...) 包裹，失败回退纯 JSON。"""
        t = text.strip()
        m = re.match(r"^[\w$.]+\((.*)\)\s*;?\s*$", t, re.S)
        if m:
            t = m.group(1)
        try:
            return json.loads(t)
        except (ValueError, TypeError) as e:
            raise KuWoAPIException(f"酷我响应解析失败: {e}") from e

    @staticmethod
    def _normalize_rid(song_id: str) -> str:
        """归一化歌曲 ID：支持纯数字 / MUSIC_123 / www.kuwo.cn 链接。"""
        s = str(song_id).strip()
        m = re.search(r"(?:play_detail|songDetail|rid=|musicId=|mid=)[/?=&]*(\d+)", s)
        if m:
            return m.group(1)
        m = re.match(r"^MUSIC_(\d+)$", s, re.I)
        if m:
            return m.group(1)
        return s.lstrip("0") or "0" if s.isdigit() else s

    @staticmethod
    def _extract_id(value: str, patterns: tuple) -> str:
        """从 URL 或原始值中提取数字 ID"""
        for p in patterns:
            m = re.search(p, value)
            if m:
                return m.group(1)
        return value.strip()

    async def _ensure_session(self):
        """确保持有服务端下发的 Hm_Iuvt_* 会话 cookie（Secret 签名依赖它）。"""
        if self._hm_key and self._hm_val:
            return
        for k, v in self.cookie_dict.items():
            if k.startswith("Hm_Iuvt_"):
                self._hm_key, self._hm_val = k, v
                return
        resp = await self.client.get(self.home_url)
        for k, v in resp.cookies.items():
            if k.startswith("Hm_Iuvt_"):
                self._hm_key, self._hm_val = k, v
                return
        raise KuWoAPIException("无法获取酷我会话 Cookie（首页未下发 Hm_Iuvt_*）")

    def _signed_headers(self) -> dict:
        """构造带 Cookie + Secret 的请求头（Secret 每次随机重新生成）。"""
        headers = dict(self.headers)
        cookies = dict(self.cookie_dict)
        cookies[self._hm_key] = self._hm_val
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Secret"] = kuwo_secret(self._hm_val, self._hm_key)
        return headers

    async def _api_get(self, url: str, params: dict) -> dict:
        """酷我签名接口 GET：自动引导会话，签名失效重置重试，网络错误重试一次
        （参考仓库 BaseService 的失败自动重试）。"""
        last_http_err: Optional[httpx.HTTPError] = None
        for attempt in (1, 2):
            await self._ensure_session()
            try:
                resp = await self.client.get(url, params={
                    **params, "httpsStatus": 1, "reqId": str(uuid.uuid4()),
                    "plat": "web_www",
                }, headers=self._signed_headers())
                resp.raise_for_status()
            except httpx.HTTPError as e:
                last_http_err = e
                continue
            data = self._strip_jsonp(resp.text)
            if data.get("message") == "The request is illegal!":
                self._hm_key = self._hm_val = ""
                if attempt == 1:
                    continue
                raise KuWoAPIException("酷我签名校验失败（接口可能已变更）")
            return data
        if last_http_err is not None:
            raise KuWoAPIException(f"酷我请求失败: {last_http_err}")
        raise KuWoAPIException("酷我签名校验失败")

    async def _plain_get(self, url: str, params: dict) -> dict:
        """免签名接口 GET（search.kuwo.cn / antiserver / m.kuwo.cn）。"""
        try:
            resp = await self.client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise KuWoAPIException(f"酷我请求失败: {e}") from e
        return self._strip_jsonp(resp.text)

    # ------------------------------------------------------------------ #
    # 数据映射
    # ------------------------------------------------------------------ #

    @staticmethod
    def _unq(s: str) -> str:
        """还原 percent-编码文本（部分用户上传曲目名称被 URL 编码，
        且尾部可能残留截断的孤立 '%'）。"""
        if s and "%" in s:
            d = unquote(s)
            if d != s and not re.search(r"%[0-9a-fA-F]{2}", d):
                if d.endswith("%") and s.endswith("%"):
                    d = d[:-1]
                return d
        return s

    @staticmethod
    def _rs_pic(raw: dict) -> str:
        """r.s 的 web_albumpic_short（如 '120/7/83/x.jpg'）拼成完整封面 URL"""
        short = raw.get("web_albumpic_short") or ""
        if short.startswith("http"):
            return short
        if short:
            return f"https://img2.kuwo.cn/star/albumcover/{short}"
        return ""

    def _song_from_api(self, raw: dict) -> Song:
        """openapi/api 搜索与列表项（小写字段）→ 统一 Song"""
        pic = raw.get("pic") or raw.get("pic120") or raw.get("albumpic") or ""
        pay_info = raw.get("payInfo") or {}
        return Song(
            source=self.name,
            song_id=str(raw.get("rid", "")),
            name=self._unq(raw.get("name", "")),
            artists=self._unq(raw.get("artist", "")),
            album=self._unq(raw.get("album", "")),
            duration=(raw.get("duration", 0) or 0) * 1000,
            pic_url=pic,
            playable=bool(raw.get("online", 1)) and not pay_info.get("cannotOnlinePlay"),
        )

    def _song_from_rs(self, raw: dict) -> Song:
        """search.kuwo.cn/r.s abslist 项（大写字段）→ 统一 Song"""
        rid = str(raw.get("MUSICRID", "")).replace("MUSIC_", "")
        pay_info = raw.get("payInfo") or {}
        return Song(
            source=self.name,
            song_id=rid,
            name=self._unq(raw.get("NAME", "") or raw.get("SONGNAME", "")),
            artists=self._unq(raw.get("ARTIST", "")),
            album=self._unq(raw.get("ALBUM", "")),
            duration=int(raw.get("DURATION", 0) or 0) * 1000,
            pic_url=self._rs_pic(raw),
            playable=str(raw.get("ONLINE", "1")) == "1"
            and not pay_info.get("cannotOnlinePlay"),
        )

    # ------------------------------------------------------------------ #
    # MusicProvider 抽象方法
    # ------------------------------------------------------------------ #

    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """搜索：1=单曲 10=专辑 100=歌手 1000=歌单"""
        st = int(search_type)
        page = offset // max(limit, 1) + 1
        if st == 1:
            return await self._search_music(keyword, limit, page)
        if st == 10:
            return await self._search_other(
                self.search_album_url, keyword, limit, page, "albumList",
                lambda x: Song(source=self.name, song_id=str(x.get("albumid", "")),
                               name=x.get("album", ""), artists=x.get("artist", ""),
                               album=x.get("album", ""), duration=0,
                               pic_url=x.get("pic", ""), playable=False))
        if st == 1000:
            return await self._search_other(
                self.search_playlist_url, keyword, limit, page, "list",
                lambda x: Song(source=self.name, song_id=str(x.get("id", "")),
                               name=x.get("name", ""), artists=x.get("uname", ""),
                               album="", duration=0, pic_url=x.get("img", ""),
                               playable=False))
        if st == 100:
            return await self._search_other(
                self.search_artist_url, keyword, limit, page, "artistList",
                lambda x: Song(source=self.name, song_id=str(x.get("id", "")),
                               name=x.get("name", ""), artists=x.get("name", ""),
                               album="", duration=0, pic_url=x.get("pic", ""),
                               playable=False))
        return await self._search_music(keyword, limit, page)

    async def _search_music(self, keyword: str, limit: int, page: int) -> SearchResult:
        """单曲搜索：优先免签名 r.s（官方曲库全量、UTF-8 JSON/JSONP），
        失败回退签名 openapi（注意其曲库覆盖差，歌手名查询常只有个位数结果，
        且 UGC 字段为 percent-编码——total 偏小属该接口自身特性，非分页 bug）。
        """
        try:
            data = await self._plain_get(self.search_rs_url, {
                "vipver": 1, "client": "kt", "ft": "music", "cluster": 0,
                "strategy": 2012, "encoding": "utf8", "rformat": "json",
                "mobi": 1, "issubtitle": 1, "show_copyright_off": 1,
                "pn": page - 1, "rn": limit, "all": keyword,
            })
            items = data.get("abslist") or []
            total = int(data.get("TOTAL", len(items)) or 0)
            return SearchResult(source=self.name, keyword=keyword, total=total,
                                songs=[self._song_from_rs(x) for x in items[:limit]])
        except KuWoAPIException:
            data = await self._api_get(self.search_music_url,
                                       {"key": keyword, "pn": page, "rn": limit})
            items = (data.get("data") or {}).get("list") or []
            total = int((data.get("data") or {}).get("total", len(items)) or 0)
            return SearchResult(source=self.name, keyword=keyword, total=total,
                                songs=[self._song_from_api(x) for x in items[:limit]])

    async def _search_other(self, url: str, keyword: str, limit: int,
                            page: int, list_key: str, mapper) -> SearchResult:
        """专辑/歌单/歌手搜索（签名接口），结果映射为统一 Song。"""
        data = await self._api_get(url, {"key": keyword, "pn": page, "rn": limit})
        payload = data.get("data") or {}
        items = payload.get(list_key) or []
        total = int(payload.get("total", len(items)) or 0)
        return SearchResult(source=self.name, keyword=keyword, total=total,
                            songs=[mapper(x) for x in items[:limit]])

    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放直链，带音质降级（rsflac→flac→320→192→128→anti.s）。

        注意：playUrl 对无 VIP 登录态的请求会静默返回最高可用音质，
        实际等级由 CDN 文件名前缀（M500/M800/F000 等）判定；
        付费歌曲匿名走 anti.s 时返回约 30 秒通用试听片段，
        quality_name 会如实标注「标准(试听片段)」，传入 VIP cookie 可得完整直链。
        """
        rid = self._normalize_rid(song_id)
        code = self._resolve_quality(level, quality)
        order = self.DEGRADE_ORDER
        start = order.index(code) if code in order else 0
        last_err = ""
        for requested in order[start:]:
            url, last_err, preview = await self._try_play_url(rid, requested)
            if url:
                actual = "128" if preview else self._actual_code(url, requested)
                size = await self._probe_size(url)
                return SongUrl(
                    source=self.name, song_id=rid, url=url, level=actual,
                    quality_name="标准(试听片段)" if preview
                    else self.QUALITY_NAMES.get(actual, actual),
                    br=self.QUALITY_BR.get(actual, 0), size=size,
                    type=self.QUALITY_TYPE.get(actual, "mp3"), expired=False,
                )
        raise KuWoAPIException(f"无法获取酷我播放链接: {song_id} ({last_err})")

    def _actual_code(self, url: str, requested: str) -> str:
        """从 CDN 直链推断实际音质等级（playUrl 静默降质时纠正 level）。

        文件名前缀 M500/M800/F000 等可精确判级；无前缀时按扩展名兜底：
        请求无损却拿到 mp3，保守报 128（匿名直链的实际可用上限）。
        """
        m = re.search(r"/([A-Z]\d{3})[^/]*?\.(mp3|flac|ogg|m4a|aac)(?:[?/]|$)", url)
        if m:
            if m.group(1) in self.FILE_PREFIX:
                return self.FILE_PREFIX[m.group(1)]
            ext = m.group(2)
        else:
            m2 = re.search(r"\.(mp3|flac|ogg|m4a|aac)(?:[?/]|$)", url)
            ext = m2.group(1) if m2 else ""
        if ext == "flac":
            return "rsflac" if requested == "rsflac" else "flac"
        if ext == "mp3" and requested in ("flac", "rsflac"):
            return "128"
        return requested

    async def _try_play_url(self, rid: str, code: str):
        """尝试单一音质：先签名 playUrl，付费受限再回退 anti.s（仅 mp3）。

        返回 (url, err, is_preview)：anti.s 对付费歌返回通用试听片段
        （URL 路径含 /nf/resource/），如实标记为 preview 而非完整直链。
        """
        try:
            data = await self._api_get(self.play_url, {
                "mid": rid, "type": "music", "plat": "web_www", "from": "",
                "br": self.BR_PARAM.get(code, "128kmp3"),
            })
        except KuWoAPIException as e:
            return "", str(e), False
        payload = data.get("data") or {}
        url = payload.get("url") or payload.get("song") or ""
        if url:
            return url, "", False
        msg = data.get("msg") or data.get("message") or ""
        if code in self.BR_PARAM and self.BR_PARAM[code].endswith("mp3"):
            bitrate = self.BR_PARAM[code].replace("kmp3", "")
            try:
                fb = await self._plain_get(self.play_fallback_url, {
                    "type": "convert_url3", "format": "mp3",
                    "bitrate": bitrate, "rid": f"MUSIC_{rid}",
                })
                if fb.get("url"):
                    return fb["url"], "", "/nf/resource/" in fb["url"]
            except KuWoAPIException:
                pass
        return "", msg, False

    async def _probe_size(self, url: str) -> int:
        """尽力获取文件大小（HEAD），失败返回 0。"""
        try:
            resp = await self.client.head(url, timeout=10)
            return int(resp.headers.get("content-length", 0) or 0)
        except httpx.HTTPError:
            return 0

    def _resolve_quality(self, level: str, quality: str) -> str:
        """统一 level/中文音质 → 酷我音质代码"""
        q = (quality or "").strip()
        if q:
            if q in self.DEGRADE_ORDER:
                return q
            for name, code in self.QUALITY_NAMES.items():
                if code == q:
                    return name
        lv = (level or "").strip()
        if lv in self.DEGRADE_ORDER:
            return lv
        return self.LEVEL_MAP.get(lv, "flac")

    async def get_song_detail(self, song_id: str) -> Song:
        """获取歌曲详情"""
        rid = self._normalize_rid(song_id)
        data = await self._api_get(self.music_info_url, {"mid": rid})
        raw = data.get("data") or {}
        if not raw:
            raise KuWoAPIException(f"酷我歌曲不存在: {song_id}")
        return self._song_from_api(raw)

    async def get_lyric(self, song_id: str) -> Lyric:
        """获取歌词（getlyric lrclist JSON → 标准 LRC；酷我无翻译歌词，tlyric 为空）。

        主接口为签名 openapi，失败回退免签名 m.kuwo.cn。
        """
        rid = self._normalize_rid(song_id)
        lrclist: list = []
        try:
            data = await self._api_get(self.lyric_url, {"musicId": rid})
            lrclist = ((data.get("data") or {}).get("lrclist")) or []
        except KuWoAPIException:
            pass
        if not lrclist:
            data = await self._plain_get(self.lyric_fallback_url,
                                         {"musicId": rid, "httpsStatus": 1})
            lrclist = ((data.get("data") or {}).get("lrclist")) or []
        if not lrclist:
            raise KuWoAPIException(f"酷我歌词不存在或查询失败: {song_id}")
        lines = []
        for item in lrclist:
            try:
                secs = float(item.get("time", 0) or 0)
            except (TypeError, ValueError):
                continue
            mm = int(secs) // 60
            ss = secs - mm * 60
            lines.append(f"[{mm:02d}:{ss:05.2f}]{item.get('lineLyric', '')}")
        return Lyric(source=self.name, song_id=rid,
                     lyric="\n".join(lines), tlyric="")

    async def get_playlist(self, playlist_id: str, limit: int = 50) -> Playlist:
        """获取歌单详情"""
        pid = self._extract_id(str(playlist_id),
                               (r"playlist_detail/(\d+)", r"[?&]pid=(\d+)"))
        data = await self._api_get(self.playlist_url,
                                   {"pid": pid, "pn": 1, "rn": limit})
        raw = data.get("data") or {}
        if not raw:
            raise KuWoAPIException(f"酷我歌单不存在: {playlist_id}")
        return Playlist(
            source=self.name,
            playlist_id=str(raw.get("id", pid)),
            name=raw.get("name", ""),
            cover_url=raw.get("img") or raw.get("img700") or "",
            creator=raw.get("uname") or raw.get("userName") or "",
            description=raw.get("desc") or raw.get("info") or "",
            tracks=[self._song_from_api(s) for s in raw.get("musicList") or []],
        )

    async def get_album(self, album_id: str) -> Album:
        """获取专辑详情"""
        aid = self._extract_id(str(album_id),
                               (r"album_detail/(\d+)", r"[?&]albumId=(\d+)"))
        data = await self._api_get(self.album_url,
                                   {"albumId": aid, "pn": 1, "rn": 100})
        raw = data.get("data") or {}
        if not raw:
            raise KuWoAPIException(f"酷我专辑不存在: {album_id}")
        return Album(
            source=self.name,
            album_id=str(raw.get("albumid", aid)),
            name=raw.get("album", ""),
            cover_url=raw.get("pic", ""),
            artist=raw.get("artist", ""),
            publish_time=self._parse_date_ms(raw.get("releaseDate", "")),
            songs=[self._song_from_api(s) for s in raw.get("musicList") or []],
        )

    @staticmethod
    def _parse_date_ms(value: str) -> int:
        """'2010-05-18' → 毫秒时间戳，失败返回 0"""
        try:
            return int(datetime.strptime(value, "%Y-%m-%d").timestamp() * 1000)
        except (ValueError, TypeError):
            return 0

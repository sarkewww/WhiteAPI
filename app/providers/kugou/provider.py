"""酷狗音乐 Provider

基于酷狗官方 Android 接口 (gateway.kugou.com / lyrics.kugou.com) 实现，
移植自 KuGouMusicApi (Node.js) 的签名与端点逻辑，纯 Python + httpx。

song_id 方案：统一使用酷狗曲目 hash（32 位十六进制字符串，如
``8E10D8825DDE03BCABBDE13E5A4150D2``）。search 返回的 song_id 即该 hash，
get_song_url / get_song_detail / get_lyric 均接受同一 hash，
也兼容直接传入酷狗歌曲链接（自动提取 hash 参数）。

签名机制（移植自 util/helper.js）：
- signature = MD5(盐值 + 按 key 排序拼接的 "k=v" 串 + 请求体 + 盐值)
- signKey   = MD5(hash + 盐值 + appid + mid + userid)   （播放直链 key 参数）

设备注册：播放直链接口要求已注册设备的 dfid（register_dev.js，
AES-128-CBC + RSA-PKCS1v15，纯 Python 实现，首次取链时自动注册一次）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
import time
import uuid
import zlib
from typing import Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import register_provider
from app.providers.base import MusicProvider


class KugouAPIException(Exception):
    """酷狗音乐 API 异常"""


# 标准版 RSA 公钥（util/crypto.js publicRasKey，用于 register_dev）
KUGOU_RSA_PUBKEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/g"
    "bjDJX51HBNnEl5HXqTW6lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+"
    "UR4tvOGOqp94TJtQ1EPnWGWXngpeIW5GxoQGao1rmYWAu6oi1z9XkChrsUdC6DJE"
    "5E221wf/4WLFxwAtRQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


def _md5(text: str) -> str:
    """MD5 十六进制小写"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


@register_provider
class KuGouProvider(MusicProvider):
    """酷狗音乐 Provider 实现

    song_id 统一为曲目 hash（32 位 hex）。免费歌曲无需 cookie；
    VIP 歌曲可通过基类 cookies 参数传入 ``token=xxx;userid=xxx`` 登录态。
    """

    name = "kugou"
    display_name = "酷狗音乐"

    GATEWAY = "https://gateway.kugou.com"
    LYRICS = "https://lyrics.kugou.com"
    USERSERVICE = "https://userservice.kugou.com"

    APPID = 1005
    CLIENTVER = 20489

    # 签名盐值（标准版，来自 util/helper.js）
    SALT_ANDROID = "OIlwieks28dk2k092lksi2UIkp"
    SALT_SIGN_KEY = "57ae12eb6890223e355ccfcb74edf70d"

    # 音质降级顺序：super → high → flac → 320 → 128
    DEGRADE_ORDER = ["super", "high", "flac", "320", "128"]

    # 统一 level 参数 → 酷狗音质参数
    LEVEL_MAP = {
        "standard": "128",
        "high": "320",
        "exhigh": "320",
        "lossless": "flac",
        "flac": "flac",
        "hires": "high",
        "master": "super",
        "viper_atmos": "viper_atmos",
        "viper_clear": "viper_clear",
        "viper_tape": "viper_tape",
    }

    # 中文音质名 → 酷狗音质参数
    QUALITY_MAP = {
        "标准": "128",
        "HQ高品质": "320",
        "SQ无损品质": "flac",
        "无损音质": "flac",
        "Hi-Res": "high",
        "杜比全景声": "viper_atmos",
        "蝰蛇全景声": "viper_atmos",
        "蝰蛇超清音质": "viper_clear",
        "蝰蛇母带": "viper_tape",
        "DSD母带": "super",
    }

    # 酷狗音质参数 → 中文音质名
    REVERSE_QUALITY_MAP = {
        "128": "标准",
        "320": "HQ高品质",
        "flac": "SQ无损品质",
        "high": "Hi-Res无损",
        "super": "DSD母带",
        "viper_atmos": "蝰蛇全景声",
        "viper_clear": "蝰蛇超清音质",
        "viper_tape": "蝰蛇母带",
    }

    # 音质参数 → (兜底码率 bps, 文件格式)；实际以 /v5/url 返回的 bitRate 为准
    FILE_CONFIG = {
        "128": (128000, "mp3"),
        "320": (320000, "mp3"),
        "flac": (0, "flac"),
        "high": (0, "flac"),
        "super": (0, "dsd"),
        "viper_atmos": (0, "mp3"),
        "viper_clear": (0, "mp3"),
        "viper_tape": (0, "mp3"),
    }

    # 搜索类型：统一 search_type → (酷狗 type, 路径)
    SEARCH_TYPES = {
        1: ("song", "/v3/search/song"),
        10: ("album", "/v1/search/album"),
        100: ("author", "/v1/search/author"),
        1000: ("special", "/v1/search/special"),
    }

    # KRC 歌词 XOR 解密密钥（util/util.js decodeLyrics）
    KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])

    def __init__(self, cookies: str = ""):
        super().__init__(cookies)
        self.cookie_dict: dict = self._parse_cookie_string(cookies)
        # 设备标识：guid = MD5(uuid4)，mid = int(MD5(guid), 16) 的十进制串
        self.guid = _md5(uuid.uuid4().hex)
        self.mid = str(int(_md5(self.guid), 16))
        self.dfid = self.cookie_dict.get("dfid") or self._random_string(24)
        self.token = self.cookie_dict.get("token", "")
        self.userid = self.cookie_dict.get("userid", "0")
        self._dfid_from_cookie = "dfid" in self.cookie_dict
        self._dfid_registered = False
        self._client: Optional[httpx.AsyncClient] = None

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
    def _random_string(length: int = 16) -> str:
        """随机大写字母+数字串（util.js randomString）"""
        pool = string.digits + string.ascii_uppercase
        return "".join(random.choice(pool) for _ in range(length))

    @staticmethod
    def _fmt_value(value) -> str:
        """参数值序列化为签名用字符串（对齐 JS 模板字符串行为）"""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return str(value)

    @staticmethod
    def _json_body(data: dict) -> str:
        """POST 请求体序列化（与发送字节完全一致）"""
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _signature_android(cls, params: dict, body: str = "") -> str:
        """Android 版 signature 签名（helper.js signatureAndroidParams）"""
        params_string = "".join(
            f"{key}={cls._fmt_value(params[key])}" for key in sorted(params)
        )
        return _md5(f"{cls.SALT_ANDROID}{params_string}{body}{cls.SALT_ANDROID}")

    @classmethod
    def _sign_key(cls, song_hash: str, mid: str, userid, appid) -> str:
        """播放直链 key 参数（helper.js signKey，标准版盐值）"""
        return _md5(f"{song_hash}{cls.SALT_SIGN_KEY}{appid}{mid}{userid or 0}")

    def _default_params(self) -> dict:
        """每个请求注入的默认设备参数（request.js defaultParams）"""
        params = {
            "dfid": self.dfid,
            "mid": self.mid,
            "uuid": "-",
            "appid": self.APPID,
            "clientver": self.CLIENTVER,
            "clienttime": int(time.time()),
        }
        if self.token:
            params["token"] = self.token
        if self.userid and self.userid != "0":
            params["userid"] = self.userid
        return params

    def _base_headers(self, clienttime: int) -> dict:
        """酷狗客户端请求头（request.js headers）"""
        headers = {
            "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",
            "dfid": self.dfid,
            "clienttime": str(clienttime),
            "mid": self.mid,
            "kg-rc": "1",
            "kg-thash": "5d816a0",
            "kg-rec": "1",
            "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        }
        cookie = dict(self.cookie_dict)
        cookie.setdefault("dfid", self.dfid)
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie.items())
        return headers

    async def _ensure_dfid(self):
        """播放直链接口要求已注册设备的 dfid，否则返回“本次请求需要验证”。

        移植自 register_dev.js：设备信息 AES 加密 + RSA 密钥包装，
        响应 AES 解密取 data.dfid。用户自带 dfid cookie 时跳过。
        """
        if self._dfid_registered or self._dfid_from_cookie:
            return
        self._dfid_registered = True  # 只尝试一次，失败也不重试
        try:
            self.dfid = await self._register_device() or self.dfid
        except KugouAPIException:
            pass

    async def _register_device(self) -> str:
        """调用 userservice.kugou.com /risk/v2/r_register_dev 注册设备"""
        imei = self.cookie_dict.get("KUGOU_API_GUID") or self.guid
        dev_data = {
            "availableRamSize": 4983533568, "availableRomSize": 48114719,
            "availableSDSize": 48114717, "basebandVer": "", "batteryLevel": 100,
            "batteryStatus": 3, "brand": "Redmi", "buildSerial": "unknown",
            "device": "marble", "imei": imei, "imsi": "", "manufacturer": "Xiaomi",
            "uuid": imei, "accelerometer": False, "accelerometerValue": "",
            "gravity": False, "gravityValue": "", "gyroscope": False,
            "gyroscopeValue": "", "light": False, "lightValue": "",
            "magnetic": False, "magneticValue": "", "orientation": False,
            "orientationValue": "", "pressure": False, "pressureValue": "",
            "step_counter": False, "step_counterValue": "",
            "temperature": False, "temperatureValue": "",
        }
        aes_key, aes_body = self._aes_encrypt_payload(dev_data)
        uid = int(self.userid) if str(self.userid).isdigit() else 0
        priv = self._rsa_encrypt_hex(
            self._json_body({"aes": aes_key, "uid": uid, "token": self.token}))

        params = self._default_params()
        params["dfid"] = "-"
        params.update({"part": 1, "platid": 1, "p": priv})
        params["signature"] = self._signature_android(params, aes_body)
        headers = self._base_headers(params["clienttime"])
        headers["dfid"] = "-"
        headers["Cookie"] = "dfid=-"
        try:
            resp = await self.client.post(
                f"{self.USERSERVICE}/risk/v2/r_register_dev",
                params=params, content=aes_body.encode(), headers=headers)
            resp.raise_for_status()
            decoded = self._aes_decrypt_payload(resp.content, aes_key)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise KugouAPIException(f"酷狗设备注册失败: {e}") from e
        if decoded.get("status") != 1:
            raise KugouAPIException(f"酷狗设备注册被拒: {decoded}")
        return str((decoded.get("data") or {}).get("dfid", ""))

    @staticmethod
    def _aes_encrypt_payload(obj) -> tuple:
        """playlistAesEncrypt：随机6位key → MD5 拆 key/iv → AES-CBC → base64"""
        key = "".join(random.choice(string.ascii_uppercase + string.digits)
                      for _ in range(6)).lower()
        mk = _md5(key)
        enc_key, iv = mk[:16].encode(), mk[16:32].encode()
        plaintext = KuGouProvider._json_body(obj).encode("utf-8")
        pad = 16 - len(plaintext) % 16
        plaintext += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        return key, base64.b64encode(ciphertext).decode()

    @staticmethod
    def _aes_decrypt_payload(ciphertext: bytes, key: str) -> dict:
        """playlistAesDecrypt：AES-CBC 解密 + PKCS7 去填充 → JSON"""
        mk = _md5(key)
        enc_key, iv = mk[:16].encode(), mk[16:32].encode()
        decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return json.loads(plaintext[: -plaintext[-1]].decode("utf-8"))

    @staticmethod
    def _rsa_encrypt_hex(text: str) -> str:
        """rsaEncrypt2：RSAES-PKCS1-V1_5 → hex 字符串"""
        public_key = serialization.load_pem_public_key(
            KUGOU_RSA_PUBKEY.encode("utf-8"))
        return public_key.encrypt(text.encode("utf-8"),
                                  asym_padding.PKCS1v15()).hex()

    async def _request(self, url: str, params: Optional[dict] = None,
                       data: Optional[dict] = None, headers: Optional[dict] = None,
                       sign_key_hash: str = "", use_defaults: bool = True) -> dict | list:
        """统一请求封装：注入默认参数 → key/signature 签名 → 发送 → 校验状态"""
        query = dict(self._default_params() if use_defaults else {})
        query.update(params or {})
        if sign_key_hash:
            query["key"] = self._sign_key(
                sign_key_hash.lower(), self.mid, self.userid, self.APPID)
        body = self._json_body(data) if data is not None else ""
        query["signature"] = self._signature_android(query, body)

        req_headers = self._base_headers(query.get("clienttime", int(time.time())))
        req_headers.update(headers or {})
        try:
            if data is not None:
                req_headers["Content-Type"] = "application/json"
                resp = await self.client.post(url, params=query, content=body,
                                              headers=req_headers)
            else:
                resp = await self.client.get(url, params=query, headers=req_headers)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as e:
            raise KugouAPIException(f"酷狗请求失败: {e}") from e
        except ValueError as e:
            raise KugouAPIException(f"酷狗响应非 JSON: {e}") from e

        if isinstance(payload, list):  # /v5/url 等接口直接返回数组
            return payload
        # 对齐 request.js：仅 status==0 或 error_code!=0 视为失败（errcode 不参与判断）
        status = payload.get("status")
        error_code = payload.get("error_code")
        if status == 0 or (error_code not in (None, 0)):
            msg = (payload.get("error_msg") or payload.get("error")
                   or payload.get("msg") or f"error_code={error_code}")
            raise KugouAPIException(f"酷狗接口错误: {msg}")
        return payload

    @staticmethod
    def _resolve_hash(song_id: str) -> str:
        """从 song_id / 链接中提取曲目 hash（32 位 hex，小写）"""
        value = song_id.strip()
        m = re.search(r"hash=([0-9a-zA-Z]{32})", value)
        if m:
            return m.group(1).lower()
        m = re.search(r"[0-9a-fA-F]{32}", value)
        if m:
            return m.group(0).lower()
        return value.lower()

    @staticmethod
    def _clean_cover(value: str) -> str:
        """酷狗封面 URL 带 {size} 模板，替换为具体尺寸"""
        return (value or "").replace("{size}", "400")

    def _resolve_quality(self, level: str, quality: str) -> str:
        """将统一 level/中文音质解析为酷狗音质参数"""
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

    # ------------------------------------------------------------------ #
    # MusicProvider 抽象方法
    # ------------------------------------------------------------------ #

    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """搜索：1=单曲 10=专辑 100=歌手 1000=歌单（search.js）

        注意：gateway 搜索要求显式携带 token/userid 参数（可为空/0），
        否则返回 error_code=152。
        """
        ktype, path = self.SEARCH_TYPES.get(int(search_type), ("song", "/v3/search/song"))
        page = offset // max(limit, 1) + 1
        params = {
            "albumhide": 0,
            "iscorrection": 1,
            "keyword": keyword,
            "nocollect": 0,
            "page": page,
            "pagesize": limit,
            "platform": "AndroidFilter",
            "token": self.token,
            "userid": int(self.userid) if str(self.userid).isdigit() else 0,
        }
        data = await self._request(f"{self.GATEWAY}{path}", params=params,
                                   headers={"x-router": "complexsearch.kugou.com"})
        block = data.get("data", {}) or {}
        items = block.get("lists", []) or []

        songs = []
        for item in items:
            if ktype == "song":
                trans = item.get("trans_param") or {}
                pic = (item.get("Image") or item.get("img")
                       or trans.get("union_cover") or "")
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("FileHash") or item.get("hash", "")),
                    name=item.get("OriSongName") or item.get("song_name")
                         or item.get("FileName", ""),
                    artists=item.get("SingerName") or item.get("singer_name", ""),
                    album=item.get("AlbumName") or item.get("album_name", ""),
                    duration=(item.get("Duration") or item.get("duration", 0) or 0) * 1000,
                    pic_url=self._clean_cover(pic),
                    playable=True,
                ))
            elif ktype == "album":
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("albumid") or item.get("album_id", "")),
                    name=item.get("albumname") or item.get("album_name", ""),
                    artists=item.get("singer") or item.get("author_name", ""),
                    album="",
                    duration=0,
                    pic_url=self._clean_cover(
                        item.get("img") or item.get("sizable_cover")
                        or item.get("cover") or ""),
                    playable=False,
                ))
            elif ktype == "special":
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("gid")
                                or item.get("global_collection_id", "")),
                    name=item.get("specialname") or item.get("collection_name", ""),
                    artists=item.get("nickname") or item.get("creator_nickname", ""),
                    album="",
                    duration=0,
                    pic_url=self._clean_cover(
                        item.get("img") or item.get("sizable_cover")
                        or item.get("img_url") or ""),
                    playable=False,
                ))
            else:  # author
                songs.append(Song(
                    source=self.name,
                    song_id=str(item.get("AuthorID") or item.get("author_id", "")),
                    name=item.get("AuthorName") or item.get("author_name", ""),
                    artists="",
                    album="",
                    duration=0,
                    pic_url=self._clean_cover(
                        item.get("AuthorImg") or item.get("author_img")
                        or item.get("img") or ""),
                    playable=False,
                ))

        return SearchResult(source=self.name, keyword=keyword,
                            total=int(block.get("total", len(songs)) or len(songs)),
                            songs=songs)

    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放链接（/v5/url），带音质降级

        免费歌曲无需 cookie；VIP/付费歌曲未登录时无法获取完整直链，
        传入含 token/userid 的登录 cookie 后按账号权限返回。
        """
        song_hash = self._resolve_hash(song_id)
        await self._ensure_dfid()
        code = self._resolve_quality(level, quality)
        order = self.DEGRADE_ORDER
        if code in order:
            attempts = order[order.index(code):]
        else:  # 蝰蛇等特殊音质：先试本身，再按通用链降级
            attempts = [code] + order
        for try_code in attempts:
            entry = await self._get_music_url(song_hash, try_code)
            if entry:
                default_br, default_ext = self.FILE_CONFIG.get(try_code, (0, "mp3"))
                return SongUrl(
                    source=self.name,
                    song_id=song_hash,
                    url=entry["url"],
                    level=try_code,
                    quality_name=self.REVERSE_QUALITY_MAP.get(try_code, try_code),
                    br=int(entry.get("bitrate", 0) or default_br),
                    size=int(entry.get("bytes", 0) or 0),
                    type=str(entry.get("ext") or default_ext),
                    expired=False,
                )
        raise KugouAPIException(f"无法获取酷狗播放链接: {song_id} (VIP 歌曲需登录 cookie)")

    async def _get_music_url(self, song_hash: str, quality: str) -> Optional[dict]:
        """请求单个音质的播放链接（song_url.js /v5/url）

        响应为对象：status=1 成功（url 为多 CDN 数组），status=2 无权限。
        """
        params = {
            "album_id": 0,
            "area_code": 1,
            "hash": song_hash,
            "ssa_flag": "is_fromtrack",
            "version": 11430,
            "page_id": 151369488,
            "quality": quality,
            "album_audio_id": 0,
            "behavior": "play",
            "pid": 2,
            "cmd": 26,
            "pidversion": 3001,
            "IsFreePart": 0,
            "ppage_id": "463467626,350369493,788954147",
            "cdnBackup": 1,
            "module": "",
            "clientver": 11430,
        }
        data = await self._request(f"{self.GATEWAY}/v5/url", params=params,
                                   headers={"x-router": "trackercdn.kugou.com"},
                                   sign_key_hash=song_hash)
        if not isinstance(data, dict) or int(data.get("status", 0) or 0) != 1:
            return None
        urls = data.get("url") or []
        if not urls:
            return None
        return {"url": str(urls[0]).replace("http://", "https://", 1),
                "bytes": data.get("fileSize", 0),
                "ext": data.get("extName", ""),
                "bitrate": data.get("bitRate", 0)}

    async def get_song_detail(self, song_id: str) -> Song:
        """获取歌曲详情（privilege_lite.js /v2/get_res_privilege/lite）"""
        song_hash = self._resolve_hash(song_id)
        data = await self._request(
            f"{self.GATEWAY}/v2/get_res_privilege/lite",
            data={
                "appid": self.APPID,
                "area_code": 1,
                "behavior": "play",
                "clientver": self.CLIENTVER,
                "need_hash_offset": 1,
                "relate": 1,
                "support_verify": 1,
                "resource": [{"type": "audio", "page_id": 0,
                              "hash": song_hash, "album_id": 0}],
                "qualities": ["128", "320", "flac", "high"],
            },
            headers={"x-router": "media.store.kugou.com"},
        )
        items = data.get("data") or []
        if not items:
            raise KugouAPIException(f"酷狗歌曲不存在: {song_id}")
        item = items[0]
        info = item.get("info") or {}
        name = item.get("name", "")
        singer = item.get("singername", "")
        if singer and name.startswith(f"{singer} - "):
            name = name[len(singer) + 3:]
        return Song(
            source=self.name,
            song_id=str(item.get("hash", song_hash)),
            name=name,
            artists=singer,
            album=item.get("albumname", ""),
            duration=int(info.get("duration", 0) or 0),
            pic_url=self._clean_cover(info.get("image", "")),
            playable=int(item.get("pay_type", 0) or 0) == 0 or bool(self.token),
        )

    async def get_lyric(self, song_id: str) -> Lyric:
        """获取歌词：先 /v1/search 取 id+accesskey，再 /download 取 KRC 并转 LRC"""
        song_hash = self._resolve_hash(song_id)
        search = await self._request(
            f"{self.LYRICS}/v1/search",
            params={
                "album_audio_id": 0,
                "appid": self.APPID,
                "clientver": self.CLIENTVER,
                "duration": 0,
                "hash": song_hash,
                "keyword": "",
                "lrctxt": 1,
                "man": "no",
            },
            use_defaults=False,
        )
        candidates = search.get("candidates") or []
        if not candidates:
            raise KugouAPIException(f"酷狗歌词不存在: {song_id}")
        lyric_id = candidates[0].get("id", "")
        accesskey = candidates[0].get("accesskey", "")

        raw = await self._request(f"{self.LYRICS}/download", params={
            "ver": 1,
            "client": "android",
            "id": lyric_id,
            "accesskey": accesskey,
            "fmt": "krc",
            "charset": "utf8",
        })
        content = raw.get("content", "")
        if not content:
            raise KugouAPIException(f"酷狗歌词内容为空: {song_id}")
        if int(raw.get("contenttype", 0) or 0) != 0:
            text = base64.b64decode(content).decode("utf-8", "replace")
        else:
            text = self._decode_krc(content)
        return Lyric(source=self.name, song_id=song_hash,
                     lyric=self._krc_to_lrc(text), tlyric="")

    def _decode_krc(self, content_b64: str) -> str:
        """KRC 解码：base64 → 跳过 4 字节头 → XOR → zlib 解压（util.js decodeLyrics）"""
        try:
            data = bytearray(base64.b64decode(content_b64))
        except Exception as e:
            raise KugouAPIException(f"KRC base64 解码失败: {e}") from e
        body = data[4:]
        for i in range(len(body)):
            body[i] ^= self.KRC_KEY[i % len(self.KRC_KEY)]
        try:
            return zlib.decompress(bytes(body)).decode("utf-8", "replace")
        except zlib.error as e:
            raise KugouAPIException(f"KRC 解压失败: {e}") from e

    @staticmethod
    def _krc_to_lrc(krc_text: str) -> str:
        """KRC 逐字歌词 → 普通 LRC：[start,len](a,b,c)字(a,b,c)字 → [mm:ss.xx]歌词"""
        lines = []
        for raw in krc_text.splitlines():
            m = re.match(r"\[(\d+),(\d+)\](.*)", raw.strip())
            if not m:
                continue
            start_ms = int(m.group(1))
            text = re.sub(r"\(\d+,\d+,\d+\)", "", m.group(3))
            text = re.sub(r"<\d+,\d+,\d+(?:,\d+)?>", "", text).strip()
            if not text:
                continue
            minutes, seconds = divmod(start_ms, 60000)
            lines.append(f"[{minutes:02d}:{seconds / 1000:05.2f}]{text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 歌单 / 专辑（可选实现）
    # ------------------------------------------------------------------ #

    async def get_playlist(self, playlist_id: str, limit: int = 50) -> Playlist:
        """获取歌单详情（playlist_track_all.js，响应内含 list_info + songs）"""
        gid = playlist_id.strip()
        data = await self._request(
            f"{self.GATEWAY}/pubsongs/v2/get_other_list_file_nofilt",
            params={
                "area_code": 1,
                "begin_idx": 0,
                "plat": 1,
                "type": 1,
                "mode": 1,
                "personal_switch": 1,
                "extend_fields": "abtags,hot_cmt,popularization",
                "pagesize": limit,
                "global_collection_id": gid,
            },
        )
        block = data.get("data", {}) or {}
        info = block.get("list_info", {}) or {}
        tracks = [self._build_track_song(s) for s in block.get("songs", []) or []]

        return Playlist(
            source=self.name,
            playlist_id=str(info.get("global_collection_id", gid)),
            name=info.get("name", ""),
            cover_url=self._clean_cover(info.get("pic") or ""),
            creator=str(info.get("list_create_username", "")),
            description=info.get("intro", ""),
            tracks=tracks,
        )

    async def get_album(self, album_id: str) -> Album:
        """获取专辑详情（album_detail.js + album_songs.js）"""
        aid = re.sub(r"\D", "", album_id) or album_id.strip()
        aid_value = int(aid) if aid.isdigit() else aid
        detail_data = await self._request(
            f"{self.GATEWAY}/kmr/v2/albums",
            data={
                "data": [{"album_id": aid_value}],
                "is_buy": 0,
                "fields": ("album_id,album_name,publish_date,sizable_cover,intro,"
                           "language,is_publish,heat,type,quality,authors,exclusive,"
                           "author_name,trans_param"),
            },
            headers={"x-router": "openapi.kugou.com", "kg-tid": "255"},
        )
        raw_lists = detail_data.get("data", [])
        lists = raw_lists if isinstance(raw_lists, list) \
            else (raw_lists or {}).get("lists", []) or []
        info = lists[0] if lists else {}

        songs_data = await self._request(
            f"{self.GATEWAY}/v1/album_audio/lite",
            data={"album_id": aid_value, "is_buy": "", "page": 1, "pagesize": 50},
            headers={"x-router": "openapi.kugou.com", "kg-tid": "255"},
        )
        sblock = songs_data.get("data", {}) or {}
        items = sblock.get("songs") or sblock.get("audio_info") or []
        songs = [self._build_album_song(s) for s in items]

        publish_ts = 0
        publish_date = str(info.get("publish_date", "") or "")
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", publish_date)
        if m:
            publish_ts = int(time.mktime(
                time.strptime(publish_date, "%Y-%m-%d")) * 1000)

        return Album(
            source=self.name,
            album_id=str(info.get("album_id", aid)),
            name=info.get("album_name", ""),
            cover_url=self._clean_cover(
                info.get("sizable_cover") or info.get("cover") or ""),
            artist=info.get("author_name", ""),
            publish_time=publish_ts,
            songs=songs,
        )

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _build_track_song(self, raw: dict) -> Song:
        """将歌单接口曲目（filename 型 "歌手 - 歌名"）转换为统一 Song 模型"""
        name = raw.get("name", "")
        artists = raw.get("singer_name", "")
        if " - " in name:
            artists, _, name = name.partition(" - ")
        return Song(
            source=self.name,
            song_id=str(raw.get("hash", "")),
            name=name,
            artists=artists,
            album=(raw.get("albuminfo") or {}).get("album_name", "")
                  or raw.get("album_name", ""),
            duration=(raw.get("timelen", 0) or 0) * 1000,
            pic_url=self._clean_cover(raw.get("cover") or raw.get("img") or ""),
            playable=True,
        )

    def _build_album_song(self, raw: dict) -> Song:
        """将专辑接口曲目（base/audio_info/album_info 嵌套结构）转为统一 Song"""
        base = raw.get("base", {}) or {}
        audio = raw.get("audio_info", {}) or {}
        album = raw.get("album_info", {}) or {}
        artists = "/".join(a.get("author_name", "")
                           for a in raw.get("authors", []) or [] if a.get("author_name"))
        return Song(
            source=self.name,
            song_id=str(audio.get("hash", "")),
            name=base.get("audio_name", ""),
            artists=artists or base.get("author_name", ""),
            album=album.get("album_name", ""),
            duration=int(audio.get("duration", 0) or 0),
            pic_url=self._clean_cover(album.get("cover", "")),
            playable=True,
        )

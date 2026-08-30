"""WhiteAPI 安全中间件模块

提供三类安全能力：
1. IP 白名单 —— 通过环境变量 ALLOWED_IPS 控制访问来源，127.0.0.1 / ::1 永远放行
2. 速率限制 —— 每 IP 每秒最多 30 次请求（线程安全）
3. SSRF 短链防护 —— 解析网易云短链时只允许重定向到 music.163.com，并提取歌曲 ID

用法（在 FastAPI 中间件中）：
    from app.security import is_ip_allowed, check_rate_limit, extract_music_id
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List
from urllib.parse import unquote, urlparse

import httpx

# ============ 配置常量 ============

RATE_LIMIT = 30          # 每 IP 每秒最大请求数
RATE_WINDOW = 1          # 速率限制窗口（秒）
RATE_CLEANUP = 60        # 超过该秒数未活动的 IP 条目被清理
MAX_IPS = 1000           # 触发全量清理的 IP 数量阈值

ALLOWED_IPS_ENV = "ALLOWED_IPS"

# ============ 1. IP 白名单 ============

# 永远放行的本地回环地址
_ALWAYS_ALLOWED = {"127.0.0.1", "::1"}


def get_allowed_ips() -> List[str]:
    """从环境变量 ALLOWED_IPS 读取白名单 IP 列表（逗号分隔）。

    返回去除空白后的 IP 列表；未设置或为空时返回空列表（表示不限制）。
    """
    raw = os.environ.get(ALLOWED_IPS_ENV, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def is_ip_allowed(ip: str) -> bool:
    """判断指定 IP 是否允许访问。

    规则：
    - 127.0.0.1 与 ::1 永远放行；
    - 未配置 ALLOWED_IPS 时不做限制（全部放行）；
    - 配置了白名单时，仅白名单内的 IP 放行。
    """
    if ip in _ALWAYS_ALLOWED:
        return True
    allowed = get_allowed_ips()
    if not allowed:
        return True
    return ip in allowed


# ============ 2. 速率限制（线程安全） ============


class RateLimiter:
    """基于滑动时间窗口的每 IP 速率限制器。

    每个 IP 维护一个时间戳列表，1 秒窗口内最多 RATE_LIMIT 次请求。
    使用 threading.Lock 保证多线程安全。

    特点：
    - 每个 IP 首次访问时按需创建记录；
    - 超过 RATE_CLEANUP 秒未活动的 IP 条目在下次访问该 IP 时被删除；
    - 记录 IP 数超过 MAX_IPS 时执行一次全量清理。
    """

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW,
                 cleanup: float = RATE_CLEANUP, max_ips: int = MAX_IPS):
        """初始化限流器。

        Args:
            limit: 窗口内最大请求数
            window: 窗口长度（秒）
            cleanup: IP 空闲多久后清理（秒）
            max_ips: 超过该数量时触发全量清理
        """
        self.limit = limit
        self.window = window
        self.cleanup = cleanup
        self.max_ips = max_ips
        self._records: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _prune_record(self, ip: str, now: float) -> bool:
        """清理单个 IP 的过期时间戳。

        返回该 IP 记录是否仍应保留（True=保留，False=已删除）。
        """
        timestamps = self._records.get(ip)
        if not timestamps:
            if ip in self._records:
                del self._records[ip]
            return False
        if now - timestamps[-1] > self.cleanup:
            del self._records[ip]
            return False
        # 只保留窗口内的时间戳
        self._records[ip] = [t for t in timestamps if now - t < self.window]
        return True

    def _full_cleanup(self, now: float) -> None:
        """全量清理超过 cleanup 秒未活动的 IP 条目。"""
        stale = [k for k, v in self._records.items() if v and now - v[-1] > self.cleanup]
        for k in stale:
            del self._records[k]

    def allow(self, ip: str) -> bool:
        """判断并记录一次请求。

        Args:
            ip: 请求来源 IP

        Returns:
            True 表示允许请求（并已记录），False 表示超过速率限制。
        """
        now = time.time()
        with self._lock:
            # 单个 IP 长时间未活动时清理其记录
            self._prune_record(ip, now)
            # IP 数量过多时做一次全量清理
            if len(self._records) > self.max_ips:
                self._full_cleanup(now)

            if ip not in self._records:
                self._records[ip] = []
            timestamps = self._records[ip]
            timestamps[:] = [t for t in timestamps if now - t < self.window]
            if len(timestamps) >= self.limit:
                return False
            timestamps.append(now)
            return True


# 模块级默认限流器，供 check_rate_limit 复用
_rate_limiter = RateLimiter()


def check_rate_limit(ip: str) -> bool:
    """可复用的速率限制检查函数（True=允许, False=拒绝）。

    Args:
        ip: 请求来源 IP

    Returns:
        是否允许本次请求。
    """
    return _rate_limiter.allow(ip)


# ============ 3. SSRF 短链防护 ============

# 网易云短链域名
_SHORT_LINK_HOST = "163cn.tv"
# 网易云音乐官方域名（短链只允许重定向到这里）
_MUSIC_HOST = "music.163.com"


def _resolve_short_link(url: str) -> str:
    """解析网易云短链，获取最终重定向地址。

    使用 httpx 发起 GET 且不跟随重定向（allow_redirects=False），读取 Location 头。

    SSRF 防护：若 Location 指向的域名不是 music.163.com（含其子域），
    或无法解析，直接返回空字符串拒绝。

    Args:
        url: 网易云短链地址

    Returns:
        重定向后的完整 URL；不合法或被拒绝时返回空字符串。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; WhiteAPI)"}
        with httpx.Client(timeout=10, follow_redirects=False, headers=headers) as client:
            resp = client.get(url)
            location = resp.headers.get("Location", "")
        if not location:
            return ""
        host = urlparse(location).hostname
        if not host:
            return ""
        # 只允许解析到 music.163.com 或其子域，拒绝其他任何域名（SSRF 防护）
        if host != _MUSIC_HOST and not host.endswith("." + _MUSIC_HOST):
            return ""
        return location
    except Exception:
        return ""


def _extract_id_from_music_url(url: str) -> str:
    """从 music.163.com 的 URL 中提取 id= 后的数字（截取到 & 为止）。

    未找到 id= 时返回空字符串。

    Args:
        url: 包含 music.163.com 的 URL

    Returns:
        提取到的歌曲/歌单 ID；未找到时返回空字符串。
    """
    index = url.find("id=")
    if index == -1:
        return ""
    return url[index + 3:].split("&")[0]


def resolve_music_url(url: str) -> str:
    """解析网易云短链或歌曲 URL，提取歌曲 ID。

    处理逻辑：
    1. 若 URL 含 163cn.tv：发起 GET（allow_redirects=False）读取 Location 头；
       若 Location 域名不含 music.163.com 则拒绝返回空字符串；否则继续按
       music.163.com 的 id= 逻辑提取；
    2. 若 URL 含 music.163.com：提取 id= 后的数字（截到 & 为止）；
    3. 其他情况返回空字符串。

    Args:
        url: 网易云短链或歌曲 URL

    Returns:
        提取到的 ID；无法解析或被拒绝时返回空字符串。
    """
    if not url:
        return ""
    if _SHORT_LINK_HOST in url:
        resolved = _resolve_short_link(url)
        if not resolved:
            return ""
        url = resolved
    if _MUSIC_HOST in url:
        return _extract_id_from_music_url(url)
    return ""


def extract_music_id(id_or_url: str) -> str:
    """对外主函数：先处理短链再提取歌曲 ID。

    对输入先做 URL 解码，再调用 resolve_music_url 完成短链解析与 ID 提取。
    若输入本身就是一个纯数字 ID，直接原样返回。

    Args:
        id_or_url: 歌曲 ID 或网易云短链/歌曲 URL

    Returns:
        提取到的歌曲 ID；无法解析时返回空字符串。
    """
    if not id_or_url:
        return ""
    decoded = unquote(str(id_or_url)).strip()
    if decoded.isdigit():
        return decoded
    return resolve_music_url(decoded)

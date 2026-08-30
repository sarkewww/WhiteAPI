"""Provider 抽象基类

所有音乐平台 provider 必须继承 MusicProvider 并实现统一接口，
WhiteAPI 通过统一的 /search /song /playlist /album 等端点对外提供聚合服务。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.models import Album, Lyric, Playlist, SearchResult, Song, SongUrl


class MusicProvider(ABC):
    """音乐平台 provider 抽象基类"""

    name: str = "base"
    display_name: str = "Base"

    def __init__(self, cookies: str = ""):
        self.cookies = cookies

    @abstractmethod
    async def search(self, keyword: str, limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> SearchResult:
        """统一搜索
        search_type: 1=单曲 10=专辑 100=歌手 1000=歌单
        """

    @abstractmethod
    async def get_song_url(self, song_id: str, level: str = "lossless",
                           quality: str = "") -> SongUrl:
        """获取播放/下载直链"""

    @abstractmethod
    async def get_song_detail(self, song_id: str) -> Song:
        """获取歌曲详情"""

    @abstractmethod
    async def get_lyric(self, song_id: str) -> Lyric:
        """获取歌词"""

    async def get_playlist(self, playlist_id: str) -> Playlist:
        """获取歌单详情（可选实现）"""
        raise NotImplementedError(f"{self.name} 不支持歌单")

    async def get_album(self, album_id: str) -> Album:
        """获取专辑详情（可选实现）"""
        raise NotImplementedError(f"{self.name} 不支持专辑")

    @staticmethod
    def _normalize_song(raw: dict, source: str) -> Song:
        """各平台把原始数据转换为统一 Song 模型"""
        return Song(
            source=source,
            song_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            artists=raw.get("artists", ""),
            album=raw.get("album", ""),
            duration=int(raw.get("duration", 0) or 0),
            pic_url=raw.get("picUrl", ""),
            playable=raw.get("playable", True),
        )

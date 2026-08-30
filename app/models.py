"""统一数据模型（所有 provider 返回统一结构）"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Song(BaseModel):
    """统一歌曲信息"""
    source: str = Field(description="来源平台, 如 netease/qq/kugou")
    song_id: str = Field(description="歌曲在该平台的 ID")
    name: str = Field(description="歌曲名")
    artists: str = Field(default="", description="歌手, 多个用 / 分隔")
    album: str = Field(default="", description="专辑名")
    duration: int = Field(default=0, description="时长(毫秒)")
    pic_url: str = Field(default="", description="封面图 URL")
    playable: bool = Field(default=True, description="是否可播放")


class SongUrl(BaseModel):
    """统一播放/下载链接"""
    source: str
    song_id: str
    url: str = Field(description="音频直链")
    level: str = Field(default="", description="实际音质等级")
    quality_name: str = Field(default="", description="音质中文名")
    br: int = Field(default=0, description="码率")
    size: int = Field(default=0, description="文件大小(字节)")
    type: str = Field(default="mp3", description="文件格式")
    expired: bool = Field(default=False, description="是否已过期")


class Lyric(BaseModel):
    """统一歌词"""
    source: str
    song_id: str
    lyric: str = Field(default="", description="原文歌词(LRC)")
    tlyric: str = Field(default="", description="翻译歌词(LRC)")


class Playlist(BaseModel):
    """统一歌单信息"""
    source: str
    playlist_id: str
    name: str
    cover_url: str = Field(default="")
    creator: str = Field(default="")
    description: str = Field(default="")
    tracks: list[Song] = Field(default_factory=list)


class Album(BaseModel):
    """统一专辑信息"""
    source: str
    album_id: str
    name: str
    cover_url: str = Field(default="")
    artist: str = Field(default="")
    publish_time: int = Field(default=0)
    songs: list[Song] = Field(default_factory=list)


class SearchResult(BaseModel):
    """统一搜索结果"""
    source: str
    keyword: str
    total: int = 0
    songs: list[Song] = Field(default_factory=list)


class ApiResponse(BaseModel):
    """统一 API 响应包裹"""
    status: int = 200
    success: bool = True
    message: str = "ok"
    data: Optional[dict] = None

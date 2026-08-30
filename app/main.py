"""WhiteAPI - 聚合音乐 API 服务

统一接口（/search, /song/url, /song/detail, /lyric, /playlist, /album）对接多个音乐平台。
支持 provider 参数指定平台，或 all 并发聚合所有平台。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.models import Album, ApiResponse, Lyric, Playlist, SearchResult, Song, SongUrl
from app.providers import get_provider, list_providers, load_builtin_providers

load_builtin_providers()

app = FastAPI(title="WhiteAPI", version="0.1.0",
              description="聚合音乐 API - 统一搜索/下载/歌词接口")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _ok(data: dict = None, message: str = "ok") -> dict:
    return ApiResponse(status=200, success=True, message=message, data=data).model_dump()


def _err(message: str, status: int = 400) -> dict:
    return ApiResponse(status=status, success=False, message=message).model_dump()


async def _call_provider(name: str, method: str, **kwargs) -> Optional[list | dict]:
    """调用单个 provider 的方法"""
    provider = get_provider(name)
    if not provider:
        return None
    try:
        result = await getattr(provider, method)(**kwargs)
        return result
    except NotImplementedError:
        return None
    except Exception as e:
        return None


async def _call_all(method: str, **kwargs) -> list:
    """并发调用所有 provider 的同一方法"""
    providers = [p["name"] for p in list_providers()]
    results = await asyncio.gather(
        *[_call_provider(p, method, **kwargs) for p in providers],
        return_exceptions=True,
    )
    ret = []
    for p, r in zip(providers, results):
        if r and not isinstance(r, BaseException):
            ret.append(r)
    return ret


# ============ API 路由 ============

@app.get("/health")
async def health():
    return _ok({"providers": list_providers()})


@app.get("/providers")
async def providers():
    return _ok({"providers": list_providers()})


@app.get("/search", summary="统一搜索")
async def search(
    keyword: str = Query(..., description="搜索关键词"),
    provider: str = Query("all", description="all 并发所有平台，或指定单个平台名"),
    limit: int = Query(10, ge=1, le=100),
    search_type: int = Query(1, description="1=单曲 10=专辑 100=歌手 1000=歌单"),
    offset: int = Query(0),
):
    if provider == "all":
        results = await _call_all("search", keyword=keyword, limit=limit,
                                  search_type=search_type, offset=offset)
        songs = []
        for r in results:
            if isinstance(r, SearchResult):
                songs.extend(r.songs)
        return _ok({"keyword": keyword, "total": len(songs), "songs": [s.model_dump() for s in songs]})
    else:
        p = get_provider(provider)
        if not p:
            raise HTTPException(404, f"不支持的 provider: {provider}")
        try:
            result = await p.search(keyword=keyword, limit=limit,
                                    search_type=search_type, offset=offset)
            return _ok({"keyword": keyword, "total": result.total, "songs": [s.model_dump() for s in result.songs]})
        except Exception as e:
            raise HTTPException(502, f"{provider} 搜索失败: {e}")


@app.get("/song/url", summary="获取播放/下载直链")
async def song_url(
    song_id: str = Query(..., description="歌曲 ID"),
    provider: str = Query(..., description="平台名"),
    level: str = Query("lossless", description="音质: standard/exhigh/lossless/hires"),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {provider}")
    try:
        result = await p.get_song_url(song_id=song_id, level=level)
        return _ok(result.model_dump())
    except Exception as e:
        raise HTTPException(502, f"获取播放链接失败: {e}")


@app.get("/song/detail", summary="获取歌曲详情")
async def song_detail(
    song_id: str = Query(..., description="歌曲 ID"),
    provider: str = Query(..., description="平台名"),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {provider}")
    try:
        result = await p.get_song_detail(song_id=song_id)
        return _ok(result.model_dump())
    except Exception as e:
        raise HTTPException(502, f"获取歌曲详情失败: {e}")


@app.get("/lyric", summary="获取歌词")
async def lyric(
    song_id: str = Query(..., description="歌曲 ID"),
    provider: str = Query(..., description="平台名"),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {provider}")
    try:
        result = await p.get_lyric(song_id=song_id)
        return _ok(result.model_dump())
    except Exception as e:
        raise HTTPException(502, f"获取歌词失败: {e}")


@app.get("/playlist", summary="获取歌单详情")
async def playlist(
    playlist_id: str = Query(..., description="歌单 ID"),
    provider: str = Query(..., description="平台名"),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {provider}")
    try:
        result = await p.get_playlist(playlist_id=playlist_id)
        return _ok(result.model_dump())
    except NotImplementedError:
        raise HTTPException(501, f"{provider} 不支持歌单")
    except Exception as e:
        raise HTTPException(502, f"获取歌单失败: {e}")


@app.get("/album", summary="获取专辑详情")
async def album(
    album_id: str = Query(..., description="专辑 ID"),
    provider: str = Query(..., description="平台名"),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {provider}")
    try:
        result = await p.get_album(album_id=album_id)
        return _ok(result.model_dump())
    except NotImplementedError:
        raise HTTPException(501, f"{provider} 不支持专辑")
    except Exception as e:
        raise HTTPException(502, f"获取专辑失败: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=5000, reload=True)
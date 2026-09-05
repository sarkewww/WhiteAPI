"""WhiteAPI - 聚合音乐 API 服务

统一接口对接多个音乐平台，支持搜索、直链、歌词、下载、批量下载。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from app.models import ApiResponse
from app.providers import get_provider, list_providers, load_builtin_providers
from app.security import check_rate_limit, extract_music_id, is_ip_allowed
from app.downloader import MusicDownloader
from app.batch import BatchTaskManager

load_builtin_providers()

downloader = MusicDownloader(download_dir=os.environ.get("DOWNLOAD_DIR", "downloads"))
batch_mgr = BatchTaskManager(downloader)

app = FastAPI(title="WhiteAPI", version="0.1.2",
              description="聚合音乐 API - 统一搜索/下载/歌词接口")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============ 安全中间件 ============

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    ip = request.client.host if request.client else "0.0.0.0"

    if not is_ip_allowed(ip):
        return Response("禁止访问", status_code=403)

    if not check_rate_limit(ip):
        return Response("请求过于频繁", status_code=429)

    return await call_next(request)


# ============ 工具函数 ============

def _ok(data: dict = None, message: str = "ok") -> dict:
    return ApiResponse(status=200, success=True, message=message, data=data).model_dump()


def _err(message: str, status: int = 400) -> dict:
    return ApiResponse(status=status, success=False, message=message).model_dump()


async def _call_provider(name: str, method: str, **kwargs):
    provider = get_provider(name)
    if not provider:
        return None
    try:
        return await getattr(provider, method)(**kwargs)
    except (NotImplementedError, Exception):
        return None


async def _call_all(method: str, **kwargs) -> list:
    providers = [p["name"] for p in list_providers()]
    results = await asyncio.gather(
        *[_call_provider(p, method, **kwargs) for p in providers],
        return_exceptions=True,
    )
    return [r for r in results if r and not isinstance(r, BaseException)]


def _get_provider(name: str):
    p = get_provider(name)
    if not p:
        raise HTTPException(404, f"不支持的 provider: {name}")
    return p


# ============ 基础路由 ============

@app.get("/health")
async def health():
    return _ok({"providers": list_providers(), "download_dir": str(downloader.download_dir),
                "allowed_ips": os.environ.get("ALLOWED_IPS", "")})


@app.get("/providers")
async def providers():
    return _ok({"providers": list_providers()})


# ============ 搜索 / 歌曲 / 歌词 ============

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
            if hasattr(r, "songs"):
                songs.extend(r.songs)
        return _ok({"keyword": keyword, "total": len(songs), "songs": [s.model_dump() for s in songs]})
    p = _get_provider(provider)
    try:
        result = await p.search(keyword=keyword, limit=limit, search_type=search_type, offset=offset)
        return _ok({"keyword": keyword, "total": result.total, "songs": [s.model_dump() for s in result.songs]})
    except Exception as e:
        raise HTTPException(502, f"{provider} 搜索失败: {e}")


@app.get("/song/url", summary="获取播放/下载直链")
async def song_url(
    song_id: str = Query(..., description="歌曲 ID"),
    provider: str = Query(..., description="平台名"),
    level: str = Query("lossless", description="音质: standard/exhigh/lossless/hires"),
):
    p = _get_provider(provider)
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
    p = _get_provider(provider)
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
    p = _get_provider(provider)
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
    p = _get_provider(provider)
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
    p = _get_provider(provider)
    try:
        result = await p.get_album(album_id=album_id)
        return _ok(result.model_dump())
    except NotImplementedError:
        raise HTTPException(501, f"{provider} 不支持专辑")
    except Exception as e:
        raise HTTPException(502, f"获取专辑失败: {e}")


# ============ 下载（单曲） ============

@app.get("/download", summary="下载单曲文件（支持音质/断点续传/标签写入）")
async def download_song(
    song_id: str = Query(..., description="歌曲 ID 或网易云 URL"),
    provider: str = Query("netease", description="平台名"),
    level: str = Query("standard", description="音质: standard/exhigh/lossless/hires"),
    format: str = Query("file", description="返回格式: file=文件流, json=下载信息"),
):
    final_id = extract_music_id(song_id) or song_id
    p = _get_provider(provider)
    try:
        result = await downloader.download_music_file(p, int(final_id), quality=level)
        if not result.success:
            raise HTTPException(502, result.error_message)
        if format == "json":
            info = result.music_info
            return _ok({"song_id": final_id, "file_path": result.file_path,
                        "file_size": result.file_size, "name": info.name if info else "",
                        "artists": info.artists if info else ""})
        return FileResponse(result.file_path, filename=Path(result.file_path).name,
                            media_type="application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"下载失败: {e}")


# ============ 批量下载（歌单） ============

@app.post("/playlist/download/start", summary="开始歌单批量下载")
async def playlist_download_start(
    playlist_id: str = Query(..., description="歌单 ID"),
    provider: str = Query("netease", description="平台名"),
    level: str = Query("standard", description="音质"),
):
    p = _get_provider(provider)
    try:
        pl = await p.get_playlist(playlist_id=playlist_id)
        if not pl or not pl.tracks:
            raise HTTPException(404, "歌单为空或不存在")
        tracks = [{"id": int(t.song_id), "name": t.name, "artists": t.artists} for t in pl.tracks]
        pl_info = {"name": pl.name, "creator": pl.creator}
        task_id = batch_mgr.create_task(tracks, pl_info, level, p)
        return _ok({"task_id": task_id, "total": len(tracks), "playlist": pl.name})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"创建批量下载任务失败: {e}")


@app.get("/playlist/download/progress", summary="查询批量下载进度")
async def playlist_download_progress(
    task_id: str = Query(..., description="任务 ID"),
):
    progress = await batch_mgr.get_progress(task_id)
    if not progress:
        raise HTTPException(404, "任务不存在或已过期")
    return _ok(progress)


@app.get("/playlist/download/result", summary="获取批量下载结果（ZIP）")
async def playlist_download_result(
    task_id: str = Query(..., description="任务 ID"),
):
    result = await batch_mgr.get_result(task_id)
    if not result:
        raise HTTPException(404, "任务未完成或不存在")
    zip_buffer, zip_filename, _ = result
    ascii_name = zip_filename.encode("ascii", errors="replace").decode("ascii")
    return Response(content=zip_buffer.read(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{ascii_name}"'})


@app.post("/playlist/download/cancel", summary="取消批量下载任务")
async def playlist_download_cancel(
    task_id: str = Query(..., description="任务 ID"),
):
    ok = await batch_mgr.cancel(task_id)
    return _ok({"cancelled": ok}, "已取消" if ok else "取消失败（任务不存在或已完成）")


# ============ 批量下载（专辑） ============

@app.post("/album/download/start", summary="开始专辑批量下载")
async def album_download_start(
    album_id: str = Query(..., description="专辑 ID"),
    provider: str = Query("netease", description="平台名"),
    level: str = Query("standard", description="音质"),
):
    p = _get_provider(provider)
    try:
        al = await p.get_album(album_id=album_id)
        if not al or not al.songs:
            raise HTTPException(404, "专辑为空或不存在")
        tracks = [{"id": int(t.song_id), "name": t.name, "artists": t.artists} for t in al.songs]
        pl_info = {"name": al.name, "creator": al.artist}
        task_id = batch_mgr.create_task(tracks, pl_info, level, p)
        return _ok({"task_id": task_id, "total": len(tracks), "album": al.name})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"创建批量下载任务失败: {e}")


@app.get("/album/download/progress", summary="查询专辑批量下载进度")
async def album_download_progress(
    task_id: str = Query(..., description="任务 ID"),
):
    return await playlist_download_progress(task_id)


@app.get("/album/download/result", summary="获取专辑批量下载结果（ZIP）")
async def album_download_result(
    task_id: str = Query(..., description="任务 ID"),
):
    return await playlist_download_result(task_id)


@app.post("/album/download/cancel", summary="取消专辑批量下载任务")
async def album_download_cancel(
    task_id: str = Query(..., description="任务 ID"),
):
    return await playlist_download_cancel(task_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=5000, reload=True)
"""音乐下载器模块（WhiteAPI 适配版）

从 Netease_url 项目的 music_downloader.py 移植，适配 WhiteAPI 的
provider 模式：不再直接依赖 NeteaseAPI/CookieManager，而是通过
MusicProvider 的统一异步接口（get_song_url / get_song_detail / get_lyric）
获取音乐信息，HTTP 下载改用 httpx。

功能包括:
- 通用工具函数（文件名安全处理、速度/时间格式化、歌词合并、ZIP打包等）
- 音乐信息获取
- 文件下载到本地（支持断点续传）
- 音乐标签写入（mutagen: mp3/flac/m4a）
"""

from __future__ import annotations

import re
import time
import logging
import zipfile
from io import BytesIO
from typing import Callable, Dict, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass

import httpx
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC
from mutagen.mp4 import MP4

from app.providers.base import MusicProvider

_logger = logging.getLogger('music_downloader')


# ==================== 通用工具函数 ====================

VALID_LEVELS = ['standard', 'exhigh', 'lossless', 'hires', 'sky', 'dolby', 'jyeffect', 'jymaster']
ILLEGAL_CHARS = r'<>:"/\|?*'


def safe_filename(name: str) -> str:
    """移除文件名中的非法字符"""
    return ''.join(c for c in (name or 'file') if c not in ILLEGAL_CHARS)


def format_speed(bytes_per_sec: int) -> str:
    """格式化下载速度"""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def format_eta(seconds: float) -> str:
    """格式化剩余时间"""
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds) // 60}分{int(seconds) % 60}秒"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}时{m}分"


def merge_translation_lyric(lrc: str, tlyric: str) -> str:
    """把翻译歌词合并到原歌词（按时间轴对齐）"""
    if not tlyric:
        return lrc

    def parse_time_tag(line: str):
        m = re.match(r'\[(\d+):(\d+[\.:]?\d*)\]', line)
        if m:
            return int(m.group(1)) * 60 + float(m.group(2).replace(':', '.'))
        return None

    lrc_lines = lrc.strip().split('\n')
    tlyric_lines = tlyric.strip().split('\n')
    tlyric_map = {}
    for line in tlyric_lines:
        t = parse_time_tag(line)
        if t is not None:
            text = re.sub(r'\[\d+:\d+[\.:]?\d*\]', '', line).strip()
            tlyric_map[t] = text
    result = []
    for line in lrc_lines:
        t = parse_time_tag(line)
        text = re.sub(r'\[\d+:\d+[\.:]?\d*\]', '', line).strip()
        if t is not None and t in tlyric_map and tlyric_map[t]:
            tag = re.match(r'(\[\d+:\d+[\.:]?\d*\])', line)
            if tag:
                result.append(f"{tag.group(1)}{text} (翻译: {tlyric_map[t]})")
                continue
        result.append(line)
    return '\n'.join(result)


def make_zip_response(files_dir: Path, zip_name: str) -> Tuple[BytesIO, str]:
    """把目录下所有文件打包为 ZIP 返回 (BytesIO, 文件名)"""
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files_dir.iterdir()):
            zf.write(str(f), f.name)
    zip_buf.seek(0)
    return zip_buf, safe_filename(zip_name) + '.zip'


# ==================== 音乐下载相关类 ====================


@dataclass
class MusicInfo:
    """音乐信息数据类"""
    id: int
    name: str
    artists: str
    album: str
    pic_url: str
    duration: int
    track_number: int
    download_url: str
    file_type: str
    file_size: int
    quality: str
    lyric: str = ""
    tlyric: str = ""


@dataclass
class DownloadResult:
    """下载结果数据类"""
    success: bool
    file_path: Optional[str] = None
    file_size: int = 0
    error_message: str = ""
    music_info: Optional[MusicInfo] = None


class DownloadException(Exception):
    """下载异常类"""
    pass


class MusicDownloader:
    """音乐下载器主类（适配 MusicProvider）"""

    def __init__(self, download_dir: str = "downloads", max_concurrent: int = 3):
        """
        初始化音乐下载器

        Args:
            download_dir: 下载目录
            max_concurrent: 最大并发下载数（batch 任务自行控制，此处保留兼容）
        """
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)
        self.max_concurrent = max_concurrent
        self._cover_cache: Dict[str, bytes] = {}  # URL -> bytes

    # ---------- 内部工具 ----------

    async def _fetch_cover(self, pic_url: str) -> Optional[bytes]:
        """下载封面图（带缓存，超过 500 条清理最旧）"""
        if not pic_url:
            return None
        if pic_url in self._cover_cache:
            return self._cover_cache[pic_url]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(pic_url)
                r.raise_for_status()
            self._cover_cache[pic_url] = r.content
            if len(self._cover_cache) > 500:
                self._cover_cache.pop(next(iter(self._cover_cache)))
            return r.content
        except Exception as e:
            _logger.warning(f"封面下载失败 ({pic_url}): {e}")
            return None

    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名，移除非法字符、前后空格/点，限制 200 字符"""
        filename = safe_filename(filename)
        filename = filename.strip(' .')
        if len(filename) > 200:
            filename = filename[:200]
        return filename or "unknown"

    def _determine_file_extension(self, url: str, content_type: str = "") -> str:
        """根据URL和Content-Type确定文件扩展名"""
        if '.flac' in url.lower():
            return '.flac'
        elif '.mp3' in url.lower():
            return '.mp3'
        elif '.m4a' in url.lower():
            return '.m4a'

        content_type = content_type.lower()
        if 'flac' in content_type:
            return '.flac'
        elif 'mpeg' in content_type or 'mp3' in content_type:
            return '.mp3'
        elif 'mp4' in content_type or 'm4a' in content_type:
            return '.m4a'

        return '.mp3'  # 默认

    # ---------- 信息获取 / 下载 ----------

    async def get_music_info(self, provider: MusicProvider, music_id: int,
                             quality: str = "standard", cookies: str = "") -> MusicInfo:
        """获取音乐详细信息

        Args:
            provider: 音乐平台 provider
            music_id: 音乐ID
            quality: 音质等级
            cookies: cookie 字符串

        Returns:
            音乐信息对象

        Raises:
            DownloadException: 获取信息失败时抛出
        """
        try:
            # 获取音乐URL信息（SongUrl 统一模型）
            url_result = await provider.get_song_url(song_id=str(music_id), level=quality)
            if not url_result or not url_result.url:
                raise DownloadException(f"无法获取音乐ID {music_id} 的播放链接")
            download_url = url_result.url

            # 获取音乐详情（Song 统一模型）
            song_detail = await provider.get_song_detail(str(music_id))
            if not song_detail:
                raise DownloadException(f"无法获取音乐ID {music_id} 的详细信息")

            # 获取歌词（Lyric 统一模型）
            try:
                lyric_obj = await provider.get_lyric(str(music_id))
            except Exception:
                lyric_obj = None
            lyric = lyric_obj.lyric if lyric_obj else ""
            tlyric = lyric_obj.tlyric if lyric_obj else ""

            return MusicInfo(
                id=music_id,
                name=song_detail.name or '未知歌曲',
                artists=song_detail.artists or '未知艺术家',
                album=song_detail.album or '未知专辑',
                pic_url=song_detail.pic_url or '',
                duration=(song_detail.duration or 0) // 1000,  # 毫秒 -> 秒
                track_number=0,
                download_url=download_url,
                file_type=(url_result.type or 'mp3').lower(),
                file_size=url_result.size or 0,
                quality=quality,
                lyric=lyric,
                tlyric=tlyric,
            )
        except DownloadException:
            raise
        except Exception as e:
            raise DownloadException(f"获取音乐信息时发生错误: {e}")

    async def download_music_file(self, provider: MusicProvider, music_id: int,
                                  quality: str = "standard",
                                  progress_callback: Optional[Callable[[int, int, int], None]] = None,
                                  cookies: str = "") -> DownloadResult:
        """下载音乐文件到本地（支持断点续传）

        Args:
            provider: 音乐平台 provider
            music_id: 音乐ID
            quality: 音质等级
            progress_callback: 进度回调 callback(downloaded, total_size, speed)
            cookies: cookie 字符串
        """
        try:
            music_info = await self.get_music_info(provider, music_id, quality, cookies)
            filename = f"{music_info.artists} - {music_info.name}"
            sfilename = self._sanitize_filename(filename)
            file_ext = self._determine_file_extension(music_info.download_url, music_info.file_type)
            file_path = self.download_dir / f"{sfilename}{file_ext}"
            part_path = file_path.with_suffix(file_path.suffix + '.part')

            if file_path.exists():
                if progress_callback:
                    fs = file_path.stat().st_size
                    progress_callback(fs, fs, 0)
                return DownloadResult(success=True, file_path=str(file_path),
                                      file_size=file_path.stat().st_size, music_info=music_info)

            # 断点续传: 检查 .part 文件
            downloaded = 0
            if part_path.exists():
                downloaded = part_path.stat().st_size

            headers = {}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'

            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream('GET', music_info.download_url, headers=headers) as response:
                    total_size = int(response.headers.get('content-length', music_info.file_size or 0))
                    if downloaded > 0 and response.status_code == 206:
                        # content-length 可能是剩余大小也可能是完整大小，与完整大小比较取较小值避免重复计数
                        if music_info.file_size:
                            total_size = min(total_size + downloaded, music_info.file_size)
                        else:
                            total_size += downloaded
                    elif response.status_code == 200 and downloaded > 0:
                        downloaded = 0
                        try:
                            part_path.unlink(missing_ok=True)
                        except OSError as e:
                            _logger.warning(f"删除残留 .part 文件失败: {e}")

                    response.raise_for_status()
                    mode = 'ab' if downloaded > 0 else 'wb'
                    base_offset = downloaded

                    if progress_callback:
                        start_time = time.time()
                        last_time = start_time
                        with open(part_path, mode) as f:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    if now - last_time >= 0.3:
                                        elapsed = now - start_time
                                        speed = int(downloaded / elapsed) if elapsed > 0 else 0
                                        progress_callback(downloaded - base_offset, total_size, speed)
                                        last_time = now
                            if downloaded > 0:
                                elapsed = time.time() - start_time
                                speed = int(downloaded / elapsed) if elapsed > 0 else 0
                                progress_callback(downloaded - base_offset, max(total_size, downloaded), speed)
                    else:
                        with open(part_path, mode) as f:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)

            # 下载完成, 重命名 .part -> 正式文件
            if part_path.exists():
                if file_path.exists():
                    file_path.unlink()
                part_path.rename(file_path)

            if file_path.stat().st_size == 0:
                file_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error_message="下载文件为空，请重试")

            await self._write_music_tags(file_path, music_info)
            return DownloadResult(success=True, file_path=str(file_path),
                                  file_size=file_path.stat().st_size, music_info=music_info)
        except DownloadException:
            raise
        except httpx.HTTPError as e:
            return DownloadResult(success=False, error_message=f"下载请求失败: {e}")
        except Exception as e:
            return DownloadResult(success=False, error_message=f"下载过程中发生错误: {e}")

    # ---------- 标签写入 ----------

    async def _write_music_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入音乐标签信息"""
        try:
            file_ext = file_path.suffix.lower()

            if file_ext == '.mp3':
                await self._write_mp3_tags(file_path, music_info)
            elif file_ext == '.flac':
                await self._write_flac_tags(file_path, music_info)
            elif file_ext == '.m4a':
                await self._write_m4a_tags(file_path, music_info)

        except Exception as e:
            _logger.warning(f"写入音乐标签失败 {file_path}: {e}")

    async def _write_mp3_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入MP3标签"""
        try:
            audio = MP3(str(file_path), ID3=ID3)

            audio.tags.add(TIT2(encoding=3, text=music_info.name))
            audio.tags.add(TPE1(encoding=3, text=music_info.artists))
            audio.tags.add(TALB(encoding=3, text=music_info.album))

            if music_info.track_number > 0:
                audio.tags.add(TRCK(encoding=3, text=str(music_info.track_number)))

            if music_info.pic_url:
                cover_data = await self._fetch_cover(music_info.pic_url)
                if cover_data:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=cover_data,
                    ))

            audio.save()
        except Exception as e:
            _logger.warning(f"写入MP3标签失败 {file_path}: {e}")

    async def _write_flac_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入FLAC标签"""
        try:
            audio = FLAC(str(file_path))

            audio['TITLE'] = music_info.name
            audio['ARTIST'] = music_info.artists
            audio['ALBUM'] = music_info.album

            if music_info.track_number > 0:
                audio['TRACKNUMBER'] = str(music_info.track_number)

            if music_info.pic_url:
                cover_data = await self._fetch_cover(music_info.pic_url)
                if cover_data:
                    picture = Picture()
                    picture.type = 3
                    picture.mime = 'image/jpeg'
                    picture.desc = 'Cover'
                    picture.data = cover_data
                    audio.add_picture(picture)

            audio.save()
        except Exception as e:
            _logger.warning(f"写入FLAC标签失败 {file_path}: {e}")

    async def _write_m4a_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入M4A标签"""
        try:
            audio = MP4(str(file_path))

            audio['\xa9nam'] = music_info.name
            audio['\xa9ART'] = music_info.artists
            audio['\xa9alb'] = music_info.album

            if music_info.track_number > 0:
                audio['trkn'] = [(music_info.track_number, 0)]

            if music_info.pic_url:
                cover_data = await self._fetch_cover(music_info.pic_url)
                if cover_data:
                    audio['covr'] = [cover_data]

            audio.save()
        except Exception as e:
            _logger.warning(f"写入M4A标签失败 {file_path}: {e}")

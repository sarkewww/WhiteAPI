"""批量下载任务管理器（WhiteAPI 适配版）

从 Netease_url 项目的 music_downloader.py 移植，将原多线程
ThreadPoolExecutor 批量下载改造为 asyncio 并发模型：
- 用 asyncio.create_task 运行后台下载任务
- 用 asyncio.Semaphore(3) 控制并发下载
- 用 asyncio.Lock 替代 threading.Lock 保护共享任务状态
- 全部完成后用 _ZipBuffer 打包 ZIP（256MB 以内内存，超出落盘）
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.downloader import MusicDownloader, safe_filename, format_speed, format_eta
from app.providers.base import MusicProvider

_batch_logger = logging.getLogger('batch_manager')


class _ZipBuffer:
    """ZIP 内存缓冲包装：256MB 以内使用内存，超出自动落盘，避免大歌单内存爆炸。

    兼容 BytesIO 的常用接口：getvalue() / getbuffer() / read() / seek()。
    """

    _MAX_IN_MEMORY = 256 * 1024 * 1024

    def __init__(self):
        self._buf = tempfile.SpooledTemporaryFile(max_size=self._MAX_IN_MEMORY, mode='w+b')
        self._size = 0

    def write(self, data):
        n = self._buf.write(data)
        pos = self._buf.tell()
        if pos > self._size:
            self._size = pos
        return n

    def seek(self, offset, whence=0):
        return self._buf.seek(offset, whence)

    def tell(self):
        return self._buf.tell()

    def flush(self):
        self._buf.flush()

    def read(self, size=-1):
        return self._buf.read(size)

    def getvalue(self):
        self._buf.seek(0)
        return self._buf.read()

    def getbuffer(self):
        return memoryview(self.getvalue())

    @property
    def size(self):
        return self._size

    def __getattr__(self, name):
        return getattr(self._buf, name)

    def close(self):
        self._buf.close()


class BatchTaskManager:
    """批量下载任务管理器（asyncio 版）"""

    TTL_SECONDS = 3600

    def __init__(self, downloader: MusicDownloader):
        self.downloader = downloader
        self.tasks: Dict[str, Dict] = {}
        self.lock = asyncio.Lock()

    def _cleanup_expired(self):
        """清理超过 TTL 的已结束任务（调用方需持锁）"""
        now = time.time()
        expired = [tid for tid, t in self.tasks.items()
                   if t['status'] in ('completed', 'failed', 'cancelled')
                   and now - t.get('start_time', now) > self.TTL_SECONDS]
        for tid in expired:
            self.tasks.pop(tid, None)

    @staticmethod
    def _safe_name(idx: int, track: Dict) -> str:
        """生成批量下载文件的安全名称（序号. 歌手 - 歌名）"""
        name = f"{idx + 1:03d}. {track.get('artists', '')} - {track.get('name', '')}"
        return ''.join(c for c in name if c not in r'<>:"/\|?*')

    def create_task(self, tracks: List[Dict], playlist_info: Dict,
                    level: str, provider: MusicProvider, cookies: str = "") -> str:
        """创建批量下载任务并后台执行

        Args:
            tracks: 曲目列表，元素格式 {'id': song_id, 'name': ..., 'artists': ...}
            playlist_info: 歌单信息 {'name': ..., 'creator': ...}
            level: 音质等级
            provider: 音乐平台 provider
            cookies: cookie 字符串

        Returns:
            任务ID
        """
        task_id = str(uuid.uuid4())[:8]
        task = {
            'task_id': task_id, 'status': 'running',
            'total': len(tracks), 'completed': 0, 'failed': 0, 'success': 0,
            'current_file': '', 'current_index': 0,
            'downloaded_bytes': 0, 'total_bytes': 0, 'speed': 0,
            'errors': [], 'files': [], 'playlist_info': playlist_info,
            'level': level, 'provider': provider, 'cookies': cookies,
            '_pre_sizes': {}, '_cancelled': False, 'start_time': time.time(),
        }
        # 单线程事件循环内同步登记，无 await 点，天然原子
        self._cleanup_expired()
        self.tasks[task_id] = task
        asyncio.create_task(self._run_download(task_id, tracks))
        return task_id

    async def _run_download(self, task_id: str, tracks: List[Dict]):
        """后台执行批量下载并打包 ZIP"""
        async with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
        level = task['level']
        provider = task['provider']
        downloader = self.downloader
        _batch_logger.info(f"[DL-TASK-{task_id}] starting download, {len(tracks)} tracks")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                sem = asyncio.Semaphore(3)

                async def _download_one_wrapper(i: int, track: Dict):
                    async with sem:
                        if await self._is_cancelled(task_id):
                            return None
                        return await self._download_one(
                            task_id, i, track, level, provider, tmp_path)

                results = await asyncio.gather(
                    *[_download_one_wrapper(i, track) for i, track in enumerate(tracks)],
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, BaseException):
                        _batch_logger.error(
                            f"[DL-TASK-{task_id}] unexpected download error: {res}")

                pl_name = task.get('playlist_info', {}).get('name', 'playlist')
                pl_creator = task.get('playlist_info', {}).get('creator', '')
                success_count = task.get('success', 0)
                _batch_logger.info(
                    f"[DL-TASK-{task_id}] all completed: {success_count}/{task.get('total')}")

                zip_buffer = None
                files_count = 0
                zip_size = 0
                if success_count > 0 and not await self._is_cancelled(task_id):
                    zip_buffer = _ZipBuffer()
                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                        files_in_tmp = sorted(tmp_path.iterdir())
                        files_count = len(files_in_tmp)
                        for f in files_in_tmp:
                            zf.write(str(f), f.name)
                    zip_buffer.seek(0)
                    zip_size = zip_buffer.size

                async with self.lock:
                    t = self.tasks.get(task_id)
                    if t is None or t.get('_cancelled'):
                        return
                    if success_count > 0:
                        t['zip_buffer'] = zip_buffer
                        t['zip_filename'] = safe_filename(f"{pl_name}-{pl_creator}").strip('-') + '.zip'
                        t['status'] = 'completed'
                        _batch_logger.info(
                            f"[DL-TASK-{task_id}] ZIP created: {files_count} files, {zip_size} bytes")
                    else:
                        t['status'] = 'failed'
        except Exception as e:
            _batch_logger.error(f"[DL-TASK-{task_id}] task failed with exception: {e}", exc_info=True)
            async with self.lock:
                t = self.tasks.get(task_id)
                if t is not None and not t.get('_cancelled'):
                    t['status'] = 'failed'
                    t['errors'].append({'index': -1, 'name': '任务异常', 'reason': str(e)})

    async def _is_cancelled(self, task_id: str) -> bool:
        """检查任务是否已取消"""
        t = self.tasks.get(task_id)
        return bool(t and t.get('_cancelled'))

    async def _download_one(self, task_id: str, idx: int, track: Dict,
                            level: str, provider: MusicProvider, tmp_path: Path):
        """下载单个曲目到临时目录并更新任务状态"""
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t['current_index'] = idx + 1
        t['current_file'] = f"{track.get('artists', '')} - {track.get('name', '')}"

        def progress_cb(downloaded, total_size, speed):
            # 同步回调，事件循环单线程下字典操作原子
            tsk = self.tasks.get(task_id)
            if tsk is None:
                return
            tsk['speed'] = speed
            prev = tsk.get(f'_lb_{idx}', 0)
            if downloaded > prev:
                tsk['downloaded_bytes'] = tsk.get('downloaded_bytes', 0) + (downloaded - prev)
                tsk[f'_lb_{idx}'] = downloaded
            ps = tsk.get('_pre_sizes', {})
            ps[idx] = total_size
            tsk['_pre_sizes'] = ps
            tsk['total_bytes'] = max(tsk['total_bytes'], sum(ps.values()))
            fp = tsk.get('_file_progress', {})
            fp[idx] = {'name': tsk['current_file'], 'downloaded': downloaded,
                       'total': total_size, 'status': 'downloading'}
            tsk['_file_progress'] = fp

        try:
            result = await self.downloader.download_music_file(
                provider, track['id'], level,
                progress_callback=progress_cb, cookies=t.get('cookies'))
            if result.success and result.file_path:
                src = Path(result.file_path)
                safe_name = self._safe_name(idx, track)
                dst = tmp_path / f"{safe_name}{src.suffix}"
                shutil.copy2(str(src), str(dst))
                fs = dst.stat().st_size
                async with self.lock:
                    tsk = self.tasks.get(task_id)
                    if tsk:
                        tsk['success'] += 1
                        tsk['completed'] += 1
                        tsk['files'].append({'name': f"{safe_name}{src.suffix}", 'size': fs})
                        fp = tsk.get('_file_progress', {})
                        if idx in fp:
                            fp[idx]['status'] = 'done'
                return True
            else:
                err_msg = result.error_message or '未知错误'
                async with self.lock:
                    tsk = self.tasks.get(task_id)
                    if tsk:
                        tsk['failed'] += 1
                        tsk['completed'] += 1
                        tsk['errors'].append(
                            {'index': idx + 1, 'name': track.get('name', ''), 'reason': err_msg})
                        fp = tsk.get('_file_progress', {})
                        if idx in fp:
                            fp[idx]['status'] = 'failed'
                return False
        except Exception as e:
            async with self.lock:
                tsk = self.tasks.get(task_id)
                if tsk:
                    tsk['failed'] += 1
                    tsk['completed'] += 1
                    tsk['errors'].append(
                        {'index': idx + 1, 'name': track.get('name', ''), 'reason': str(e)})
            return False

    async def get_progress(self, task_id: str) -> Optional[Dict]:
        """获取任务进度"""
        async with self.lock:
            self._cleanup_expired()
            task = self.tasks.get(task_id)
            if not task:
                return None
            elapsed = time.time() - task['start_time']
            avg_speed = int(task['downloaded_bytes'] / elapsed) if elapsed > 0 else 0
            remaining_bytes = max(0, task['total_bytes'] - task['downloaded_bytes'])
            eta = remaining_bytes / avg_speed if avg_speed > 0 and task['total_bytes'] > 0 else -1
            return {
                'task_id': task['task_id'], 'status': task['status'],
                'total': task['total'], 'completed': task['completed'],
                'success': task['success'], 'failed': task['failed'],
                'current_index': task['current_index'], 'current_file': task['current_file'],
                'downloaded_bytes': task['downloaded_bytes'], 'total_bytes': task['total_bytes'],
                'speed': task['speed'], 'errors': task['errors'],
                'speed_formatted': format_speed(task['speed']),
                'avg_speed_formatted': format_speed(avg_speed),
                'eta_formatted': format_eta(eta),
                'percent': round(task['completed'] / task['total'] * 100, 1) if task['total'] > 0 else 0,
                'files_progress': list(task.get('_file_progress', {}).values()),
            }

    async def get_result(self, task_id: str) -> Optional[Tuple]:
        """获取完成任务的 ZIP 结果 (zip_buffer, zip_filename, task)"""
        async with self.lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'completed':
                return None
            return task.get('zip_buffer'), task.get('zip_filename'), task

    async def cleanup(self, task_id: str):
        """清理任务"""
        async with self.lock:
            self.tasks.pop(task_id, None)

    async def cancel(self, task_id: str) -> bool:
        """取消运行中的任务"""
        async with self.lock:
            task = self.tasks.get(task_id)
            if task and task['status'] == 'running':
                task['_cancelled'] = True
                task['status'] = 'cancelled'
                return True
            return False

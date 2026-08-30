"""网易云音乐加密工具

包含 eapi 请求参数加密 (AES-ECB)、MD5 工具、图片 ID 加密算法等，
均为经过验证可用的官方加密逻辑移植。
"""

import base64
import hashlib
import json
import urllib.parse
from hashlib import md5
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_KEY = b"e82ckenh8dichen8"


class CryptoUtils:
    """网易云 eapi 加密工具类"""

    @staticmethod
    def hex_digest(data: bytes) -> str:
        """将字节数据转换为十六进制字符串"""
        return "".join([hex(d)[2:].zfill(2) for d in data])

    @staticmethod
    def hash_digest(text: str) -> bytes:
        """计算 MD5 哈希值"""
        return md5(text.encode("utf-8")).digest()

    @staticmethod
    def hash_hex_digest(text: str) -> str:
        """计算 MD5 哈希值并转换为十六进制字符串"""
        return CryptoUtils.hex_digest(CryptoUtils.hash_digest(text))

    @staticmethod
    def encrypt_params(url: str, payload: Dict[str, Any]) -> str:
        """加密 eapi 请求参数

        算法流程:
        1. url_path = 请求路径中的 /eapi/ 替换为 /api/
        2. digest = MD5("nobody{url_path}use{json.dumps(payload)}md5forencrypt")
        3. params = "{url_path}-36cd479b6b5-{json}-36cd479b6b5-{digest}"
        4. 对 params 做 PKCS7 填充后用 AES-ECB 加密，返回十六进制字符串

        Args:
            url: 完整请求 URL（含 /eapi/ 前缀）
            payload: 待加密的请求参数

        Returns:
            加密后的十六进制字符串
        """
        url_path = urllib.parse.urlparse(url).path.replace("/eapi/", "/api/")
        digest = CryptoUtils.hash_hex_digest(
            f"nobody{url_path}use{json.dumps(payload)}md5forencrypt"
        )
        params = f"{url_path}-36cd479b6b5-{json.dumps(payload)}-36cd479b6b5-{digest}"

        padder = padding.PKCS7(algorithms.AES(AES_KEY).block_size).padder()
        padded_data = padder.update(params.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(AES_KEY), modes.ECB())
        encryptor = cipher.encryptor()
        enc = encryptor.update(padded_data) + encryptor.finalize()

        return CryptoUtils.hex_digest(enc)


def netease_encrypt_id(id_str: str) -> str:
    """网易云加密图片 ID 算法

    对图片 ID 字符串逐字符与 magic 密钥异或，结果做 MD5 后
    base64 编码，并将 / 替换为 _、+ 替换为 -。
    """
    magic = list('3go8&$8*3*3h0k(2)2')
    song_id = list(id_str)
    for i in range(len(song_id)):
        song_id[i] = chr(ord(song_id[i]) ^ ord(magic[i % len(magic)]))
    m = ''.join(song_id)
    md5_bytes = hashlib.md5(m.encode('utf-8')).digest()
    result = base64.b64encode(md5_bytes).decode('utf-8')
    result = result.replace('/', '_').replace('+', '-')
    return result


def get_pic_url(pic_id: Optional[int], size: int = 300) -> str:
    """获取网易云加密歌曲/专辑封面直链

    Args:
        pic_id: 封面图片 ID
        size: 图片尺寸

    Returns:
        图片 URL；pic_id 为空时返回空字符串
    """
    if pic_id is None:
        return ''

    enc_id = netease_encrypt_id(str(pic_id))
    return f'https://p3.music.126.net/{enc_id}/{pic_id}.jpg?param={size}y{size}'

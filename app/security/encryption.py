"""对称加密工具，用于敏感字段静态加密（Fernet / AES-128-CBC + HMAC-SHA256）。"""

import base64
import hashlib
import os
import warnings
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# 加密密钥优先级：
#   env DB_ENCRYPTION_KEY
#   > SESSION_SECRET_KEY 派生
#   > 数据目录 data/.db_encryption_key（Docker 卷持久化）
#   > 兼容旧路径项目根 .db_encryption_key
#   > 首次启动自动生成到数据目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LEGACY_KEY_FILE = _PROJECT_ROOT / ".db_encryption_key"
KEY_FILE = _PROJECT_ROOT / "data" / ".db_encryption_key"


def _derive_key_from_secret(secret: str) -> bytes:
    """从 SESSION_SECRET_KEY 派生 32 字节 Fernet 密钥。"""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def get_fernet() -> Fernet:
    """获取 Fernet 实例。生产环境必须通过 env 注入密钥；dev 自动生成并落盘。"""
    key = os.getenv("DB_ENCRYPTION_KEY")
    if not key:
        session_secret = os.getenv("SESSION_SECRET_KEY")
        if session_secret:
            return Fernet(_derive_key_from_secret(session_secret))
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    # 兼容旧部署：密钥曾写在容器可写层 /app/.db_encryption_key，重建即丢
    if not key and _LEGACY_KEY_FILE.exists():
        key = _LEGACY_KEY_FILE.read_text().strip()
        try:
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            KEY_FILE.write_text(key)
            KEY_FILE.chmod(0o600)
        except OSError as e:
            warnings.warn(f"无法迁移加密密钥到数据目录: {e}", RuntimeWarning)
    if not key:
        key = Fernet.generate_key().decode()
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)  # noqa: ignore return value
        warnings.warn(
            f"DB_ENCRYPTION_KEY 未设置，已自动生成并保存到 {KEY_FILE}。"
            + "生产环境请通过环境变量注入并备份此密钥！",
            RuntimeWarning,
        )
    return Fernet(key.encode())


def encrypt_value(plaintext: str) -> str:
    """加密字符串，返回 Fernet token（utf-8 字符串）。"""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """解密 Fernet token。已为明文的输入会原样返回（迁移期兼容）。"""
    if ciphertext and ciphertext.startswith("gAAAAA"):
        try:
            return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            warnings.warn("AI 配置加密密钥已变更，请重新保存 AI 配置", RuntimeWarning)
            return ciphertext
    return ciphertext
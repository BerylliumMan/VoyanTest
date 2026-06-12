"""对称加密工具，用于敏感字段静态加密（Fernet / AES-128-CBC + HMAC-SHA256）。"""

import os
import warnings
from pathlib import Path

from cryptography.fernet import Fernet

# 加密密钥优先级：env DB_ENCRYPTION_KEY > 项目根目录 .db_encryption_key > 首次启动自动生成
KEY_FILE = Path(__file__).resolve().parent.parent.parent / ".db_encryption_key"


def get_fernet() -> Fernet:
    """获取 Fernet 实例。生产环境必须通过 env 注入密钥；dev 自动生成并落盘。"""
    key = os.getenv("DB_ENCRYPTION_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        key = Fernet.generate_key().decode()
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)  # noqa: ignore return value
        # 生产环境必须从环境变量注入并备份密钥文件
        warnings.warn(
            f"DB_ENCRYPTION_KEY 未设置，已自动生成并保存到 {KEY_FILE}。"
            + "生产环境必须通过环境变量注入并安全备份此文件！",
            RuntimeWarning,
        )
    return Fernet(key.encode())


def encrypt_value(plaintext: str) -> str:
    """加密字符串，返回 Fernet token（utf-8 字符串）。"""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """解密 Fernet token。已为明文的输入会原样返回（迁移期兼容）。"""
    if ciphertext and ciphertext.startswith("gAAAAA"):
        return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    return ciphertext
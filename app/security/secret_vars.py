# app/security/secret_vars.py — secret 变量加密（031-api-testing-enhancements US2）
#
# 契约（spec FR-004/005、data-model §1.3）：
#   · ``secret=true`` 的环境变量/场景变量一律**密文落库**，前缀 ``enc:v1:``（Fernet token）
#     密钥沿用 app/security/encryption.py 的 .db_encryption_key / DB_ENCRYPTION_KEY 设施。
#   · 回显仍是 ``******``（UI 语义）；``******`` 回传 == 不修改原值（crud 层旧值保留，且旧值是密文）。
#   · 明文输入（历史数据/回滚）读取时原样放行 —— 迁移与回滚安全；启动回填幂等。
#   · 密文损坏或密钥更换 → SecretDecryptError（调用方转可读失败，绝不静默传错值）。
from __future__ import annotations

import logging
from typing import Any

from cryptography.fernet import InvalidToken

from app.security.encryption import get_fernet

logger = logging.getLogger(__name__)

SECRET_PREFIX = "enc:v1:"
MASK = "******"


class SecretDecryptError(ValueError):
    """密文损坏或加密密钥已更换（调用方转 400 / 步骤失败）。"""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def encrypt_secret_value(plaintext: Any) -> Any:
    """加密单个值；掩码/空值/已加密 → 原样返回（幂等）。"""
    if plaintext is None:
        return plaintext
    text = str(plaintext)
    if text == "" or text == MASK or is_encrypted(text):
        return text
    return SECRET_PREFIX + get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(value: Any) -> Any:
    """解密 ``enc:v1:`` 值；非密文原样返回（迁移/回滚兼容）。"""
    if not is_encrypted(value):
        return value
    token = str(value)[len(SECRET_PREFIX):]
    try:
        return get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptError("密文损坏或加密密钥已更换，请重新填写该变量") from exc


def _items(items: Any) -> list[dict]:
    return [it for it in (items or []) if isinstance(it, dict)]


def encrypt_variables(items: Any) -> list[dict]:
    """secret 项写库前加密（非 secret 项不动）。"""
    out: list[dict] = []
    for it in _items(items):
        item = dict(it)
        if bool(it.get("secret")):
            item["value"] = encrypt_secret_value(it.get("value"))
        out.append(item)
    return out


def decrypt_variables(items: Any) -> list[dict]:
    """读取给执行层时解密 secret 项；非密文原样（迁移/回滚期）。"""
    out: list[dict] = []
    for it in _items(items):
        item = dict(it)
        if bool(it.get("secret")) and is_encrypted(it.get("value")):
            item["value"] = decrypt_secret_value(it.get("value"))
        out.append(item)
    return out


def decrypt_variables_safe(items: Any) -> tuple[list[dict], list[str]]:
    """解密变量并**收集**失败项（不抛异常）：返回 ``(解密后列表, 失败 key 列表)``。

    失败项的值置空（绝不把密文当值使用）；调用方按场景转可读失败（执行链路 / debug 400）。
    """
    out: list[dict] = []
    errors: list[str] = []
    for it in _items(items):
        item = dict(it)
        if bool(it.get("secret")) and is_encrypted(it.get("value")):
            try:
                item["value"] = decrypt_secret_value(it.get("value"))
            except SecretDecryptError:
                errors.append(str(it.get("key") or "?"))
                item["value"] = ""
        out.append(item)
    return out, errors


def has_plaintext_secret(items: Any) -> bool:
    """是否存在「secret=true 且值仍是明文」的项（回填迁移判定用）。"""
    for it in _items(items):
        if not bool(it.get("secret")):
            continue
        value = it.get("value")
        if value in (None, "", MASK):
            continue
        if not is_encrypted(value):
            return True
    return False


async def backfill_secret_variables(db) -> int:
    """启动回填：把 environments / api_scenarios 中 secret 明文值加密。

    幂等（已加密项跳过）；单个对象失败只记 warning 不阻断启动；返回改动的对象行数。
    """
    from sqlalchemy import select

    from app import db_models

    changed = 0
    try:
        for model, label in (
            (db_models.Environment, "environments"),
            (db_models.ApiScenario, "api_scenarios"),
        ):
            rows = list((await db.execute(select(model))).scalars().all())
            for row in rows:
                items = row.variables or []
                if not has_plaintext_secret(items):
                    continue
                try:
                    row.variables = encrypt_variables(items)
                    changed += 1
                except Exception:  # noqa: BLE001 - 单行失败不阻断整体迁移
                    logger.warning(
                        "secret 回填失败 %s id=%s", label, getattr(row, "id", "?"), exc_info=True
                    )
        if changed:
            await db.commit()
    except Exception:  # noqa: BLE001 - 迁移失败绝不能阻断启动
        logger.warning("secret 变量回填迁移失败（跳过，不影响启动）", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    return changed

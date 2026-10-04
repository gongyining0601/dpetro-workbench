"""访问密码认证模块。

设计要点：
- 单密码（唯一性）：整个应用只有一个访问密码，存储在 app_setting 表的
  key='access_password' 行。PRIMARY KEY 保证只有一条记录。
- 密码哈希：PBKDF2-HMAC-SHA256 + 随机盐 + 10 万次迭代，用 Python 标准库实现，
  不引入新依赖。存储格式 "salt_hex$hash_hex"。
- 常量时间比较：用 hmac.compare_digest 避免时序攻击。
- 不存明文：数据库里只有哈希，即使库泄露也无法反推密码。

用法：
    import auth
    # 首次设置
    auth.set_access_password("my-secret")
    # 验证
    if auth.verify_access_password("my-secret"):
        ...
    # 是否已设置
    if auth.has_access_password():
        ...
"""
from __future__ import annotations

import hashlib
import hmac
import os

import db

# 访问密码在 app_setting 表中的 key（单条记录 = 唯一性）
_ACCESS_PASSWORD_KEY = "access_password"

# PBKDF2 参数
_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 100_000
_SALT_BYTES = 16
_HASH_BYTES = 32


def _hash_password(password: str) -> str:
    """生成密码哈希，返回 'salt_hex$hash_hex' 格式字符串。"""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_HASH_BYTES,
    )
    return f"{salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """校验密码与存储哈希是否匹配（常量时间比较）。"""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_HASH_BYTES,
    )
    return hmac.compare_digest(dk, expected)


def has_access_password() -> bool:
    """是否已设置访问密码。"""
    return db.get_setting(_ACCESS_PASSWORD_KEY) is not None


def set_access_password(password: str) -> None:
    """设置（或覆盖）访问密码。密码会被哈希后存储，不存明文。"""
    if not password or not isinstance(password, str):
        raise ValueError("密码不能为空")
    if len(password) < 4:
        raise ValueError("密码至少 4 位")
    hashed = _hash_password(password)
    db.set_setting(_ACCESS_PASSWORD_KEY, hashed)


def verify_access_password(password: str) -> bool:
    """验证访问密码是否正确。未设置密码时返回 False。"""
    stored = db.get_setting(_ACCESS_PASSWORD_KEY)
    if not stored:
        return False
    return _verify_password(password, stored)

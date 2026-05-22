"""
Keycloak 认证模块

提供获取 Momenta 内网服务 Keycloak Token 的功能。
"""

import json
import os
import time
import requests
from typing import Optional

# Keycloak 配置
KEYCLOAK_URL = "https://keycloak-prod-cla.mmtwork.com/auth"
REALM_NAME = "momenta-prod"
CLIENT_ID = "ess-client"

# Token URL
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{REALM_NAME}/protocol/openid-connect/token"

# 默认账号（从环境变量读取，或使用默认值）
DEFAULT_USERNAME = os.getenv("KEYCLOAK_USERNAME", "cp_system_gpt")
DEFAULT_PASSWORD = os.getenv("KEYCLOAK_PASSWORD", "csg_2025")

# 文件缓存路径
_TOKEN_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "skill_tokens")
_TOKEN_CACHE_FILE = os.path.join(_TOKEN_CACHE_DIR, "keycloak_ess_token")

# 全局 Token 缓存
_cached_token: Optional[str] = None


def _load_cached_token() -> Optional[str]:
    """从文件缓存加载 token，检查 expiry（300s 余量）。"""
    try:
        if not os.path.exists(_TOKEN_CACHE_FILE):
            return None
        with open(_TOKEN_CACHE_FILE, "r") as f:
            data = json.load(f)
        expire_time = data.get("expire_time", 0)
        if time.time() < expire_time - 300:
            return data.get("access_token")
    except Exception:
        pass
    return None


def _save_token_to_file(access_token: str, expires_in: int, username: str):
    """写入 token 到文件缓存，设置 0o600 权限。"""
    try:
        os.makedirs(_TOKEN_CACHE_DIR, exist_ok=True)
        data = {
            "access_token": access_token,
            "expire_time": time.time() + expires_in,
            "username": username,
            "client_id": CLIENT_ID,
        }
        with open(_TOKEN_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(_TOKEN_CACHE_FILE, 0o600)
    except Exception:
        pass  # best-effort


def get_token(
    username: Optional[str] = None,
    password: Optional[str] = None,
    force_refresh: bool = False
) -> str:
    """
    获取 Keycloak access token

    Args:
        username: Keycloak 用户名，默认从环境变量 KEYCLOAK_USERNAME 读取或使用默认账号
        password: Keycloak 密码，默认从环境变量 KEYCLOAK_PASSWORD 读取或使用默认密码
        force_refresh: 是否强制刷新 token（忽略缓存），默认 False

    Returns:
        str: Bearer access token

    Raises:
        Exception: 当认证失败时抛出异常

    Examples:
        >>> token = get_token()
        >>> token = get_token(username="custom_user", password="custom_pass")
        >>> token = get_token(force_refresh=True)
    """
    global _cached_token

    # 1. 内存缓存
    if _cached_token is not None and not force_refresh:
        return _cached_token

    # 2. 文件缓存
    if not force_refresh:
        file_token = _load_cached_token()
        if file_token:
            _cached_token = file_token
            return _cached_token

    # 3. 请求新 token
    username = username or DEFAULT_USERNAME
    password = password or DEFAULT_PASSWORD

    data = {
        'client_id': CLIENT_ID,
        'username': username,
        'password': password,
        'grant_type': 'password'
    }

    try:
        response = requests.post(TOKEN_URL, data=data, timeout=10)
        response.raise_for_status()

        tokens = response.json()
        access_token = tokens.get("access_token")

        if not access_token:
            raise Exception("Response does not contain access_token")

        expires_in = tokens.get("expires_in", 300)

        # 写回文件 + 内存
        _save_token_to_file(access_token, expires_in, username)
        _cached_token = access_token
        return access_token

    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get token: {e}")
    except (KeyError, ValueError) as e:
        raise Exception(f"Failed to parse token response: {e}")


def clear_cache():
    """清除 token 缓存（内存 + 文件）"""
    global _cached_token
    _cached_token = None
    try:
        if os.path.exists(_TOKEN_CACHE_FILE):
            os.remove(_TOKEN_CACHE_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    # 命令行模式：输出 token
    try:
        token = get_token()
        print(token)
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        exit(1)

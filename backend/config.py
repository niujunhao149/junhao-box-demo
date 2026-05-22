"""应用配置管理"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """应用配置（从环境变量或 .env 文件读取）"""

    # ESS 配置
    ESS_BASE_URL: str = "https://ess.momenta.works"
    ESS_DEFAULT_USERNAME: str = "junhao.niu"
    ESS_DEFAULT_PASSWORD: str = "Mmt@20020102"
    ESS_TOKEN_EXPIRE_HOURS: int = 2

    # 飞书配置
    FEISHU_ENABLE: bool = True
    FEISHU_APP_ID: str = "cli_a94a6d93323bdcef"
    FEISHU_APP_SECRET: str = "FEISHU_SECRET_PLACEHOLDER_CHANGE_IN_ENV"
    FEISHU_VERIFICATION_TOKEN: str = "zIUncW2VT8gVbAaiYVoeabIdSk2Qusc5"  # 事件订阅验证 token
    FEISHU_ENCRYPT_KEY: str = ""  # 消息加密 key（可选）
    FEISHU_TOKEN_SYNC_KEY: str = "ess-roadtest-token-sync-2026"  # 内部 token 同步 API 密钥
    CLOUD_FEISHU_URL: str = ""  # 本地模式：云端地址，用于同步飞书 token（如 http://10.21.143.240:8001）

    # 应用配置
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # 并发配置
    ESS_CONCURRENT_WORKERS: int = 10
    ESS_RETRY_ATTEMPTS: int = 3

    # 超时配置（秒）
    ESS_LOGIN_TIMEOUT: int = 30
    ESS_API_TIMEOUT: int = 30

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()

"""全局配置。

对应实施设计第 21 节。所有配置项通过环境变量注入（前缀 REPOPILOT_），
使用 pydantic-settings 强类型解析，避免散落的 os.environ.get 调用。

安全约束（见设计文档）：
- 本模块只负责“加载”配置，不负责“分发”凭据；哪个进程能看到哪些凭据，
  由各 Worker 的启动入口自行决定加载哪个 Settings 子集，而不是在这里做区分。
- 日志中打印配置时必须使用 `safe_summary()`，禁止直接打印 Settings 对象。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程级配置。

    Phase 0 单仓库多进程共用同一个 Settings 定义；具体某个 Worker 是否
    真的需要某个凭据字段，由该 Worker 的入口函数决定是否读取，而不是
    在这里拆分成多个 Settings 类——拆分会在 Phase 1 引入更严格的进程边界
    时再做，避免过早设计。
    """

    model_config = SettingsConfigDict(
        env_prefix="REPOPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"

    # --- API ---
    api_token: SecretStr = Field(default=SecretStr("local-dev-token"))
    api_host: str = "127.0.0.1"
    api_port: int = 8080

    # --- PostgreSQL ---
    postgres_dsn: str = "postgresql+asyncpg://repopilot:repopilot@localhost:5432/repopilot"

    # --- Temporal ---
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "repopilot-dev"

    # --- MinIO / Artifact Store ---
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "repopilot"
    minio_secret_key: SecretStr = SecretStr("repopilot-secret")
    minio_bucket: str = "repopilot-artifacts"

    # --- Model Gateway ---
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_name: str = "fake-model"

    # --- Sandbox RPC（仅 model-worker / sandbox-worker 使用） ---
    sandbox_service_url: str = "http://127.0.0.1:8091"
    sandbox_service_token: SecretStr = SecretStr("local-sandbox-token")

    # --- 数据目录 ---
    data_dir: Path = Path("./.repopilot").resolve()

    @field_validator("sandbox_service_url")
    @classmethod
    def _sandbox_url_must_be_loopback(cls, value: str) -> str:
        """Sandbox RPC 只允许监听/访问 loopback 地址，防止内部 RPC 被外部访问。"""
        if not (
            value.startswith("http://127.0.0.1")
            or value.startswith("http://localhost")
            or value.startswith("https://127.0.0.1")
            or value.startswith("https://localhost")
        ):
            raise ValueError("REPOPILOT_SANDBOX_SERVICE_URL 必须是 loopback 地址")
        return value

    @field_validator("data_dir")
    @classmethod
    def _data_dir_must_be_absolute(cls, value: Path) -> Path:
        return value.resolve()

    def safe_summary(self) -> dict[str, str | int | bool]:
        """返回不含任何 Secret 的配置摘要，供启动日志打印。"""
        return {
            "env": self.env,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "temporal_address": self.temporal_address,
            "temporal_namespace": self.temporal_namespace,
            "minio_endpoint": self.minio_endpoint,
            "minio_bucket": self.minio_bucket,
            "model_name": self.model_name,
            "sandbox_service_url": self.sandbox_service_url,
            "data_dir": str(self.data_dir),
            "model_configured": self.model_base_url is not None,
        }


@lru_cache
def get_settings() -> Settings:
    """进程内单例；测试中可通过 `get_settings.cache_clear()` 重置。"""
    return Settings()

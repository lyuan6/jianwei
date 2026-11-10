"""Settings read from environment variables at startup."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=ROOT / ".env"):
    """Read KEY=VALUE lines without python-dotenv; variables already set (e.g. by systemd) win."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def data_dir():
    return Path(os.getenv("JIANWEI_DATA") or ROOT / "var")


def _check_url(url, name):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query:
        raise ValueError(f"{name} 必须是无凭据、无查询参数的 HTTPS 地址")


@dataclass(frozen=True)
class Settings:
    app_name: str = "见微"
    mode: str = "offline"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    exa_api_key: str = field(default="", repr=False)
    fallback_base_url: str = ""
    fallback_model: str = ""
    fallback_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        settings = cls(
            app_name=os.getenv("APP_NAME", "见微"),
            mode=os.getenv("APP_MODE", "offline"),
            base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
            model=os.getenv("LLM_MODEL", ""), api_key=os.getenv("LLM_API_KEY", ""),
            exa_api_key=os.getenv("EXA_API_KEY", "").strip(),
            fallback_base_url=os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/"),
            fallback_model=os.getenv("LLM_FALLBACK_MODEL", "deepseek-v4.1-flash"),
            fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY", "").strip(),
        )
        if settings.mode not in {"offline", "api"}:
            raise ValueError("APP_MODE 必须为 offline 或 api")
        if settings.mode == "api":
            if not all((settings.base_url, settings.model, settings.api_key)):
                raise ValueError("API 模式需要配置 LLM_BASE_URL、LLM_MODEL 和 LLM_API_KEY")
            _check_url(settings.base_url, "LLM_BASE_URL")
            if bool(settings.fallback_base_url) != bool(settings.fallback_api_key):
                raise ValueError("备用模型需要同时配置 LLM_FALLBACK_BASE_URL 和 LLM_FALLBACK_API_KEY")
            if settings.fallback_base_url:
                _check_url(settings.fallback_base_url, "LLM_FALLBACK_BASE_URL")
        return settings

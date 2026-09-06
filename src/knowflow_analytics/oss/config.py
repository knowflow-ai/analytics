from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

_CONFIG_FILE = "config.json"
_MASK = "********"


class ModelEndpoint(BaseModel):
    """One OpenAI-compatible endpoint (chat or embeddings)."""

    base_url: str = Field(default="", max_length=512)
    api_key: SecretStr = Field(default=SecretStr(""))
    model: str = Field(default="", max_length=256)
    # Output budget for this deployment.  ``/v1/models`` declares no capability
    # on any provider we have seen, so the ceiling cannot be discovered and has
    # to be stated by whoever configured the endpoint.  ``None`` keeps the
    # previous built-in ceiling.
    max_output_tokens: int | None = Field(default=None, ge=256, le=131_072)
    # Reasoning models spend the same output budget on thinking before writing
    # the answer, so a budget sized for the answer alone starves them.  ``off``
    # asks the provider to skip thinking; ``auto`` sends nothing and keeps the
    # provider default.
    thinking: Literal["auto", "off"] = "auto"

    @field_validator("base_url")
    @classmethod
    def base_url_is_http(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be HTTP(S)")
        return value

    @field_validator("model")
    @classmethod
    def model_is_trimmed(cls, value: str) -> str:
        return value.strip()

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)


def normalize_postgres_url(url: str) -> str:
    """Pin the psycopg3 driver: bare ``postgresql://`` makes SQLAlchemy look for psycopg2."""

    url = url.strip()
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


class OssConfig(BaseModel):
    """开源版设置：只有两个模型端点。数据源在「数据库连接」里逐个添加，不在这里。"""

    chat_model: ModelEndpoint = Field(default_factory=ModelEndpoint)
    embedding_model: ModelEndpoint = Field(default_factory=ModelEndpoint)

    def is_complete(self) -> bool:
        return self.chat_model.is_configured() and self.embedding_model.is_configured()

    def public_view(self) -> dict:
        """Settings as shown to the browser: secrets replaced by a mask or blank."""

        def endpoint(item: ModelEndpoint) -> dict:
            return {
                "base_url": item.base_url,
                "api_key": _MASK if item.api_key.get_secret_value() else "",
                "model": item.model,
                "max_output_tokens": item.max_output_tokens,
                "thinking": item.thinking,
            }

        return {
            "chat_model": endpoint(self.chat_model),
            "embedding_model": endpoint(self.embedding_model),
            "configured": {
                "chat_model": self.chat_model.is_configured(),
                "embedding_model": self.embedding_model.is_configured(),
            },
        }

    def merged_with(self, incoming: OssConfig) -> OssConfig:
        """Apply a browser update, keeping stored secrets where the mask was sent back."""

        def endpoint(current: ModelEndpoint, new: ModelEndpoint) -> ModelEndpoint:
            key = new.api_key.get_secret_value()
            if key == _MASK:
                # A masked key only ever means "the key for the address I was
                # shown"; never forward a stored key to a different host.
                if new.base_url != current.base_url:
                    raise ValueError("更换 Base URL 后请重新填写 API Key")
                key_secret = current.api_key
            else:
                key_secret = new.api_key
            return ModelEndpoint(
                base_url=new.base_url,
                model=new.model,
                api_key=key_secret,
                max_output_tokens=new.max_output_tokens,
                thinking=new.thinking,
            )

        return OssConfig(
            chat_model=endpoint(self.chat_model, incoming.chat_model),
            embedding_model=endpoint(self.embedding_model, incoming.embedding_model),
        )


class ConfigStore:
    """Single JSON file under the data directory; writes are atomic."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _CONFIG_FILE
        data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> OssConfig:
        if not self._path.exists():
            return OssConfig()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"config file {self._path} is unreadable") from exc
        return OssConfig.model_validate(raw)

    def save(self, config: OssConfig) -> None:
        payload = {
            "chat_model": _dump_endpoint(config.chat_model),
            "embedding_model": _dump_endpoint(config.embedding_model),
        }
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise


def _dump_endpoint(item: ModelEndpoint) -> dict:
    return {
        "base_url": item.base_url,
        "api_key": item.api_key.get_secret_value(),
        "model": item.model,
        "max_output_tokens": item.max_output_tokens,
        "thinking": item.thinking,
    }

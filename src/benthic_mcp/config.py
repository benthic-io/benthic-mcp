import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BDP_ROOT = "https://benthic.io/bdp"
DEFAULT_TRUSTED_KEYS = ("qwG8vQQN7m/uB0Nwgor3s1EJCKjqZM/SNThV+V7XumM=",)
DEFAULT_MCP_HOSTS = ("127.0.0.1:8082", "localhost:8082", "[::1]:8082")
# Localhost only, and deliberately so. These are the allow-lists for the Host and Origin headers, so
# a default that named a particular machine on a particular network would silently authorise that host
# for anyone who installed the server without reading the configuration. Add your own host
# explicitly; see config/benthic-mcp.env.example.
DEFAULT_MCP_ORIGINS = ("http://127.0.0.1:8081", "http://localhost:8081")


def _parse_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _parse_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.environ.get(name)
    if value is None:
        return default
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError(f"{name} must contain at least one value")
    return items


def _parse_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "benthic-mcp"


PLAYBOOK_MODES = ("off", "seed", "active")


@dataclass(frozen=True, slots=True)
class Settings:
    bdp_root: str = DEFAULT_BDP_ROOT
    collections: tuple[str, ...] = ("ngopen",)
    trusted_keys: tuple[str, ...] = DEFAULT_TRUSTED_KEYS
    cache_dir: Path = _default_cache_dir()
    cache_ttl_seconds: int = 900
    max_cache_age_seconds: int = 604800
    request_timeout_seconds: float = 30.0
    max_rows: int = 1000
    default_query_limit: int = 100
    aggregate_scan_limit: int = 10_000
    max_response_bytes: int = 1_048_576
    user_agent: str = "benthic-mcp/0.1"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8082
    mcp_path: str = "/mcp"
    mcp_allowed_hosts: tuple[str, ...] = DEFAULT_MCP_HOSTS
    mcp_allowed_origins: tuple[str, ...] = DEFAULT_MCP_ORIGINS
    mcp_bearer_token: str | None = None
    playbook_mode: str = "seed"
    playbook_path: Path | None = None
    playbook_token_budget: int = 600
    lesson_retention_days: int = 30
    trace_enabled: bool = True
    trace_retention_days: int = 30
    trace_include_text: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        collections_raw = os.environ.get("BENTHIC_COLLECTIONS", "ngopen")
        collections = tuple(item.strip() for item in collections_raw.split(",") if item.strip())
        if not collections:
            raise ValueError("BENTHIC_COLLECTIONS must contain at least one collection")

        cache_dir = Path(os.environ.get("BENTHIC_CACHE_DIR", str(_default_cache_dir())))

        keys_raw = os.environ.get("BENTHIC_TRUSTED_KEYS")
        trusted_keys = (
            tuple(item.strip() for item in keys_raw.split(",") if item.strip()) if keys_raw else DEFAULT_TRUSTED_KEYS
        )
        if not trusted_keys:
            raise ValueError("BENTHIC_TRUSTED_KEYS must contain at least one key")

        settings = cls(
            bdp_root=os.environ.get("BENTHIC_BDP_ROOT", DEFAULT_BDP_ROOT).rstrip("/"),
            collections=collections,
            trusted_keys=trusted_keys,
            cache_dir=cache_dir,
            cache_ttl_seconds=_parse_int("BENTHIC_CACHE_TTL_SECONDS", 900),
            max_cache_age_seconds=_parse_int("BENTHIC_MAX_CACHE_AGE_SECONDS", 604800),
            request_timeout_seconds=_parse_float("BENTHIC_REQUEST_TIMEOUT_SECONDS", 30.0),
            max_rows=_parse_int("BENTHIC_MAX_ROWS", 1000),
            default_query_limit=_parse_int("BENTHIC_DEFAULT_QUERY_LIMIT", 100),
            aggregate_scan_limit=_parse_int("BENTHIC_AGGREGATE_SCAN_LIMIT", 10_000),
            max_response_bytes=_parse_int("BENTHIC_MAX_RESPONSE_BYTES", 1_048_576),
            mcp_host=os.environ.get("BENTHIC_MCP_HOST", "0.0.0.0"),
            mcp_port=_parse_int("BENTHIC_MCP_PORT", 8082),
            mcp_path=os.environ.get("BENTHIC_MCP_PATH", "/mcp"),
            mcp_allowed_hosts=_parse_csv("BENTHIC_MCP_ALLOWED_HOSTS", DEFAULT_MCP_HOSTS),
            mcp_allowed_origins=_parse_csv("BENTHIC_MCP_ALLOWED_ORIGINS", DEFAULT_MCP_ORIGINS),
            mcp_bearer_token=os.environ.get("BENTHIC_MCP_BEARER_TOKEN") or None,
            playbook_mode=os.environ.get("BENTHIC_PLAYBOOK_MODE", "seed"),
            playbook_path=Path(os.environ.get("BENTHIC_PLAYBOOK_PATH", str(cache_dir / "playbook.json"))),
            playbook_token_budget=_parse_int("BENTHIC_PLAYBOOK_TOKEN_BUDGET", 600),
            lesson_retention_days=_parse_int("BENTHIC_LESSON_RETENTION_DAYS", 30),
            trace_enabled=_parse_bool("BENTHIC_TRACE_ENABLED", True),
            trace_retention_days=_parse_int("BENTHIC_TRACE_RETENTION_DAYS", 30),
            trace_include_text=_parse_bool("BENTHIC_TRACE_INCLUDE_TEXT", False),
        )
        settings.validate()

        return settings

    def validate(self) -> None:
        root = urlparse(self.bdp_root)
        if root.scheme != "https" or not root.netloc or root.username or root.password or root.query or root.fragment:
            raise ValueError("BENTHIC_BDP_ROOT must be an absolute HTTPS URL without credentials or query data")
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None for name in self.collections):
            raise ValueError("BENTHIC_COLLECTIONS contains an invalid collection name")
        if self.cache_ttl_seconds < 0 or self.max_cache_age_seconds < 0:
            raise ValueError("cache ages cannot be negative")
        if self.max_rows < 1 or self.default_query_limit < 1 or self.aggregate_scan_limit < 1:
            raise ValueError("row limits must be positive")
        if self.default_query_limit > self.max_rows:
            raise ValueError("BENTHIC_DEFAULT_QUERY_LIMIT cannot exceed BENTHIC_MAX_ROWS")
        if self.aggregate_scan_limit < self.max_rows:
            raise ValueError("BENTHIC_AGGREGATE_SCAN_LIMIT cannot be below BENTHIC_MAX_ROWS")
        if self.request_timeout_seconds <= 0 or self.max_response_bytes < 1:
            raise ValueError("timeout and response limits must be positive")
        if not self.mcp_host or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.mcp_host):
            raise ValueError("BENTHIC_MCP_HOST is invalid")
        if not 1 <= self.mcp_port <= 65535:
            raise ValueError("BENTHIC_MCP_PORT must be between 1 and 65535")
        if not self.mcp_path.startswith("/") or "?" in self.mcp_path or "#" in self.mcp_path:
            raise ValueError("BENTHIC_MCP_PATH must be an absolute HTTP path")
        if any(
            not host or "://" in host or "/" in host or any(character.isspace() for character in host)
            for host in self.mcp_allowed_hosts
        ):
            raise ValueError("BENTHIC_MCP_ALLOWED_HOSTS contains an invalid host")
        for origin in self.mcp_allowed_origins:
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("BENTHIC_MCP_ALLOWED_ORIGINS contains an invalid origin")
        if self.mcp_bearer_token is not None and len(self.mcp_bearer_token) < 32:
            raise ValueError("BENTHIC_MCP_BEARER_TOKEN must contain at least 32 characters")
        if self.playbook_mode not in PLAYBOOK_MODES:
            raise ValueError(f"BENTHIC_PLAYBOOK_MODE must be one of {', '.join(PLAYBOOK_MODES)}")
        if self.playbook_token_budget < 50:
            raise ValueError("BENTHIC_PLAYBOOK_TOKEN_BUDGET must be at least 50")
        if self.lesson_retention_days < 1 or self.trace_retention_days < 1:
            raise ValueError("lesson and trace retention must be at least 1 day")

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ServerConfig:
    host: str
    socket_port: int
    http_port: int
    tokens: Dict[str, str] = field(default_factory=dict)


@dataclass
class MatrixConfig:
    homeserver: str
    user_id: str
    access_token: str
    device_id: Optional[str] = None
    sync_filter_path: Optional[Path] = None
    store_path: Optional[Path] = None
    encryption: bool = False
    media_cache_dir: Optional[Path] = None
    verify_ssl: bool = True


@dataclass
class AppConfig:
    server: ServerConfig
    matrix: MatrixConfig


DEFAULT_SERVER_TOKENS = {
    "SERVER": "3qGT_%78Dtr|&*7ufZoO",
    "CLIENT": "vC.I)Xsfe(;p4YB6E5@y",
}


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _resolve(path: str | os.PathLike[str] | None) -> Optional[Path]:
    if not path:
        return None
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def load_config(
    root_dir: Path,
    server_config_path: str = "config.json",
    matrix_config_path: str = "matrix.config.json",
) -> AppConfig:
    server_raw = _load_json(root_dir / server_config_path)
    matrix_raw = _load_json(root_dir / matrix_config_path)

    server_cfg = ServerConfig(
        host=server_raw.get("HOST", "0.0.0.0"),
        socket_port=int(server_raw.get("PORT", 7300)),
        http_port=int(server_raw.get("HTTP_PORT", 7301)),
        tokens=server_raw.get("TOKENS", DEFAULT_SERVER_TOKENS),
    )

    tokens = server_cfg.tokens or {}
    if not tokens:
        server_cfg.tokens = DEFAULT_SERVER_TOKENS.copy()

    matrix_cfg = MatrixConfig(
        homeserver=matrix_raw["homeserver"],
        user_id=matrix_raw["user_id"],
        access_token=matrix_raw["access_token"],
        device_id=matrix_raw.get("device_id"),
        sync_filter_path=_resolve(matrix_raw.get("sync_filter_path")),
        store_path=_resolve(matrix_raw.get("store_path")),
        encryption=bool(matrix_raw.get("encryption", False)),
        media_cache_dir=_resolve(matrix_raw.get("media_cache_dir")),
        verify_ssl=matrix_raw.get("verify_ssl", True),
    )

    return AppConfig(server=server_cfg, matrix=matrix_cfg)

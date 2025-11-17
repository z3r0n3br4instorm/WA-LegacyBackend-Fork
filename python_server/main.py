from __future__ import annotations

from pathlib import Path

import uvicorn

from .http_api import create_app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    app = create_app(root)
    config = app.state.config
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

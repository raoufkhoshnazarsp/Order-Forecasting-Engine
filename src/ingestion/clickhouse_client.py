"""ClickHouse connection wrapper.

Credentials come exclusively from environment variables; nothing is hardcoded
and nothing is logged. The client is created read-only.
"""

from __future__ import annotations

from typing import Any

from src import config


class ClickHouseUnavailable(RuntimeError):
    """Raised when the driver is missing or the connection cannot be built."""


def build_client(settings: dict[str, Any] | None = None) -> Any:
    """Create a clickhouse-connect client from resolved config."""
    try:
        import clickhouse_connect
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ClickHouseUnavailable(
            "clickhouse-connect is not installed; run pip install clickhouse-connect"
        ) from exc

    db = (settings or config.load_yaml("database.yaml"))["clickhouse"]
    host = db.get("host")
    if not host:
        raise ClickHouseUnavailable(
            "CLICKHOUSE_HOST is empty; set the environment variables before extracting"
        )
    return clickhouse_connect.get_client(
        host=host,
        port=int(db.get("port") or 8443),
        username=db.get("user"),
        password=db.get("password"),
        database=db.get("database") or "default",
        secure=str(db.get("secure", "true")).lower() == "true",
        connect_timeout=int(db.get("connect_timeout_seconds", 30)),
        read_only=True,
    )


def query_df(client: Any, sql: str, settings: dict[str, Any] | None = None):
    """Execute SQL and return a pandas DataFrame."""
    return client.query_df(sql)

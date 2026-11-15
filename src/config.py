"""Configuration loading with environment-variable substitution.

No credentials are ever stored in YAML; ``${VAR}`` placeholders are resolved
from the process environment at load time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"
SQL_DIR = PROJECT_ROOT / "sql"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised for missing or invalid configuration."""


def _substitute(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name!r} is required but not set")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    return value


def load_yaml(name: str) -> dict[str, Any]:
    """Load a YAML file from the configs directory."""
    path = CONFIG_DIR / name
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _substitute(data)


def load_all() -> dict[str, dict[str, Any]]:
    """Load every top-level config document used by the engine."""
    return {
        "base": load_yaml("base.yaml"),
        "database": load_yaml("database.yaml"),
        "target": load_yaml("target.yaml"),
        "hierarchy": load_yaml("hierarchy.yaml"),
        "features": load_yaml("features.yaml"),
    }


def load_sql(relative_path: str) -> str:
    """Read a versioned SQL template from the sql/ tree."""
    path = SQL_DIR / relative_path
    if not path.exists():
        raise ConfigError(f"SQL file not found: {path}")
    return path.read_text(encoding="utf-8")

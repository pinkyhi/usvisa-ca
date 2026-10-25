"""Stable configuration and state paths for source and installed launches."""

import os
from pathlib import Path


def project_root() -> Path:
    """Keep source checkouts anchored to their root, independent of cwd."""
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "usvisa").is_dir():
        return candidate
    return Path.cwd().resolve()


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def environment_file() -> Path:
    value = os.getenv("USVISA_ENV_FILE")
    return _resolve(value, project_root()) if value else project_root() / ".env"


def load_environment() -> None:
    # Import on use so package/CLI help does not need runtime dependencies.
    from dotenv import load_dotenv

    load_dotenv(environment_file())


def data_directory() -> Path:
    value = os.getenv("USVISA_DATA_DIR")
    return _resolve(value, project_root()) if value else project_root()

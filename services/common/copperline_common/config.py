"""Where config files live. In vK8s they are one flat ConfigMap mount; locally set CONFIG_DIR."""

import os
from pathlib import Path
from typing import Any

import yaml

MATRIX_RELATIVE_PATH = Path("config/policy/tool-access-matrix.yaml")


def config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", "/config"))


def config_file(name: str) -> Path:
    return config_dir() / name


def load_yaml(path: str | Path) -> Any:
    """yaml.safe_load the file. A missing file raises FileNotFoundError."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def repo_root() -> Path | None:
    """The checkout root (dev and tests only). None inside a container image."""
    for parent in Path(__file__).resolve().parents:
        if (parent / MATRIX_RELATIVE_PATH).is_file():
            return parent
    return None

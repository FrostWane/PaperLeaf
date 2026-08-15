import json
import tomllib
from pathlib import Path

from paperleaf_api import __version__


def test_public_release_versions_are_consistent() -> None:
    root = Path(__file__).resolve().parents[2]
    package_version = json.loads((root / "package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    backend_version = tomllib.loads(
        (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]

    assert package_version == backend_version == __version__ == "0.9.0"

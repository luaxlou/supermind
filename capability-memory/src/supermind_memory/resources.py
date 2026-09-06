"""Resolve immutable distribution data in a source checkout or installed wheel."""

from pathlib import Path


def distribution_data(relative: str) -> Path:
    package = Path(__file__).resolve().parent
    # Hatch includes the same root data under the package when building a wheel.
    root = package / "data" if (package / "data").is_dir() else package.parent.parent
    return root / relative

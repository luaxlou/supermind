"""Stable category rules for capability records."""

TOP_LEVEL_CATEGORIES: tuple[str, ...] = (
    "Code and components",
    "Product and business",
    "Design and experience",
    "Engineering and methods",
    "Tools and integrations",
    "Data and intelligence",
)


def validate_category_path(path: tuple[str, ...]) -> None:
    """Reject category paths that are not rooted in the stable taxonomy."""
    if not path:
        raise ValueError("category path must contain at least one category")
    if path[0] not in TOP_LEVEL_CATEGORIES:
        raise ValueError(f"unknown top-level category: {path[0]}")

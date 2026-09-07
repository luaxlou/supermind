"""Stable category rules for capability records."""

TOP_LEVEL_CATEGORIES: tuple[str, ...] = (
    "code", "product", "design", "engineering", "tools", "data",
)

LEGACY_CATEGORIES = dict(zip(("Code and components", "Product and business", "Design and experience",
    "Engineering and methods", "Tools and integrations", "Data and intelligence"), TOP_LEVEL_CATEGORIES))


def canonical_category_path(path: tuple[str, ...]) -> tuple[str, ...]:
    """Decode historical category names into the current stable taxonomy."""
    if not path:
        return ()
    aliases = {key.casefold(): value for key, value in LEGACY_CATEGORIES.items()}
    aliases.update({key: key for key in TOP_LEVEL_CATEGORIES})
    return (aliases.get(path[0].casefold(), path[0]), *path[1:])


def validate_category_path(path: tuple[str, ...]) -> None:
    """Reject category paths that are not rooted in the stable taxonomy."""
    if not path:
        raise ValueError("category path must contain at least one category")
    if canonical_category_path(path)[0] not in TOP_LEVEL_CATEGORIES:
        raise ValueError(f"unknown top-level category: {path[0]}")

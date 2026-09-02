""".. include:: ../README.md"""  # noqa: D415

from __future__ import annotations

import importlib.metadata

from .piccoma import (
    TILE_SIZE,
    VALID_HOSTS,
    Entry,
    Episode,
    EpisodeType,
    LoginError,
    NeedPurchase,
    NotAPiccomaPageError,
    Page,
    Piccoma,
    PiccomaError,
    descramble,
    parse_seed,
    seedrandom,
    shuffle_order,
)

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = (
    "TILE_SIZE",
    "VALID_HOSTS",
    "Entry",
    "Episode",
    "EpisodeType",
    "LoginError",
    "NeedPurchase",
    "NotAPiccomaPageError",
    "Page",
    "Piccoma",
    "PiccomaError",
    "descramble",
    "parse_seed",
    "seedrandom",
    "shuffle_order",
)

"""Shared reducer for system_info child_aggregates.

One implementation behind both the ``xml`` and ``json`` system_info
sources. The formats differ only in how an item is enumerated and how a
key is read from it; the filter, conversion, and max steps are the same,
so they live here rather than once per format.

See SYSTEM_INFO_SPEC.md child_aggregates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from ..models.parser_config.common import FilterValue
from .filter import passes_filter
from .type_conversion import convert_value


class ChildAggregate(Protocol):
    """The fields both XMLChildAggregate and JSONChildAggregate share."""

    filter: dict[str, FilterValue]
    map: dict[str, str] | None
    max: str
    type: str
    scale: int | float | None


def aggregate_max[Item](
    items: Iterable[Item],
    agg: ChildAggregate,
    read: Callable[[Item, str], Any],
) -> int | float | None:
    """Max of ``agg.max`` across the items passing ``agg.filter``.

    ``read`` pulls a raw key value from one item, or ``None`` when the
    item does not carry that key.
    """
    best: int | float | None = None

    for item in items:
        if not passes_filter(_filter_values(item, agg, read), agg.filter):
            continue

        raw = read(item, agg.max)
        if raw is None:
            continue

        converted = convert_value(raw, agg.type, scale=agg.scale)
        if converted is not None and isinstance(converted, int | float) and (best is None or converted > best):
            best = converted

    return best


def _filter_values[Item](
    item: Item,
    agg: ChildAggregate,
    read: Callable[[Item, str], Any],
) -> dict[str, Any]:
    """Read and normalize the filter keys of one item.

    Absent keys are left out so ``passes_filter`` sees ``None`` and
    rejects on an equality rule, rather than matching a stringified
    ``None``. Order matches the channel parsers: convert (which applies
    ``map``) before filtering, so filters compare normalized values
    rather than wire spellings.
    """
    values: dict[str, Any] = {}
    for key in agg.filter:
        raw = read(item, key)
        if raw is None:
            continue
        values[key] = convert_value(raw, "string", map_config=agg.map)
    return values

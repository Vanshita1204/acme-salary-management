"""Opaque keyset-pagination cursors. Pure functions: no database or network access.

A cursor records where a page boundary sits in a sort: the sort it belongs to, which
way to read from it ("after" = next page, "before" = previous page), and the sort-key
values of the boundary row. Clients treat it as an opaque string.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Literal

Direction = Literal["after", "before"]
Order = Literal["asc", "desc"]

INVALID_CURSOR = "invalid cursor"
CURSOR_MISMATCH = "cursor belongs to a different sort; restart from the first page"


class CursorError(ValueError):
    pass


@dataclass(frozen=True)
class Cursor:
    sort: str
    order: Order
    direction: Direction
    key: tuple[str | None, ...]  # boundary row's sort-key values, as strings


def encode_cursor(cursor: Cursor) -> str:
    payload = {
        "s": cursor.sort,
        "o": cursor.order,
        "d": cursor.direction,
        "k": list(cursor.key),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str, sort: str, order: Order) -> Cursor:
    """Parse a cursor and check it belongs to the requested sort and order."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw)
        cursor = Cursor(
            sort=payload["s"],
            order=payload["o"],
            direction=payload["d"],
            key=tuple(payload["k"]),
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise CursorError(INVALID_CURSOR) from exc
    if cursor.direction not in ("after", "before") or cursor.order not in (
        "asc",
        "desc",
    ):
        raise CursorError(INVALID_CURSOR)
    if (cursor.sort, cursor.order) != (sort, order):
        raise CursorError(CURSOR_MISMATCH)
    return cursor


def reads_ascending(order: Order, direction: Direction | None) -> bool:
    """Which way the database scans: reading 'before' a cursor walks the sort backwards."""
    return (order == "asc") != (direction == "before")

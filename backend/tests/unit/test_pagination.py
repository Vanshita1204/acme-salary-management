import pytest

from app.domain.pagination import (
    CURSOR_MISMATCH,
    INVALID_CURSOR,
    Cursor,
    CursorError,
    decode_cursor,
    encode_cursor,
    reads_ascending,
)


def test_round_trip():
    cursor = Cursor("compensation", "desc", "before", ("123456.78", "42"))
    token = encode_cursor(cursor)
    assert "=" not in token  # URL-safe, unpadded
    assert decode_cursor(token, "compensation", "desc") == cursor


def test_rejects_cursor_for_another_sort_or_order():
    token = encode_cursor(Cursor("name", "asc", "after", ("a", "b", "1")))
    with pytest.raises(CursorError, match=CURSOR_MISMATCH):
        decode_cursor(token, "hire_date", "asc")
    with pytest.raises(CursorError, match=CURSOR_MISMATCH):
        decode_cursor(token, "name", "desc")


@pytest.mark.parametrize("token", ["", "not base64!", "e30", "eyJzIjoibmFtZSJ9"])
def test_rejects_garbage(token):
    with pytest.raises(CursorError, match=INVALID_CURSOR):
        decode_cursor(token, "name", "asc")


def test_rejects_unknown_direction():
    token = encode_cursor(Cursor("name", "asc", "sideways", ()))  # type: ignore[arg-type]
    with pytest.raises(CursorError, match=INVALID_CURSOR):
        decode_cursor(token, "name", "asc")


@pytest.mark.parametrize(
    ("order", "direction", "ascending"),
    [
        ("asc", None, True),
        ("asc", "after", True),
        ("asc", "before", False),
        ("desc", None, False),
        ("desc", "after", False),
        ("desc", "before", True),
    ],
)
def test_reads_ascending(order, direction, ascending):
    assert reads_ascending(order, direction) is ascending

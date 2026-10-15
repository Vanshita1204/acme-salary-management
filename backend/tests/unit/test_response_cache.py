from app.core.cache import CachedResponse, ResponseCache

RESPONSE = CachedResponse(200, {}, b"{}")


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_serves_a_stored_response_until_the_time_limit():
    clock = Clock()
    cache = ResponseCache(ttl=60, clock=clock)
    cache.put("a", RESPONSE, cache.version)

    assert cache.get("a") == RESPONSE
    clock.now = 61
    assert cache.get("a") is None


def test_invalidate_empties_the_cache():
    cache = ResponseCache(ttl=60)
    cache.put("a", RESPONSE, cache.version)
    cache.invalidate()

    assert cache.get("a") is None


def test_a_response_computed_before_a_write_is_not_stored():
    cache = ResponseCache(ttl=60)
    version = cache.version
    cache.invalidate()  # a write lands while the response is being computed
    cache.put("a", RESPONSE, version)

    assert cache.get("a") is None


def test_the_oldest_entries_go_first_when_full():
    cache = ResponseCache(ttl=60, max_entries=2)
    for key in ("a", "b"):
        cache.put(key, RESPONSE, cache.version)
    cache.get("a")  # "b" is now the least recently used
    cache.put("c", RESPONSE, cache.version)

    assert cache.get("b") is None
    assert cache.get("a") and cache.get("c")


def test_a_zero_time_limit_disables_it():
    cache = ResponseCache(ttl=0)
    cache.put("a", RESPONSE, cache.version)

    assert not cache.enabled
    assert cache.get("a") is None

from memu.api import _expansion_schedule
from memu.models import SearchRequest


def test_expansion_schedule_doubles_with_cap():
    schedule = _expansion_schedule(base_limit=20, max_steps=4)
    assert schedule == [20, 40, 80, 160]


def test_expansion_schedule_hard_cap():
    schedule = _expansion_schedule(base_limit=100, max_steps=4)
    assert schedule == [100, 200, 400, 400]


def test_search_request_upgrade_defaults():
    req = SearchRequest(query="memory retrieval")
    assert req.min_results == 3
    assert req.max_expansion_steps == 3
    assert req.lexical_fallback is True

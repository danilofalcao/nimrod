"""Window-bound parsing for the MCP list tools.

The tools accept an ISO date/datetime or epoch seconds/ms. Naive values are read
on the local clock -- that is what a user means by "yesterday" -- while an
explicit offset in the string is honoured as given.
"""

import datetime

from nimrod.mcp_server import _to_ms


def _local(y, m, d, hh=0, mm=0, ss=0, us=0):
    naive = datetime.datetime(y, m, d, hh, mm, ss, us)
    return int(naive.astimezone().timestamp() * 1000)


def test_to_ms_ignores_missing_and_unparseable_bounds():
    assert _to_ms(None) is None
    assert _to_ms("") is None
    assert _to_ms("   ") is None
    assert _to_ms("not-a-date") is None


def test_to_ms_accepts_epoch_seconds_and_milliseconds():
    assert _to_ms(1790000000000) == 1790000000000
    assert _to_ms(1790000000) == 1790000000000
    assert _to_ms(1790000000.0) == 1790000000000


def test_to_ms_reads_naive_values_on_the_local_clock():
    assert _to_ms("2026-09-21") == _local(2026, 9, 21)
    assert _to_ms("2026-09-21T08:30") == _local(2026, 9, 21, 8, 30)


def test_to_ms_includes_the_whole_named_day_for_until():
    start = _to_ms("2026-09-21")
    end = _to_ms("2026-09-21", end_of_day=True)
    assert end == _local(2026, 9, 21, 23, 59, 59, 999000)
    assert end > start


def test_to_ms_honours_an_explicit_offset():
    aware = datetime.datetime(
        2026, 9, 21, tzinfo=datetime.timezone(datetime.timedelta(hours=-3))
    )
    assert _to_ms("2026-09-21T00:00:00-03:00") == int(aware.timestamp() * 1000)

"""Pure-function tests and hypothesis invariants for the slot engine."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from apps.bookings.availability import _ceil_to_grid, free_starts

TZ = ZoneInfo("Asia/Karachi")
DAY = date(2026, 11, 3)
EARLY = datetime(2020, 1, 1, tzinfo=TZ)


def at(h: int, m: int = 0) -> datetime:
    return datetime.combine(DAY, time(h, m), tzinfo=TZ)


def run(**over):  # type: ignore[no-untyped-def]
    args = {
        "day": DAY,
        "tz": TZ,
        "windows": [(time(10), time(12))],
        "duration": timedelta(minutes=30),
        "interval_minutes": 30,
        "buffer": timedelta(0),
        "blocks": [],
        "busy": [],
        "capacity": 1,
        "earliest": EARLY,
    }
    args.update(over)
    return free_starts(**args)


def hhmm(starts: list[datetime]) -> list[str]:
    return [s.strftime("%H:%M") for s in starts]


def test_basic_grid_includes_last_fitting_start() -> None:
    assert hhmm(run()) == ["10:00", "10:30", "11:00", "11:30"]


def test_duration_longer_than_window_gives_nothing() -> None:
    assert run(duration=timedelta(hours=3)) == []


def test_exact_fit_allowed() -> None:
    assert hhmm(run(duration=timedelta(hours=2))) == ["10:00"]


def test_grid_aligned_to_clock_not_window_open() -> None:
    assert hhmm(run(windows=[(time(10, 10), time(12))])) == ["10:30", "11:00", "11:30"]


def test_split_shifts() -> None:
    out = run(windows=[(time(10), time(11)), (time(15), time(16))])
    assert hhmm(out) == ["10:00", "10:30", "15:00", "15:30"]


def test_no_windows_closed_day() -> None:
    assert run(windows=[]) == []


def test_busy_blocks_overlap_but_not_adjacent() -> None:
    out = run(busy=[(at(10, 30), at(11, 0))])
    assert hhmm(out) == ["10:00", "11:00", "11:30"]


def test_buffer_after_booking_and_before_next() -> None:
    out = run(busy=[(at(10, 30), at(11, 0))], buffer=timedelta(minutes=30))
    # 10:00 would butt up against the booking without the buffer; 11:00 starts inside it
    assert hhmm(out) == ["11:30"]


def test_closure_blocks_any_overlap() -> None:
    assert hhmm(run(blocks=[(at(10, 45), at(11, 5))])) == ["10:00", "11:30"]


def test_closure_not_extended_by_buffer() -> None:
    out = run(blocks=[(at(10, 30), at(11, 0))], buffer=timedelta(minutes=30))
    assert hhmm(out) == ["10:00", "11:00", "11:30"]


def test_capacity_two_allows_one_overlap() -> None:
    busy = [(at(10), at(10, 30)), (at(10), at(10, 30)), (at(11), at(11, 30))]
    out = run(busy=busy, capacity=2)
    assert hhmm(out) == ["10:30", "11:00", "11:30"]


def test_earliest_cutoff_is_inclusive() -> None:
    assert hhmm(run(earliest=at(10, 30)))[0] == "10:30"
    assert hhmm(run(earliest=at(10, 31)))[0] == "11:00"


def test_interval_60_and_15() -> None:
    assert hhmm(run(interval_minutes=60, duration=timedelta(minutes=60))) == ["10:00", "11:00"]
    assert len(run(interval_minutes=15, duration=timedelta(minutes=15))) == 8


def test_ceil_to_grid() -> None:
    assert _ceil_to_grid(at(10, 0), 30) == at(10, 0)
    assert _ceil_to_grid(at(10, 1), 30) == at(10, 30)
    assert _ceil_to_grid(at(23, 50), 30) == at(23, 30) + timedelta(hours=0, minutes=30)


def test_utc_vs_karachi_midnight_crossing() -> None:
    # 00:30 PKT is 19:30 UTC of the previous day; slots must be computed on the local date
    out = run(windows=[(time(0), time(1))], duration=timedelta(minutes=30))
    assert [s.isoformat() for s in out] == [
        "2026-11-03T00:00:00+05:00",
        "2026-11-03T00:30:00+05:00",
    ]
    assert out[0].astimezone(ZoneInfo("UTC")).date() == date(2026, 11, 2)


def test_leap_day() -> None:
    leap = date(2028, 2, 29)
    out = free_starts(
        day=leap,
        tz=TZ,
        windows=[(time(10), time(11))],
        duration=timedelta(minutes=60),
        interval_minutes=60,
        buffer=timedelta(0),
        blocks=[],
        busy=[],
        capacity=1,
        earliest=EARLY,
    )
    assert [s.date() for s in out] == [leap]


# ---- hypothesis invariants ----

minutes = st.integers(min_value=0, max_value=24 * 60 - 1)


@st.composite
def windows_strategy(draw):  # type: ignore[no-untyped-def]
    cuts = sorted(draw(st.lists(st.integers(0, 24 * 60), min_size=2, max_size=6, unique=True)))
    pairs = list(zip(cuts[::2], cuts[1::2], strict=False))
    return [
        (time(a // 60, a % 60), time(b // 60 % 24, b % 60) if b < 1440 else time(23, 59))
        for a, b in pairs
        if b > a
    ]


@st.composite
def intervals_strategy(draw):  # type: ignore[no-untyped-def]
    out = []
    for _ in range(draw(st.integers(0, 6))):
        start = draw(st.integers(-60, 24 * 60 + 60))
        length = draw(st.integers(5, 240))
        out.append((at(0) + timedelta(minutes=start), at(0) + timedelta(minutes=start + length)))
    return out


@settings(max_examples=150, deadline=None)
@given(
    windows=windows_strategy(),
    blocks=intervals_strategy(),
    busy=intervals_strategy(),
    interval=st.sampled_from([15, 30, 60]),
    duration=st.integers(5, 180),
    buffer=st.sampled_from([0, 5, 15, 30]),
    earliest_min=st.integers(0, 24 * 60),
)
def test_invariants(windows, blocks, busy, interval, duration, buffer, earliest_min):  # type: ignore[no-untyped-def]
    dur, buf = timedelta(minutes=duration), timedelta(minutes=buffer)
    earliest = at(0) + timedelta(minutes=earliest_min)
    starts = free_starts(
        day=DAY,
        tz=TZ,
        windows=windows,
        duration=dur,
        interval_minutes=interval,
        buffer=buf,
        blocks=blocks,
        busy=busy,
        capacity=1,
        earliest=earliest,
    )
    assert starts == sorted(set(starts))
    for s in starts:
        e = s + dur
        # inside at least one window
        assert any(
            datetime.combine(DAY, o, tzinfo=TZ) <= s and e <= datetime.combine(DAY, c, tzinfo=TZ)
            for o, c in windows
        )
        # aligned to the interval on the local clock
        assert (s.hour * 60 + s.minute) % interval == 0 and s.second == 0
        assert s >= earliest
        for bs, be in blocks:
            assert not (s < be and bs < e)
        for bs, be in busy:
            assert not (s < be + buf and bs < e + buf)


@settings(max_examples=60, deadline=None)
@given(busy=intervals_strategy(), capacity=st.integers(1, 4))
def test_capacity_never_exceeded(busy, capacity):  # type: ignore[no-untyped-def]
    dur = timedelta(minutes=30)
    starts = free_starts(
        day=DAY,
        tz=TZ,
        windows=[(time(0), time(23, 59))],
        duration=dur,
        interval_minutes=30,
        buffer=timedelta(0),
        blocks=[],
        busy=busy,
        capacity=capacity,
        earliest=EARLY,
    )
    for s in starts:
        overlapping = sum(1 for bs, be in busy if s < be and bs < s + dur)
        assert overlapping < capacity


@settings(max_examples=60, deadline=None)
@given(busy=intervals_strategy())
def test_more_busy_never_adds_slots(busy):  # type: ignore[no-untyped-def]
    base = {
        "day": DAY,
        "tz": TZ,
        "windows": [(time(9), time(18))],
        "duration": timedelta(minutes=45),
        "interval_minutes": 15,
        "buffer": timedelta(minutes=10),
        "blocks": [],
        "capacity": 1,
        "earliest": EARLY,
    }
    full = set(free_starts(busy=[], **base))  # type: ignore[arg-type]
    some = set(free_starts(busy=busy, **base))  # type: ignore[arg-type]
    assert some <= full

"""The price collar (backlog T10): an entry whose final limit is implausibly far from the last
completed bar's close or the previous quote's ask is refused with one cause recording both
distances. Off at 0 on each side, as shipped. Every bar and quote here is SYNTHETIC: made-up
prices around the conftest's 10.00-10.20 window, not captured market data."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from baseline import load_baseline

from tickr.api.reasons import refusal_params
from tickr.config import ConfigError, load
from tickr.risk.admission import admit, collar_distance
from tickr.risk.causes import AdmissionCause as C
from tickr.types import Bar

from .conftest import NOW, ROOT, D, inputs, quote

# SYNTHETIC references: the decision bar closed at 10.10 and the previous ask was 10.09, so the
# conftest's 10.20 limit is 0.0099 from the close and 0.0109 from the previous ask.
BAR = Bar("ABCD", NOW - timedelta(minutes=1), D("10.00"), D("10.15"), D("9.95"), D("10.10"),
          D(5000))
PREVIOUS = quote(ask=D("10.09"), provider_time=NOW - timedelta(seconds=2))


def collared(lim, bar="0", quote_side="0"):
    return replace(lim, collar_bar_ratio=D(bar), collar_quote_ratio=D(quote_side))


def with_references(**changes):
    return inputs(**{"last_bar": BAR, "previous_quote": PREVIOUS, **changes})


def test_shipped_off_and_off_needs_no_references(lim):
    shipped = load(ROOT / "config" / "bot.toml").risk
    assert (shipped.entry_collar_bar_ratio, shipped.entry_collar_quote_ratio) == (0, 0)
    assert (lim.collar_bar_ratio, lim.collar_quote_ratio) == (0, 0)
    assert load_baseline().risk.entry_collar_bar_ratio == 0
    # Off: an absurd or missing reference changes nothing.
    assert admit(inputs(), lim).ok
    far = replace(BAR, close=D("1.00"))
    assert admit(inputs(last_bar=far, previous_quote=None), lim).ok


def test_within_both_sides_is_admitted(lim):
    assert admit(with_references(), collared(lim, "0.02", "0.02")).ok
    # Exactly at the maximum is inside: 10.20 is 0.02 above a 10.00 close.
    at_max = replace(BAR, close=D("10.00"))
    assert admit(with_references(last_bar=at_max), collared(lim, "0.02")).ok


@pytest.mark.parametrize(("bar", "quote_side", "close", "ask"), [
    ("0.05", "0", "9.00", "10.09"),   # bar side on, 10.20 is 0.1333 from 9.00
    ("0", "0.05", "10.10", "9.50"),   # quote side on, 0.0737 from 9.50
    ("0.05", "0.05", "11.00", "10.09"),  # both on, the limit far BELOW the bar close
])
def test_a_breach_is_refused_with_both_distances(lim, bar, quote_side, close, ask):
    result = admit(with_references(last_bar=replace(BAR, close=D(close)),
                                   previous_quote=replace(PREVIOUS, ask=D(ask))),
                   collared(lim, bar, quote_side))
    assert not result.ok and result.cause is C.PRICE_COLLAR_BREACH
    params = refusal_params(str(result.cause), result.detail, None, None)
    assert params == {
        "limit": "10.20", "bar_close": close, "bar_max": bar, "previous_ask": ask,
        "quote_max": quote_side,
        "bar_distance": f"{collar_distance(D('10.20'), D(close)):.4f}",
        "quote_distance": f"{collar_distance(D('10.20'), D(ask)):.4f}"}


@pytest.mark.parametrize("changes", [
    dict(last_bar=None),
    dict(last_bar=replace(BAR, symbol="WXYZ")),
    dict(last_bar=replace(BAR, close=D(0))),
    dict(last_bar=replace(BAR, close=D("NaN"))),
    dict(last_bar=replace(BAR, start=NOW)),
    dict(last_bar=replace(BAR, start=NOW.replace(tzinfo=None) - timedelta(minutes=1))),
    dict(previous_quote=None),
    dict(previous_quote=replace(PREVIOUS, symbol="WXYZ")),
    dict(previous_quote=replace(PREVIOUS, ask=D(0))),
    dict(previous_quote=replace(PREVIOUS, provider_time=NOW)),  # after the current quote
    # Older than [risk] max_quote_age_seconds (5 s in the baseline), the current quote's bound.
    dict(previous_quote=replace(PREVIOUS, provider_time=NOW - timedelta(seconds=6))),
])
@pytest.mark.parametrize("side", ["bar", "quote"])
def test_while_either_side_is_on_both_references_are_needed(lim, changes, side):
    limits = collared(lim, "0.5") if side == "bar" else collared(lim, quote_side="0.5")
    result = admit(with_references(**changes), limits)
    assert not result.ok and result.cause is C.PRICE_COLLAR_UNKNOWN, result.detail
    assert result.detail


def test_the_collar_runs_after_the_quote_window(lim):
    """Check 5's earlier parts keep their own cause: the collar is its last part."""
    result = admit(inputs(quote=quote(ask=D("10.50")), last_bar=None),
                   collared(lim, "0.01", "0.01"))
    assert result.cause is C.PRICE_OUTSIDE_WINDOW


def test_negative_maxima_are_refused_by_config(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    (tmp_path / "config").mkdir()
    bad = tmp_path / "config" / "bot.toml"
    bad.write_text(text.replace("entry_collar_quote_ratio = 0", "entry_collar_quote_ratio = -0.1"),
                   "utf-8")
    with pytest.raises(ConfigError, match="entry_collar"):
        load(bad)


def test_the_previous_quote_is_held_to_the_quote_age_limit(lim):
    """The previous quote may be as old as the current one may (`[risk]
    max_quote_age_seconds` in the regular window), no older: a stale quote is no reference."""
    limits = collared(lim, quote_side="0.5")
    oldest = replace(PREVIOUS, provider_time=NOW - timedelta(seconds=float(
        lim.max_quote_age_seconds)))
    assert admit(with_references(previous_quote=oldest), limits).ok
    stale = replace(oldest, provider_time=oldest.provider_time - timedelta(milliseconds=1))
    result = admit(with_references(previous_quote=stale), limits)
    assert result.cause is C.PRICE_COLLAR_UNKNOWN
    assert result.detail == "previous quote 5.001 s old > 5 s (regular)"

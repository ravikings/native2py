"""Shared normalization for the date bounds used to filter recorded calls.

Lives outside both routers because the calls page and the evidence pack must
answer the *same* question for the same range: they previously normalized
differently (the page not at all), so a bare `until` under-reported the final
day on screen while the signed export for the identical range included it —
two different numbers, neither flagged as wrong.
"""

from __future__ import annotations

import re
from datetime import datetime


class BadTimeBound(ValueError):
    """A since/until field that isn't a date this can compare against."""


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?$")


def _require_real_date(candidate: str, shown: str) -> None:
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BadTimeBound(f"Not a real date: {shown!r}.") from exc


def normalize_bound(raw: str | None, end_of_day: bool) -> str | None:
    """Turn a date form field into the ISO string `ts` is compared against.

    The form takes dates, but `ts` holds full timestamps; an unextended
    `until` of "2026-03-31" would exclude everything that happened that day,
    silently truncating a quarter's evidence by one day.

    Anything not a recognisable date is rejected rather than passed through.
    An unparsed bound would compare lexicographically against every stored
    `ts`, match nothing, and hand back a validly-signed but *empty* pack —
    a typo silently producing "there is no evidence" is the worst failure
    this feature has.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if _DATE.match(value):
        _require_real_date(value, value)
        return f"{value}T23:59:59.999999" if end_of_day else f"{value}T00:00:00"
    if _DATETIME.match(value):
        # The regexes only check digit grouping, so "2026-13-45" and
        # "…T25:99" match. Left unvalidated they normalize into a bound that
        # can never equal a real timestamp, quietly yielding an empty result
        # set — the same silent "there is no evidence" outcome this function
        # exists to prevent, just reached a different way.
        _require_real_date(value.replace(" ", "T"), value)
        return value.replace(" ", "T")

    # A domain error, not an HTTPException: the two callers want different
    # outcomes. A pack download can fail outright, but the calls page must
    # still render — losing the whole page (build filter, pagination, the
    # history itself) because one date field has a typo in it is a worse
    # answer than showing the page with the filter flagged.
    raise BadTimeBound(f"Not a date: {value!r}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM.")


def to_datetime_local(raw: str | None) -> str:
    """Best-effort ``YYYY-MM-DDTHH:MM`` for an ``<input type="datetime-local">``.

    ``normalize_bound`` accepts more shapes than that control can render — a
    bare date, a space instead of "T", trailing seconds/microseconds. Feeding
    any of those back as the input's ``value`` makes the browser reject it
    and render the field blank, which then looks unfilled and silently drops
    the bound the moment the form is resubmitted. An unconvertible value
    returns "" for the same reason: an empty field is honest, a stale one
    that resubmits as nothing is not.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if _DATE.match(value):
        return f"{value}T00:00"
    if _DATETIME.match(value):
        return value.replace(" ", "T")[:16]
    return ""

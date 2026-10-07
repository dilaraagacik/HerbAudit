"""Date parsing / accuracy scoring."""
from __future__ import annotations

import re


def _lazy_imports():
    """Import dateutil.parser on first use, not at CLI startup."""
    global dateutil_parser
    from dateutil import parser as dateutil_parser

dateutil_parser = None


def _parse_date_components(s):
    """
    Parse a date string into (precision, year, month, day).

    Precision levels:
      3 = full date  (year + month + day)
      2 = year+month (day absent or explicitly 00)
      1 = year only
      0 = unparseable

    Handles ISO-style strings like "1854-05", "1854-05-00", "1854-05-12",
    as well as free-text dates via dateutil (fuzzy=True).
    Day value of "00" or "0" is treated as "not provided" → precision 2.
    """
    s = s.strip()
    # Try structured ISO-like split first (YYYY[-MM[-DD]])
    iso_parts = s.split("-")
    # Handle bare year "1854" — must be caught before dateutil guesses current month/day
    if len(iso_parts) == 1 and iso_parts[0].isdigit() and len(iso_parts[0]) == 4:
        return 1, int(iso_parts[0]), None, None
    if len(iso_parts) in (2, 3) and iso_parts[0].isdigit() and len(iso_parts[0]) == 4:
        try:
            year  = int(iso_parts[0])
            month = int(iso_parts[1]) if len(iso_parts) >= 2 else None
            day_raw = iso_parts[2] if len(iso_parts) == 3 else None
            # day "00" or "0" explicitly means the day is absent
            day = int(day_raw) if (day_raw and day_raw.lstrip("0") != "") else None
            if day == 0:
                day = None
            if year and month and day:
                return 3, year, month, day
            if year and month:
                return 2, year, month, None
            if year:
                return 1, year, None, None
        except ValueError:
            pass

    # Fallback: try dateutil fuzzy parse (handles "May 1854", "12 Jun 1923", etc.)
    try:
        d = dateutil_parser.parse(s, fuzzy=True).date()
        # dateutil always fills in missing parts with defaults, so we can't tell
        # whether the day was genuinely present. Use the original string to decide.
        nums = re.findall(r"\b\d+\b", s)
        has_day_hint = any(1 <= int(n) <= 31 and len(n) <= 2 for n in nums
                           if n not in (str(d.year), f"{d.month:02d}", str(d.month)))
        precision = 3 if has_day_hint else 2
        return precision, d.year, d.month, d.day if precision == 3 else None
    except (ValueError, OverflowError):
        pass

    return 0, None, None, None


def date_accuracy(target, extraction):
    """
    Compare two date strings and return a partial-credit score [0.0 – 1.0].

    Scoring is precision-aware based on the TRUTH (GBIF) date:
      • If truth has only year+month, AI is scored against year+month only —
        a correct year+month (even written as YYYY-MM-00) scores 1.0.
      • If truth has a full date (year+month+day), the AI must also supply the
        day to receive full credit; omitting the day is penalised.
      • Supplying *fewer* components than the truth has is always penalised.
    """
    t_str = str(target).split("T")[0].strip()
    e_str = str(extraction).split("T")[0].strip()

    null_values = {"na", "none", "nan", "n/a", ""}
    if not t_str or not e_str or t_str.lower() in null_values or e_str.lower() in null_values:
        return 0.0

    t_prec, t_year, t_month, t_day = _parse_date_components(t_str)
    e_prec, e_year, e_month, e_day = _parse_date_components(e_str)

    # If neither side parsed at all, give up
    if t_prec == 0 or e_prec == 0:
        return 0.0

    # Year must always match
    if t_year != e_year:
        return 0.0

    if t_prec == 1:
        return 1.0   # year matched above

    if t_prec == 2:
        if e_prec < 2:
            # AI gave only a year — partial credit
            return 0.50
        if t_month != e_month:
            return 0.50   # same year, wrong month
        return 1.0        # year+month match; extra day info from AI is irrelevant

    if t_prec == 3:
        if t_month != e_month:
            # Day/month transposition — a very common notation mixup (e.g. a label
            # written day-month order reformatted to YYYY-MM-DD the wrong way round),
            # not a real content error. "1948-04-06" vs "1948-06-04" is the same date,
            # just day and month swapped.
            if e_prec == 3 and e_day is not None and t_month == e_day and t_day == e_month:
                return 0.90
            return 0.50   # same year, wrong month

        # Month matches — check day
        if e_prec < 3 or e_day is None:
            # AI omitted the day even though truth has one → penalise
            return 0.75   # got year+month right but missing day

        # Both have a day — compare
        diff = abs(t_day - e_day)
        if diff == 0:
            return 1.0
        if diff == 1:
            return 0.95   # off by one — likely a transcription typo
        if diff <= 7:
            return 0.85   # within a week
        return 0.75       # same year+month, wrong day

    return 0.0  # unreachable, but safe default

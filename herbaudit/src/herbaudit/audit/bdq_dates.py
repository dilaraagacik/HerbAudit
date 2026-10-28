"""Date parsing for the BDQ date amendments, translated by hand from FilteredPush event_date_qc v3.0.5
(org.filteredpush.qc.date.util.DateUtils, Apache License 2.0), so results match the reference implementation."""
from __future__ import annotations

import re

# English month names (LLL = short, LLLL = full), as Java parses them. Only English is needed:
# every formatter in the reference code ends up parsing in English (see the multi-locale block).
MONTH_NAMES = {
    'en': (['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
           ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']),
}
DAY_NAMES_EN = (['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
                ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'])

_CLEAN_MONTH_OPS = [
    ('replace', 'Sept.', 'Sep.'),
    ('replace', 'sEPT.', 'Sep.'),
    ('replace', 'SEpt.', 'Sep.'),
    ('replace', 'Sept ', 'Sep. '),
    ('replace', 'Sept-', 'Sep-'),
    ('replace', 'Sept,', 'Sep.,'),
    ('replace', 'Sept/', 'Sep./'),
    ('replace', 'OCt.', 'Oct.'),
    ('replace', 'OCt-', 'Oct-'),
    ('replace', 'OCt ', 'Oct '),
    ('replace', 'JAn.', 'Jan.'),
    ('replace', 'FEb.', 'Feb.'),
    ('replace', 'MAr.', 'Mar.'),
    ('replace', 'APr.', 'APr.'),
    ('replace', 'JUn.', 'Jun.'),
    ('replace', 'JUl.', 'Jul.'),
    ('replace', 'AUg.', 'Aug.'),
    ('replace', 'NOv.', 'Nov.'),
    ('replace', 'DEc.', 'Dec.'),
    ('replace_trim', '  ', ' '),
    ('replace', ' ,', ','),
    ('replace', ' - ', '-'),
    ('replace', '- ', '-'),
    ('replace', ' -', '-'),
    ('replaceall_if_matches', '^.*[0-9]{4}[.]$', '[.]$', ''),
    ('replace', 'Jly. ', 'July '),
    ('replace', 'Jly ', 'July '),
    ('replace', 'Febr.', 'February'),
    ('replace', 'Jully ', 'July '),
    ('replace', 'Septmber', 'September'),
    ('replace', 'Febuary', 'February'),
    ('replace', 'Janauary', 'January'),
    ('replace', 'DECEMBER', 'December'),
    ('replace', 'NOVEMBER', 'November'),
    ('replace', 'OCTOBER', 'October'),
    ('replace', 'SEPTEMBER', 'September'),
    ('replace', 'AUGUST', 'August'),
    ('replace', 'JULY', 'July'),
    ('replace', 'JUNE', 'June'),
    ('replace', 'MAY', 'May'),
    ('replace', 'APRIL', 'April'),
    ('replace', 'MARCH', 'March'),
    ('replace', 'FEBRUARY', 'February'),
    ('replace', 'JANUARY', 'January'),
    ('replace', 'DEcember', 'December'),
    ('replace', 'NOvember', 'November'),
    ('replace', 'OCtober', 'October'),
    ('replace', 'SEptember', 'September'),
    ('replace', 'AUgust', 'August'),
    ('replace', 'JUly', 'July'),
    ('replace', 'JUne', 'June'),
    ('replace', 'MAy', 'May'),
    ('replace', 'APril', 'April'),
    ('replace', 'MArch', 'March'),
    ('replace', 'FEbruary', 'February'),
    ('replace', 'JAnuary', 'January'),
    ('replace', 'DEC', 'Dec'),
    ('replace', 'NOV', 'Nov'),
    ('replace', 'OCT', 'Oct'),
    ('replace', 'SEP', 'Sep'),
    ('replace', 'SEP', 'Sep'),
    ('replace', 'AUG', 'Aug'),
    ('replace', 'JUL', 'Jul'),
    ('replace', 'JUN', 'Jun'),
    ('replace', 'APR', 'Apr'),
    ('replace', 'MAR', 'Mar'),
    ('replace', 'FEB', 'Feb'),
    ('replace', 'JAN', 'Jan'),
    ('replace', 'dec ', 'Dec '),
    ('replace', 'nov ', 'Nov '),
    ('replace', 'oct ', 'Oct '),
    ('replace', 'sep ', 'Sep '),
    ('replace', 'aug ', 'Aug '),
    ('replace', 'jul ', 'Jul '),
    ('replace', 'jun ', 'Jun '),
    ('replace', 'apr ', 'Apr '),
    ('replace', 'mar ', 'Mar '),
    ('replace', 'feb ', 'Feb '),
    ('replace', 'jan ', 'Jan '),
    ('replace', 'dECEMBER', 'December'),
    ('replace', 'nOVEMBER', 'November'),
    ('replace', 'oCTOBER', 'October'),
    ('replace', 'sEPTEMBER', 'September'),
    ('replace', 'aUGUST', 'August'),
    ('replace', 'jULY', 'July'),
    ('replace', 'jUNE', 'June'),
    ('replace', 'mAY', 'May'),
    ('replace', 'aPRIL', 'April'),
    ('replace', 'mARCH', 'March'),
    ('replace', 'fEBRUARY', 'February'),
    ('replace', 'jANUARY', 'January'),
    ('replace', 'Mrch', 'March'),
    ('replace', 'Jnry', 'January'),
    ('replace', 'Februar', 'February'),
    ('replace', 'january', 'January'),
    ('replace', 'february', 'February'),
    ('replace', 'march', 'March'),
    ('replace', 'april', 'April'),
    ('replace', 'may', 'May'),
    ('replace', 'june', 'June'),
    ('replace', 'july', 'July'),
    ('replace', 'august', 'August'),
    ('replace', 'september', 'Sepember'),
    ('replace', 'october', 'October'),
    ('replace', 'november', 'November'),
    ('replace', 'december', 'December'),
    ('replace', 'Dicembre', 'December'),
    ('replace', 'dicembre', 'December'),
    ('replace', 'Novembre', 'November'),
    ('replace', 'novembre', 'November'),
    ('replace', 'Ottobre', 'October'),
    ('replace', 'ottobre', 'October'),
    ('replace', 'Settembre', 'September'),
    ('replace', 'settembre', 'September'),
    ('replace', 'Agosto', 'August'),
    ('replace', 'agosto', 'August'),
    ('replace', 'Luglio', 'July'),
    ('replace', 'luglio', 'July'),
    ('replace', 'Giugno', 'June'),
    ('replace', 'giugno', 'June'),
    ('replace', 'Maggio', 'May'),
    ('replace', 'maggio', 'May'),
    ('replace', 'Aprile', 'April'),
    ('replace', 'aprile', 'April'),
    ('replace', 'Marzo', 'March'),
    ('replace', 'marzo', 'March'),
    ('replace', 'Febbraio', 'February'),
    ('replace', 'febbraio', 'February'),
    ('replace', 'Gennaio', 'January'),
    ('replace', 'gennaio', 'January'),
    ('replace', 'Janvier', 'January'),
    ('replace', 'janvier', 'January'),
    ('replace', 'janv', 'January'),
    ('replace', 'Février', 'February'),
    ('replace', 'Fevrier', 'February'),
    ('replace', 'fevrier', 'February'),
    ('replace', 'Mars', 'March'),
    ('replace', 'mars', 'March'),
    ('replace', 'Avril', 'April'),
    ('replace', 'avril', 'April'),
    ('replace', 'Mai', 'May'),
    ('replace', 'Juin', 'June'),
    ('replace', 'juin', 'June '),
    ('replace', 'Juillet', 'July'),
    ('replace', 'juillet', 'July'),
    ('replace', 'Août', 'August'),
    ('replace', 'août', 'August'),
    ('replace', 'Aout', 'August'),
    ('replace', 'aout', 'August'),
    ('replace', 'Septembre', 'September'),
    ('replace', 'septembre', 'September'),
    ('replace', 'Octobre', 'October'),
    ('replace', 'octobre', 'October'),
    ('replace', 'Novembre', 'November'),
    ('replace', 'novembre', 'November'),
    ('replace', 'Décembre', 'December'),
    ('replace', 'Decembre', 'December'),
    ('replace', 'decembre', 'December'),
    ('replace', 'Enero', 'January'),
    ('replace', 'enero', 'January'),
    ('replace', 'Febrero', 'February'),
    ('replace', 'febrero', 'February'),
    ('replace', 'Marzo', 'March'),
    ('replace', 'marzo', 'March'),
    ('replace', 'Abril', 'April'),
    ('replace', 'abril', 'April'),
    ('replace', 'Mayo', 'May'),
    ('replace', 'mayo', 'May'),
    ('replace', 'Junio', 'June'),
    ('replace', 'junio', 'June'),
    ('replace', 'Julio', 'July'),
    ('replace', 'julio', 'July'),
    ('replace', 'Agosto', 'August'),
    ('replace', 'agosto', 'August'),
    ('replace', 'Septiembre', 'September'),
    ('replace', 'septiembre', 'September'),
    ('replace', 'Setiembre', 'September'),
    ('replace', 'setiembre', 'September'),
    ('replace', 'Octubre', 'October'),
    ('replace', 'octubre', 'October'),
    ('replace', 'Noviembre', 'November'),
    ('replace', 'noviembre', 'November'),
    ('replace', 'Diciembre', 'December'),
    ('replace', 'diciembre', 'December'),
    ('replace', 'januar', 'January'),
    ('replace', 'februar', 'February'),
    ('replace', 'marts', 'March'),
    ('replace', 'maj', 'May'),
    ('replace', 'juni', 'June'),
    ('replace', 'juli', 'July'),
    ('replace', 'oktober', 'October'),
    ('replace', 'Ionawr', 'January'),
    ('replace', 'Chwefror', 'February'),
    ('replace', 'Mawrth', 'March'),
    ('replace', 'Ebrill', 'April'),
    ('replace', 'Mai', 'May'),
    ('replace', 'mai ', 'May '),
    ('replace', 'mai-', 'May-'),
    ('replace', 'mai,', 'May,'),
    ('replace', 'mai.', 'May.'),
    ('replace', 'Mehefin', 'June'),
    ('replace', 'Gorffennaf', 'July'),
    ('replace', 'Awst', 'August'),
    ('replace', 'Medi', 'September'),
    ('replace', 'Hydref', 'October'),
    ('replace', 'Tachwedd', 'November'),
    ('replace', 'Rhagfyr', 'December'),
    ('replace', '.i.', '.January.'),
    ('replace', '/i/', '/January/'),
    ('replace', ' i ', ' January '),
    ('replace', '.ii.', '.February.'),
    ('replace', '/ii/', '/February/'),
    ('replace', ' ii ', ' February '),
    ('replace', '.v.', '.May.'),
    ('replace', '/v/', '/May/'),
    ('replace', ' v ', ' May '),
    ('replace', '.iv.', '.April.'),
    ('replace', '/iv/', '/April/'),
    ('replace', ' iv ', ' April '),
    ('replace', '.vi.', '.June.'),
    ('replace', '/vi/', '/June/'),
    ('replace', ' vi ', ' June '),
    ('replace', '.x.', '.October.'),
    ('replace', '/x/', '/October/'),
    ('replace', ' x ', ' October '),
    ('replace', '.ix.', '.September.'),
    ('replace', '/ix/', '/September/'),
    ('replace', ' ix ', ' September '),
    ('replace', '/xi/', '/November/'),
    ('replace', '.xi.', '.November.'),
    ('replace', ' xi ', ' November '),
    ('replace', ',i,', '.January.'),
    ('replace', '-i-', ' January '),
    ('replace', ',ii,', '.February.'),
    ('replace', '-ii-', ' February '),
    ('replace', ',v,', '.May.'),
    ('replace', '-v-', ' May '),
    ('replace', ',iv,', '.April.'),
    ('replace', '-iv-', ' April '),
    ('replace', ',vi,', '.June.'),
    ('replace', '-vi-', ' June '),
    ('replace', ',x,', '.October.'),
    ('replace', '-x-', ' October '),
    ('replace', ',ix,', '.September.'),
    ('replace', '-ix-', ' September '),
    ('replace', ',xi,', '.November.'),
    ('replace', '-xi-', ' November '),
    ('replace', 'XII', 'December'),
    ('replace', 'xii', 'December'),
    ('replace', 'XI', 'November'),
    ('replace', 'xi', 'November'),
    ('replace', 'IX', 'September'),
    ('replace_unless_matches', '.*XX.*', 'X', 'October'),
    ('replace', 'VIII', 'August'),
    ('replace', 'viii', 'August'),
    ('replace', 'VII', 'July'),
    ('replace', 'vii', 'July'),
    ('replace', 'VI', 'June'),
    ('replace_unless_contains', 'janvier', 'vi', 'June'),
    ('replace', 'IV', 'April'),
    ('replace', 'iv', 'April'),
    ('replace', 'V', 'May'),
    ('replace', 'III', 'March'),
    ('replace', 'iii', 'March'),
    ('replace', 'II', 'February'),
    ('replace', 'ii', 'February'),
    ('replace', 'I', 'January'),
    ('replace', '.vi', '.June'),
    ('replace', '.ix', '.September'),
    ('replace', '.i', '.January'),
    ('replace', '.v', '.May'),
    ('replace_unless_matches', '.*\\.xx.*', '.x', '.October'),
    ('replace', ' vi', ' June'),
    ('replace', ' ix', ' September'),
    ('replace', ' i', ' January'),
    ('replace', ' v', ' May'),
    ('replace', ' x', ' October'),
    ('replace', '月', ''),
    ('replace', '三', 'March'),
    ('replace', '四', 'April'),
    ('replace', '五', 'May'),
    ('replace', '六', 'June'),
    ('replace', '七', 'July'),
    ('replace', '八', 'August'),
    ('replace', '九', 'September'),
    ('replace', '十一', 'November'),
    ('replace', '十二', 'December'),
    ('replace', '十', 'October'),
    ('replace', '一', 'January'),
    ('replace', '二', 'February'),
]



# --- A date that, like Java's LocalDate, allows year 0 and negative years ---------------

class JDate:
    """Proleptic Gregorian date (Java LocalDate semantics), so year 0000 behaves as in Java."""

    __slots__ = ("year", "month", "day")

    def __init__(self, year: int, month: int, day: int):
        if not -999_999_999 <= year <= 999_999_999:
            raise ValueError("year")
        if not 1 <= month <= 12:
            raise ValueError("month")
        if not 1 <= day <= _month_length(year, month):
            raise ValueError("day")
        self.year, self.month, self.day = year, month, day

    def _key(self):
        return (self.year, self.month, self.day)

    def __eq__(self, other):
        return isinstance(other, JDate) and self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self._key() <= other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f"JDate({iso_local_date(self)})"

    def epoch_day(self) -> int:
        # Port of java.time.LocalDate.toEpochDay
        y, m = self.year, self.month
        total = 365 * y
        if y >= 0:
            total += (y + 3) // 4 - (y + 99) // 100 + (y + 399) // 400
        else:
            total -= y // -4 - y // -100 + y // -400
        total += (367 * m - 362) // 12
        total += self.day - 1
        if m > 2:
            total -= 1 if _is_leap(y) else 2
        return total - 719528

    @staticmethod
    def of_epoch_day(epoch_day: int) -> "JDate":
        # Port of java.time.LocalDate.ofEpochDay
        zero_day = epoch_day + 719528 - 60
        adjust = 0
        if zero_day < 0:
            adjust_cycles = (zero_day + 1) // 146097 - 1
            adjust = adjust_cycles * 400
            zero_day += -adjust_cycles * 146097
        year_est = (400 * zero_day + 591) // 146097
        doy_est = zero_day - (365 * year_est + year_est // 4 - year_est // 100 + year_est // 400)
        if doy_est < 0:
            year_est -= 1
            doy_est = zero_day - (365 * year_est + year_est // 4 - year_est // 100 + year_est // 400)
        year_est += adjust
        march_doy0 = doy_est
        march_month0 = (march_doy0 * 5 + 2) // 153
        month = (march_month0 + 2) % 12 + 1
        dom = march_doy0 - (march_month0 * 306 + 5) // 10 + 1
        year_est += march_month0 // 10
        return JDate(year_est, month, dom)

    def plus_days(self, n: int) -> "JDate":
        return JDate.of_epoch_day(self.epoch_day() + n)

    def day_of_year(self) -> int:
        return self.epoch_day() - JDate(self.year, 1, 1).epoch_day() + 1

    def day_of_week(self) -> int:
        """1 = Monday ... 7 = Sunday."""
        return (self.epoch_day() + 3) % 7 + 1

    def last_day_of_month(self) -> "JDate":
        return JDate(self.year, self.month, _month_length(self.year, self.month))

    def length_of_month(self) -> int:
        return _month_length(self.year, self.month)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _month_length(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def iso_local_date(d: JDate) -> str:
    """DateTimeFormatter.ISO_LOCAL_DATE"""
    if 0 <= d.year <= 9999:
        y = f"{d.year:04d}"
    elif d.year > 9999:
        y = f"+{d.year}"
    else:
        y = f"-{-d.year:04d}"
    return f"{y}-{d.month:02d}-{d.day:02d}"


def fmt_yyyy(d: JDate) -> str:
    """Pattern "yyyy" (year of era, so 1 BCE prints as 0001)."""
    yoe = d.year if d.year >= 1 else 1 - d.year
    return f"+{yoe}" if yoe > 9999 else f"{yoe:04d}"


def fmt_yyyy_mm(d: JDate) -> str:
    return f"{fmt_yyyy(d)}-{d.month:02d}"


# --- Java string helpers ------------------------------------------------------------------

def jmatches(regex: str, text: str) -> bool:
    """String.matches: the whole string must match."""
    return re.fullmatch(regex, text) is not None


def jsplit(text: str, regex: str) -> list[str]:
    """String.split: trailing empty strings are removed."""
    parts = re.split(regex, text)
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    if parts == [""] and text != "":
        return []
    return parts


def jtrim(text: str) -> str:
    """String.trim: removes characters <= U+0020 at both ends."""
    start, end = 0, len(text)
    while start < end and text[start] <= " ":
        start += 1
    while end > start and text[end - 1] <= " ":
        end -= 1
    return text[start:end]


def is_empty(value) -> bool:
    """DateUtils.isEmpty"""
    return value is None or jtrim(str(value)) == ""


def _parse_int(text: str) -> int:
    """Integer.parseInt"""
    if not re.fullmatch(r"[+-]?[0-9]+", text):
        raise ValueError(text)
    return int(text)


class DateTimeParseError(ValueError):
    pass


# --- A small emulation of java.time.format.DateTimeFormatter.ofPattern(...).parse ----------
# Supported letters: u (year), M/L (month: number or text), d (day), E (day of week),
# k/H (hour), m (minute); '...' quoted literals; any other character is a literal.

_PATTERN_CACHE: dict = {}


def _compile(pattern: str) -> list:
    if pattern in _PATTERN_CACHE:
        return _PATTERN_CACHE[pattern]
    tokens, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "'":
            j = i + 1
            lit = ""
            while j < len(pattern):
                if pattern[j] == "'":
                    if j + 1 < len(pattern) and pattern[j + 1] == "'":
                        lit += "'"
                        j += 2
                        continue
                    break
                lit += pattern[j]
                j += 1
            if j == i + 1:              # '' is a literal quote
                lit = "'"
                j = i + 1
            tokens.append(("lit", lit))
            i = j + 1
        elif c.isalpha():
            j = i
            while j < len(pattern) and pattern[j] == c:
                j += 1
            n = j - i
            if c == "u":
                tokens.append(("num", "year", n, 19, "exceeds_pad" if n == 4 else "normal"))
            elif c in "ML":
                if n <= 2:
                    tokens.append(("num", "month", n, 19 if n == 1 else 2, "normal"))
                else:
                    tokens.append(("text", "month", "short" if n == 3 else "full"))
            elif c == "d":
                tokens.append(("num", "day", n, 19 if n == 1 else 2, "normal"))
            elif c == "E":
                tokens.append(("text", "dow", "short" if n <= 3 else "full"))
            elif c in "kHm":
                field = {"k": "clockhour", "H": "hour", "m": "minute"}[c]
                tokens.append(("num", field, n, 19 if n == 1 else 2, "normal"))
            else:
                raise ValueError(f"Unsupported pattern letter {c}")
            i = j
        else:
            tokens.append(("lit", c))
            i += 1
    _PATTERN_CACHE[pattern] = tokens
    return tokens


def _names(field: str, style: str, locale: str) -> list[tuple[str, int]]:
    if field == "month":
        names = MONTH_NAMES[locale][0 if style == "short" else 1]
    else:
        names = DAY_NAMES_EN[0 if style == "short" else 1]
    pairs = [(name, i + 1) for i, name in enumerate(names)]
    # Java drops names shared by more than one value, and tries the longest first.
    counts = {}
    for name, _ in pairs:
        counts[name] = counts.get(name, 0) + 1
    return sorted([p for p in pairs if counts[p[0]] == 1], key=lambda p: -len(p[0]))


def jparse(pattern: str, text: str, locale: str = "en", strict: bool = True) -> JDate:
    """LocalDate.parse(text, DateTimeFormatter.ofPattern(pattern).withLocale(locale))."""
    values, pos = {}, 0
    for tok in _compile(pattern):
        if tok[0] == "lit":
            if not text.startswith(tok[1], pos):
                raise DateTimeParseError(text)
            pos += len(tok[1])
        elif tok[0] == "num":
            _, field, minw, maxw, sign = tok
            negative = plus = False
            if field == "year" and pos < len(text) and text[pos] in "+-":
                # A year may carry a sign: '-' always, '+' only when it exceeds the padding (strict).
                negative, plus = text[pos] == "-", text[pos] == "+"
                if plus and sign != "exceeds_pad":
                    raise DateTimeParseError(text)
                pos += 1
            start = pos
            while pos < len(text) and pos - start < maxw and "0" <= text[pos] <= "9":
                pos += 1
            count = pos - start
            if count < minw or (sign == "exceeds_pad" and not negative and (count > minw) != plus):
                raise DateTimeParseError(text)
            if negative and int(text[start:pos]) == 0:     # strict parsing rejects "-0"
                raise DateTimeParseError(text)
            values[field] = -int(text[start:pos]) if negative else int(text[start:pos])
        else:
            _, field, style = tok
            for name, value in _names(field, style, locale):
                if text.startswith(name, pos):
                    values[field] = value
                    pos += len(name)
                    break
            else:
                raise DateTimeParseError(text)
    if pos != len(text):
        raise DateTimeParseError(text)
    if not {"year", "month", "day"} <= values.keys():
        raise DateTimeParseError(text)
    day = values["day"]
    if not strict and 1 <= day <= 31 and 1 <= values["month"] <= 12:
        day = min(day, _month_length(values["year"], values["month"]))
    try:
        result = JDate(values["year"], values["month"], day)
    except ValueError:
        raise DateTimeParseError(text)
    if "dow" in values and values["dow"] != result.day_of_week():
        raise DateTimeParseError(text)
    if "clockhour" in values and not 1 <= values["clockhour"] <= 24:
        raise DateTimeParseError(text)
    if "hour" in values and not 0 <= values["hour"] <= 23:
        raise DateTimeParseError(text)
    if "minute" in values and not 0 <= values["minute"] <= 59:
        raise DateTimeParseError(text)
    return result


def try_patterns(patterns, text: str, locale: str = "en") -> JDate | None:
    """Try each pattern in turn, as the Java code's formatter loops do."""
    for pattern in patterns:
        try:
            return jparse(pattern, text, locale)
        except DateTimeParseError:
            continue
    return None


# --- The predefined ISO formatters ----------------------------------------------------

# Year as appendValue(YEAR, 4, 10, EXCEEDS_PAD): 4 digits, or a sign ("+" only past 4 digits).
_DATE = r"([0-9]{4}|-[0-9]{4,10}|\+[0-9]{5,10})-([0-9]{2})-([0-9]{2})"
_TIME = r"([01][0-9]|2[0-3]):([0-5][0-9])(:([0-5][0-9])(\.[0-9]{0,9})?)?"
_OFFSET = r"([Zz]|[+-]((0[0-9]|1[0-7]):[0-5][0-9](:[0-5][0-9])?|18:00(:00)?))"   # at most +/-18:00
# A mandatory offset (ISO_OFFSET_DATE_TIME, ISO_ZONED_DATE_TIME) may also be just +HH.
_OFFSET_REQUIRED = r"([Zz]|[+-]((0[0-9]|1[0-7])(:[0-5][0-9](:[0-5][0-9])?)?|18(:00(:00)?)?))"
_ZONE = r"\[[A-Za-z][A-Za-z0-9~/._+-]*\]"
_ISO = {
    "ISO_LOCAL_DATE": rf"{_DATE}",
    "ISO_DATE": rf"{_DATE}{_OFFSET}?",
    "ISO_LOCAL_DATE_TIME": rf"{_DATE}T{_TIME}",
    "ISO_OFFSET_DATE_TIME": rf"{_DATE}T{_TIME}{_OFFSET_REQUIRED}",
    "ISO_ZONED_DATE_TIME": rf"{_DATE}T{_TIME}{_OFFSET_REQUIRED}({_ZONE})?",
    "ISO_DATE_TIME": rf"{_DATE}T{_TIME}({_OFFSET}({_ZONE})?)?",
}


def iso_parse(name: str, text: str) -> JDate:
    """LocalDate.parse(text, DateTimeFormatter.<name>) with STRICT resolution."""
    if name == "ISO_ORDINAL_DATE":
        m = re.fullmatch(rf"([0-9]{{4}}|-[0-9]{{4,10}}|\+[0-9]{{5,10}})-([0-9]{{3}}){_OFFSET}?", text)
        if not m or not 1 <= int(m.group(2)) <= (366 if _is_leap(int(m.group(1))) else 365):
            raise DateTimeParseError(text)
        return JDate(int(m.group(1)), 1, 1).plus_days(int(m.group(2)) - 1)
    if name == "BASIC_ISO_DATE":
        m = re.fullmatch(r"([0-9]{4})([0-9]{2})([0-9]{2})(Z|[+-][0-9]{4}([0-9]{2})?)?", text)
    else:
        m = re.fullmatch(_ISO[name], text)
    if not m:
        raise DateTimeParseError(text)
    if re.fullmatch(r"-0+", m.group(1)):
        raise DateTimeParseError(text)
    try:
        return JDate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise DateTimeParseError(text)


def try_iso(names, text: str) -> JDate | None:
    for name in names:
        try:
            return iso_parse(name, text)
        except DateTimeParseError:
            continue
    return None


# --- LocalDateInterval -----------------------------------------------------------------

# Time zone / offset suffixes accepted by the optional [VV][zz][X][xx][OOOO] sections, in that order.
# Offsets are exact; zone ids and names are matched by shape (Java checks them against its zone list).
_HH = r"(0[0-9]|1[0-7])"
_ZONE_ONE_WORD_IDS = ("CET|CST6CDT|Cuba|EET|EST5EDT|Egypt|Eire|GB-Eire|GB|GMT0|GMT|Greenwich|Hongkong|Iceland|"
                      "Iran|Israel|Jamaica|Japan|Kwajalein|Libya|MET|MST7MDT|NZ-CHAT|NZ|Navajo|PRC|PST8PDT|"
                      "Poland|Portugal|ROK|Singapore|Turkey|UCT|UTC|Universal|W-SU|WET|Zulu")
_VV = (rf"(Z|(UTC|GMT|UT)([+-]({_HH}(:?[0-5][0-9](:?[0-5][0-9])?)?|18(:?00(:?00)?)?))?"
       rf"|[+-]({_HH}:[0-5][0-9](:[0-5][0-9])?|18:00(:00)?)"
       rf"|[A-Z][A-Za-z_-]*(/[A-Za-z][A-Za-z0-9_+-]*){{1,2}}|{_ZONE_ONE_WORD_IDS})")
_ZZ = r"([A-Z][A-Za-z]{1,4})"
_X = rf"(Z|[+-]({_HH}([0-5][0-9])?|18(00)?))"
_XX = rf"([+-]({_HH}[0-5][0-9]|1800))"
_OOOO = rf"(GMT([+-]({_HH}:[0-5][0-9]|18:00))?)"
_ZONE_SUFFIX = rf"{_VV}?{_ZZ}?{_X}?{_XX}?{_OOOO}?"
# The same without the localized offset [OOOO], for the formatter that has seconds.
_ZONE_SUFFIX_NO_OOOO = rf"{_VV}?{_ZZ}?{_X}?{_XX}?"


def _parses_with_time(text: str) -> bool:
    """One of LocalDateInterval's date-time formatters parses the text."""
    if try_iso(("ISO_DATE_TIME", "ISO_ZONED_DATE_TIME", "ISO_LOCAL_DATE_TIME", "ISO_OFFSET_DATE_TIME"), text):
        return True
    m = re.fullmatch(rf"{_DATE}T([0-9]{{2}})(:([0-9]{{2}}))?(.*)", text)
    if not m:
        return False
    try:
        JDate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    hour, minute, rest = int(m.group(4)), m.group(6), m.group(7)
    if not 0 <= hour <= 24 or (minute is not None and int(minute) > 59):
        return False
    return re.fullmatch(_ZONE_SUFFIX, rest) is not None


class LocalDateInterval:
    """Port of org.filteredpush.qc.date.LocalDateInterval(String)."""

    def __init__(self, date_string: str):
        if is_empty(date_string):
            raise DateTimeParseError("empty")
        if "/" in date_string:
            bits = jsplit(date_string, "/")
            if len(bits) != 2:
                raise DateTimeParseError(date_string)
            start_bit, end_bit = bits
            if re.match(r"^-[0-9]{3,4}", start_bit) and jmatches(r"^-[0-9]{3,4}", start_bit):
                start_bit = start_bit.replace("-", "") + " BCE"
            if jmatches(r"^-[0-9]{3,4}", end_bit):
                end_bit = end_bit.replace("-", "") + " BCE"
            if jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", start_bit) and jmatches(r"^[0-9]{2}$", end_bit):
                end_bit = start_bit[0:8] + end_bit
            if jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", start_bit) and jmatches(r"^[0-9]{2}-[0-9]{2}$", end_bit):
                end_bit = start_bit[0:5] + end_bit
            if jmatches(r"^[0-9]{4}-[0-9]{2}$", start_bit) and jmatches(r"^[0-9]{2}$", end_bit):
                end_bit = start_bit[0:5] + end_bit
            self.start = self._parse_bit(start_bit)[0]
            self.end = self._parse_bit(end_bit)[1]
            if self.end < self.start:
                raise DateTimeParseError(date_string)
        elif jmatches(r"^-[0-9]{3,4}", date_string):
            self.start, self.end = self._parse_bit(date_string.replace("-", "") + " BCE")
        else:
            self.start, self.end = self._parse_bit(date_string)

    @staticmethod
    def _parse_bit(bit: str) -> tuple[JDate, JDate]:
        if is_empty(bit):
            raise DateTimeParseError("empty")
        if jmatches(r"^[0-9]{1,3}$", bit):
            raise DateTimeParseError(bit)
        if "T" in bit:
            test = bit
            if jmatches(r"^.*T[0-9]{2}[.,][0-9]+$", bit) or jmatches(r"^.*T[0-9]{2}:[0-9]{2}[.,][0-9]+$", bit):
                test = re.sub(r"[.,][0-9]+$", "", bit, count=1)
            if not _parses_with_time(test):
                raise DateTimeParseError(bit)
            bit = bit[: bit.index("T")]
        result = None
        if jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", bit):
            d = iso_parse("ISO_LOCAL_DATE", bit)
            result = (d, d)
        elif jmatches(r"^[0-9]{4}-[0-9]{3}$", bit):
            d = iso_parse("ISO_ORDINAL_DATE", bit)
            result = (d, d)
        elif jmatches(r"^[0-9]{4}-[0-9]{2}$", bit):
            d = iso_parse("ISO_LOCAL_DATE", bit + "-01")
            result = (d, d.last_day_of_month())
        elif jmatches(r"^[0-9]{1,4}$", bit):
            d = iso_parse("ISO_LOCAL_DATE", f"{int(bit):04d}-01-01")
            result = (d, JDate(d.year, 12, 31))
        elif jmatches(r"^[0-9]{1,4} BCE$", bit):
            # Java: yyyy-MM-dd with era B, then one year earlier.
            year = int(_parse_int(bit[0:4].strip()) if bit[0:4].strip().isdigit() else _parse_int(bit[0:4]))
            if year < 1:                        # year of era starts at 1, so "0000 BCE" does not parse
                raise DateTimeParseError(bit)
            proleptic = 1 - year - 1
            result = (JDate(proleptic, 1, 1), JDate(proleptic, 12, 31))
        else:
            d = try_iso(("ISO_DATE", "BASIC_ISO_DATE", "ISO_DATE"), bit)
            if d:
                result = (d, d)
        if result is None:
            raise DateTimeParseError(bit)
        return result

    def is_single_day(self) -> bool:
        return self.start == self.end

    def __str__(self) -> str:
        s, e = self.start, self.end
        if self.is_single_day():
            return iso_local_date(s)
        if s.year == e.year and s.month == 1 and e.month == 12 and s.day == 1 and e.day == 31:
            return fmt_yyyy(s)
        if s.year != e.year and s.month == 1 and e.month == 12 and s.day == 1 and e.day == 31:
            return fmt_yyyy(s) + "/" + fmt_yyyy(e)
        if s.year == e.year and s.month == e.month and s.day == 1 and e.day == e.length_of_month():
            return fmt_yyyy_mm(s)
        return iso_local_date(s) + "/" + iso_local_date(e)

    def duration_seconds(self) -> int:
        return (self.end.epoch_day() + 1 - self.start.epoch_day()) * 86400


def extract_interval(text) -> LocalDateInterval | None:
    """DateUtils.extractInterval / extractDateInterval"""
    try:
        return LocalDateInterval(text)
    except (DateTimeParseError, ValueError):
        return None


def extract_date(event_date: str) -> JDate | None:
    """DateUtils.extractDate"""
    result = None
    if event_date is not None and jmatches(r"^[0-9]{1,4}$", event_date):
        result = try_iso(("ISO_LOCAL_DATE",), event_date + "-01-01")
    elif event_date is not None and jmatches(r"^[0-9]{4}-[0-9]{2}$", event_date):
        result = try_iso(("ISO_LOCAL_DATE",), event_date + "-01")
    elif event_date is not None and jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$", event_date):
        result = try_iso(("ISO_LOCAL_DATE",), jsplit(event_date, "/")[0])
    else:
        # Java keeps the last formatter that succeeds; any success gives the same date.
        result = try_iso(("ISO_LOCAL_DATE", "ISO_ORDINAL_DATE", "ISO_DATE_TIME", "ISO_DATE"), event_date or "")
    for bad in (r"^[0-9]{4}-[0-9]{1}$", r"^[0-9]{4}-[0-9]{1}-.+", r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1}$",
                r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1}/.+"):
        if result is not None and jmatches(bad, event_date):
            result = None
    return result


def event_date_valid(event_date) -> bool:
    """DateUtils.eventDateValid"""
    if is_empty(event_date):
        return False
    if "/" not in event_date and extract_date(event_date) is not None:
        return True
    interval = extract_interval(event_date)
    return interval is not None and interval.start <= interval.end


# --- cleanMonth --------------------------------------------------------------------------

def clean_month(text: str) -> str:
    """Port of DateUtils.cleanMonth: normalise month spellings and roman numerals to English.

    The steps are applied in the Java order (order matters); see _CLEAN_MONTH_OPS.
    """
    if is_empty(text):
        return text
    c = text
    for op in _CLEAN_MONTH_OPS:
        kind = op[0]
        if kind == "replace":
            c = c.replace(op[1], op[2])
        elif kind == "replace_trim":
            c = jtrim(c.replace(op[1], op[2]))
        elif kind == "replaceall_if_matches":
            if jmatches(op[1], c):
                c = re.sub(op[2], op[3], c)
        elif kind == "replace_unless_matches":
            if not jmatches(op[1], c):
                c = c.replace(op[2], op[3])
        elif kind == "replace_unless_contains":
            if op[1] not in c:
                c = c.replace(op[2], op[3])
    return c


# --- extractDateFromVerbatimER ------------------------------------------------------------

NOT_RUN, AMBIGUOUS, SUSPECT, DATE, RANGE, DISJUNCT_RANGE = (
    "NOT_RUN", "AMBIGUOUS", "SUSPECT", "DATE", "RANGE", "DISJUNCT_RANGE")
YEAR_BEFORE_SUSPECT = 1000

_ORDINALS = ("st", "nd", "rd", "th")
_DAY_MONTH_YEAR = ("dd'-'LLL'-'uuuu", "dd'-'LLL'/'uuuu", "dd'-'LLL' 'uuuu",
                   "dd'-'LLLL'-'uuuu", "dd'-'LLLL'/'uuuu", "dd'-'LLLL' 'uuuu")
_TEXT_MONTH_DMY = ("dd'-'LLLL'-'uuuu", "LLLL'-'dd'-'uuuu", "dd'-'LLL'-'uuuu", "LLL'-'dd'-'uuuu",
                   "d'-'LLLL'-'uuuu", "LLLL'-'d'-'uuuu", "d'-'LLL'-'uuuu", "LLL'-'d'-'uuuu")
_WEEKDAY_DATE = tuple(
    f"{e}' 'd{suffix}{m}' 'uuuu"
    for e in ("EEE", "EEEE")
    for suffix, m in [("'st '", "LLLL"), ("'nd '", "LLLL"), ("'rd '", "LLLL"), ("'th '", "LLLL"), ("' '", "LLL"),
                      ("'st '", "LLL"), ("'nd '", "LLL"), ("'rd '", "LLL"), ("'th '", "LLL"), ("' '", "LLL")]
)


def _multi_locale_patterns(years_before_suspect: int) -> list[str]:
    """The formatter list of the multi-locale block (the same patterns for every locale)."""
    pats = ["uuuu'-'LLL'-'d", "uuuu'-'LLLL'-'d"]
    for o in _ORDINALS:
        pats += [f"uuuu'-'LLL'-'d'{o}'", f"uuuu'-'LLLL'-'d'{o}'"]
    if years_before_suspect < 1000:
        pats += ["LLLL'-'d'-'uuu", "LLL'-'d'-'uuu", "LLL'.-'d'-'uuu"]
    pats += ["LLL'-'d'-'uuuu", "LLL,'-'d'-'uuuu", "LLLL'-'d'-'uuuu"]
    for o in ("st", "nd", "rd", "d", "th"):
        pats += [f"LLL'-'d'{o}-'uuuu", f"LLLL'-'d'{o}-'uuuu"]
    pats += ["LLL'-'d'''-'uuuu", "LLLL'-'d'''-'uuuu", "LLL'-'d'''''-'uuuu", "LLLL'-'d'''''-'uuuu"]
    pats += ["d'-'LLL'-'uuuu", "d'-'LLLL'-'uuuu"]
    for o in _ORDINALS:
        pats += [f"d'{o}-'LLL'-'uuuu", f"d'{o}-'LLLL'-'uuuu"]
    pats += ["uuuu'-'LLLd", "LLLd'-'uuuu", "dLLL'-'uuuu", "uuuuLLLd", "LLLduuuu", "dLLLuuuu",
             "uuuu'-'LLLLd", "LLLLd'-'uuuu", "dLLLL'-'uuuu", "uuuuLLLLd", "dLLLLuuuu"]
    return pats


def _dashes(text: str) -> str:
    return text.replace("/", "-").replace(".", "-").replace(" ", "-").replace("--", "-")


def extract_date_from_verbatim(verbatim, years_before_suspect: int = YEAR_BEFORE_SUSPECT,
                               assume_mmddyyyy: bool | None = None) -> tuple[str, str | None]:
    """Port of DateUtils.extractDateFromVerbatimER: (state, ISO 8601 result) for a free-text date.

    state is DATE or RANGE for an unambiguous interpretation, AMBIGUOUS, SUSPECT,
    DISJUNCT_RANGE, or NOT_RUN when nothing could be read.
    """
    v = verbatim
    state, result = NOT_RUN, None

    # Remove some common no data comments, brackets and quotes
    if v is not None and "[no date]" in v:
        v = v.replace("[no date]", "")
    if v is not None and "[no year]" in v:
        v = v.replace("[no year]", "")
    if v is not None and v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    if v is not None and jmatches(r".*\[[0-9]+\].*", v):
        v = v.replace("[", "").replace("]", "")
    if v is not None and v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    if v is not None and (v.startswith(" ") or v.endswith(" ")):
        v = jtrim(v)
    if v is not None and v.endswith(".") and jmatches(r".*[0-9]\.$", v):
        v = v[:-1]
    if is_empty(v):
        return NOT_RUN, None

    # Ranges whose two halves are identical: use one.
    if jmatches(r"^[0-9]{4}[-][0-9]{2}[-][0-9]{2}/[0-9]{4}[-][0-9]{2}[-][0-9]{2}$", v):
        bits = jsplit(v, "/")
        if len(bits) == 2 and bits[0] == bits[1]:
            v = bits[0]
    if jmatches(r"^[0-9]{4}[/][0-9]{2}[/][0-9]{2}-[0-9]{4}[/][0-9]{2}[/][0-9]{2}$", v):
        bits = jsplit(v, "-")
        if len(bits) == 2 and bits[0] == bits[1]:
            v = bits[0]
    if jmatches(r"^[0-9]{1,2}[-. ][0-9]{1,2}[-. ][0-9]{4}/[0-9]{1,2}[-. ][0-9]{1,2}[-. ][0-9]{4}$", v):
        bits = jsplit(v, "/")
        if len(bits) == 2 and bits[0] == bits[1]:
            v = bits[0]
    if jmatches(r"^[0-9]{1,2}[./ ][0-9]{1,2}[./ ][0-9]{4}[-][0-9]{1,2}[./ ][0-9]{1,2}[./ ][0-9]{4}$", v):
        bits = jsplit(v, "-")
        if len(bits) == 2 and bits[0] == bits[1]:
            v = bits[0]

    # Example 1982-02-05, 1982-Feb-05, 1982-02-05T05:03:06
    if jmatches(r"^[0-9]{4}[-]([0-9]{1,2}|[A-Za-z]+)[-][0-9]{1,2}.*", v):
        d = try_iso(("ISO_DATE", "ISO_DATE_TIME", "ISO_LOCAL_DATE_TIME", "ISO_OFFSET_DATE_TIME", "ISO_DATE"), v) \
            or try_patterns(("uuuu'-'M'-'d", "uuuu'-'LLL'-'dd", "uuuu'-'LLLL'-'dd"), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982/02/05, 1982/Feb/05
    if jmatches(r"^[0-9]{4}[/]([0-9]{1,2}|[A-Za-z]+)[/][0-9]{1,2}$", v):
        d = try_patterns(("uuuu'/'M'/'d", "uuu'/'LLL'/'d"), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982/02/05T05:03:06
    if jmatches(r"^[0-9]{4}[/]([0-9]{1,2}|[A-Za-z]+)[/][0-9]{1,2}T.*$", v):
        d = try_iso(("ISO_LOCAL_DATE_TIME", "ISO_OFFSET_DATE_TIME", "ISO_ZONED_DATE_TIME"), v.replace("/", "-"))
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982-22-05: yyyy-dd-mm
    if state == NOT_RUN and jmatches(r"^[0-9]{4}-([2-3][0-9]|[1][3-9])-([1-9]|[0][0-9]|[1][0-2])$", v):
        d = try_patterns(("uuuu'-'dd'-'MM",), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982.22.05, 1982,22,5: yyyy-dd-mm
    if state == NOT_RUN and jmatches(r"^[0-9]{4}[.,]([2-3][0-9]|[1][3-9])[.,]([0-9]|[0][0-9]|[1][0-2])$", v):
        d = try_patterns(("uuuu'.'dd'.'MM", "uuuu'.'dd'.'M"), v.replace(",", "."))
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982.02.05, 1982,02,05
    if state == NOT_RUN and jmatches(r"^[0-9]{4}[.,][0-9]{1,2}[.,][0-9]{1,2}$", v):
        commas = v.replace(",", ".")
        md = try_patterns(("uuuu'.'M'.'d",), commas)
        dm = try_patterns(("uuuu'.'d'.'M",), commas)
        if md and not dm:
            state, result = DATE, iso_local_date(md)
        elif dm and not md:
            state, result = DATE, iso_local_date(dm)
        elif md and dm:
            if md == dm:
                state, result = DATE, iso_local_date(dm)
            elif jmatches(r"^[0-9]{4}\.[0-9]{2}\.[0-9]{1,2}$", commas):
                state, result = DATE, iso_local_date(md)
            else:
                state = AMBIGUOUS
                first, second = (md, dm) if md < dm else (dm, md)
                result = iso_local_date(first) + "/" + iso_local_date(second)
    # Example 02/1982
    if jmatches(r"^[0-9]{1,2}[-/ ][0-9]{4}$", v):
        d = try_patterns(("dd'-'M'-'uuuu", "dd'-'M' 'uuuu"), "01-" + v.replace("/", "-"))
        if d:
            state, result = RANGE, fmt_yyyy_mm(d)
    # Example 1972年03月25日
    if jmatches(r"^[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}[日号]$", v):
        d = try_patterns(("uuuu'年'M'月'd'日'", "uuuu'年'M'月'd'号'"), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1982-145/1982-200
    if jmatches(r"^[0-9]{4}[-][0-9]{3}/[0-9]{4}[-][0-9]{3}$", v):
        try:
            bits = jsplit(v, "/")
            s, e = iso_parse("ISO_ORDINAL_DATE", bits[0]), iso_parse("ISO_ORDINAL_DATE", bits[1])
            if s == e:
                state, result = DATE, iso_local_date(s)
            else:
                state, result = RANGE, iso_local_date(s) + "/" + iso_local_date(e)
        except DateTimeParseError:
            pass
    # Example 19800000
    if state == NOT_RUN and jmatches(r"^[0-9]{4}0000$", v):
        v = v[0:4]
    # Example 1962
    if state == NOT_RUN and jmatches(r"^[0-9]{4}$", v):
        d = try_iso(("ISO_LOCAL_DATE",), v + "-01-01")
        if d:
            state, result = RANGE, fmt_yyyy(d)
    # (Negative years such as -2000 are only read when years_before_suspect < 0; not supported here.)
    # Example 1900s, 1900's
    if state == NOT_RUN and jmatches(r"^[12][0-9]{1}00[']{0,1}s$", v):
        d = try_iso(("ISO_LOCAL_DATE",), v.replace("'", "").replace("s", "-01-01"))
        if d:
            end = JDate(d.year + 100, d.month, d.day).plus_days(-1)
            state, result = RANGE, fmt_yyyy(d) + "-01-01/" + fmt_yyyy(end) + "-12-31"
    # Example 1970s
    if state == NOT_RUN and jmatches(r"^[12][0-9]{2}0[']{0,1}s$", v):
        d = try_iso(("ISO_LOCAL_DATE",), v.replace("'", "").replace("s", "-01-01"))
        if d:
            end = JDate(d.year + 10, d.month, d.day).plus_days(-1)
            state, result = RANGE, fmt_yyyy(d) + "-01-01/" + fmt_yyyy(end) + "-12-31"
    # Example Jan-1980, Jan./1980, January 1980
    if state == NOT_RUN and jmatches(r"^[A-Za-z]{3,9}[.]{0,1}[ ]{0,1}[-/ ][0-9]{4}$", v):
        d = try_patterns(_DAY_MONTH_YEAR, "01-" + v.replace(".", ""))
        if d:
            state, result = RANGE, fmt_yyyy_mm(d)
    # Example June, 1980; Jan. 1980
    if state == NOT_RUN and jmatches(r"^[A-Za-z]{3,9}[.]{0,1}[,]{0,1}[ ]{0,1}[0-9]{4}$", v):
        cleaned = clean_month(v).replace(".", "-").replace(",", "-").replace(" ", "-")
        cleaned = cleaned.replace("--", "-").replace("--", "-")
        d = try_patterns(_DAY_MONTH_YEAR, "01-" + cleaned)
        if d:
            state, result = RANGE, fmt_yyyy_mm(d)
    # Example 04/03/1994 (ambiguous), 04/20/1994, 20/04/1994
    if state == NOT_RUN:
        cleaned = v.replace("/", "-").replace(" ", "-").replace(".", "-").replace("--", "-")
        md = dm = None
        if assume_mmddyyyy is None or assume_mmddyyyy:
            md = try_patterns(("MM'-'dd'-'uuuu", "MM'-'dd',-'uuuu", "MM'-'dd','uuuu",
                               "M'-'d'-'uuuu", "M'-'d',-'uuuu", "M'-'d','uuuu"), cleaned)
        if assume_mmddyyyy is None or not assume_mmddyyyy:
            dm = try_patterns(("dd'-'MM'-'uuuu", "dd'-'MM',-'uuuu", "dd'-'MM','uuuu",
                               "d'-'M'-'uuuu", "d'-'M',-'uuuu", "d'-'M','uuuu"), cleaned)
        if md and not dm:
            state, result = DATE, iso_local_date(md)
        elif dm and not md:
            state, result = DATE, iso_local_date(dm)
        elif md and dm:
            if md == dm:
                state, result = DATE, iso_local_date(dm)
            else:
                state = AMBIGUOUS
                first, second = (md, dm) if md < dm else (dm, md)
                result = iso_local_date(first) + "/" + iso_local_date(second)
    # Example Friday 29th Oct. 2021
    if state == NOT_RUN and jmatches(r"^[A-Z][A-Za-z]+ [0-9]{1,2}[stndrh]{2} [A-Za-z]+.{0,1},{0,1} [0-9]{4}$", v):
        cleaned = clean_month(v.replace(",", "")).replace(".", "")
        d = try_patterns(_WEEKDAY_DATE, cleaned)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 03/Jan/1982, Jan-03-1982
    if state == NOT_RUN and jmatches(r"^([0-9]{1,2}|[A-Za-z]+)[-/. ]([0-9]{1,2}|[A-Za-z]+),{0,1}[-/. ][0-9]{4}$", v):
        d = try_patterns(_TEXT_MONTH_DMY, _dashes(v).replace(",", ""))
        if d:
            state, result = DATE, iso_local_date(d)
    # Example Jan 03, 1982; 3 Jan, 1982
    if state == NOT_RUN and jmatches(
            r"^([0-9]{1,2}|[A-Za-z]+)[-/. ]{1,2}([0-9]{1,2}|[A-Za-z]+),{0,1}[-/., ]{1,2}[0-9]{4}$", v):
        cleaned = _dashes(clean_month(v)).replace(",", "-")
        cleaned = cleaned.replace("--", "-").replace("--", "-")
        d = try_patterns(_TEXT_MONTH_DMY, cleaned)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example XX-04-1982, XX-Jan-1995 (unknown day)
    if state == NOT_RUN and jmatches(r"^[X*]{2}[-/. ]([0-9]{1,2}|[A-Za-z]+)[-/. ][0-9]{4}$", v):
        cleaned = _dashes(v[0:3] + clean_month(v[3:])).replace(",", "")
        cleaned = re.sub("XX-", "01-", cleaned, count=1).replace("**-", "01-")
        d = None
        for pattern, strict in (("dd'-'LLL'-'uuuu", False), ("dd'-'LLLL'-'uuuu", True),
                                ("dd'-'MM'-'uuuu", True), ("dd'-'M'-'uuuu", True)):
            try:
                d = jparse(pattern, cleaned, "en", strict)
                break
            except DateTimeParseError:
                continue
        if d:
            state, result = RANGE, fmt_yyyy_mm(d)
    # Example XX-XXX-1995, **-**-1995 (unknown day and month)
    if state == NOT_RUN and jmatches(r"^[X*]{2,3}[-/. ][X*]{2,3}[-/. ][0-9]{4}$", v):
        cleaned = _dashes(v).replace(",", "")
        for old in ("XX-XX-", "XX-XXX-", "XXX-XX-", "**-**-", "**-***-", "***-**-"):
            cleaned = cleaned.replace(old, "01-01-")
        d = try_patterns(("dd'-'MM'-'uuuu",), cleaned)
        if d:
            state, result = RANGE, fmt_yyyy(d)
    # Example 1994-128 (day of year)
    if jmatches(r"^[0-9]{4}[-][0-9]{3}$", v) and state == NOT_RUN:
        d = try_iso(("ISO_ORDINAL_DATE",), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Block order from here on matters.
    # Example 1983-15, 1933-Mar
    if state == NOT_RUN:
        cleaned = _dashes(clean_month(v)).replace(",", "")
        cleaned = re.sub("-$", "", cleaned, count=1) + "-01"
        d = try_patterns(("uuuu'-'MM'-'dd", "uuuu'-'M'-'dd", "uuuu'-'LLL'-'dd", "uuuu'-'LLLL'-'dd"), cleaned)
        if d:
            month_text = fmt_yyyy_mm(d)
            if jmatches(r"^[0-9]{4}[-][0-9]{2}$", v):
                century, start_bit, end_bit = v[0:2], v[0:4], v[5:7]
                # 1805-06 could be a month or an abbreviated year: suspect.
                state = RANGE if int(start_bit) >= int(century + end_bit) else SUSPECT
            else:
                state = RANGE
            result = month_text
    # Example 1884-85
    if state == NOT_RUN and jmatches(r"^[0-9]{4}[-][0-9]{2}$", v):
        interval = extract_interval(v[0:4] + "/" + v[0:2] + v[5:7])
        if interval:
            state, result = RANGE, fmt_yyyy(interval.start) + "/" + fmt_yyyy(interval.end)
    # Example 19950315
    if state == NOT_RUN and jmatches(r"^[0-9]{4}[0-9]{2}[0-9]{2}$", v) and not v.endswith("0000"):
        d = try_iso(("BASIC_ISO_DATE",), v)
        if d:
            state, result = DATE, iso_local_date(d)
    # Multiple yyyy-mmm-ddd, mmm-dd-yyyy, dd-mmm-yyyy patterns in 17 languages.
    if state == NOT_RUN:
        cleaned = clean_month(v).replace("/", "-").replace(".", "-").replace(" ", "-").replace(":", "-") \
            .replace(";", "-").replace(",", "-")
        cleaned = re.sub("-+", "-", cleaned).replace(",", "")
        cleaned = re.sub("-$", "", cleaned, count=1)
        if years_before_suspect < 100 and jmatches(r"^[A-Za-z]+-[0-9]{1,2}-[0-9]{1,2}$", cleaned):
            cleaned = cleaned[:-2] + "0" + cleaned[-2:]
        # The Java builds these formatters once per locale (en, fr, de, ...), but wraps each in a
        # DateTimeFormatterBuilder whose toFormatter() drops the locale, so all of them parse English.
        d = try_patterns(_multi_locale_patterns(years_before_suspect), cleaned, "en")
        if d:
            state, result = DATE, fmt_yyyy(d) + f"-{d.month:02d}-{d.day:02d}"
    # Example jan.-1992, January 1992
    if state == NOT_RUN and jmatches(r".*[A-Za-z].+[0-9]{4}$", v):
        cleaned = clean_month(v).replace("/", "-").replace(".", "-").replace(",", "-").replace(" ", "-")
        cleaned = "01-" + re.sub("-(-)+", "-", cleaned)
        d = try_patterns(("dd'-'LLL'-'uuuu", "dd'-'LLLL'-'uuuu"), cleaned)
        if d:
            state, result = RANGE, fmt_yyyy_mm(d)
    # Example 1882-1995, 1882 to 1885, 1882/1885
    if state == NOT_RUN and jmatches(r"^[0-9]{4}([- ]+| to |[/ ]+)[0-9]{4}$", v):
        interval = extract_interval(v.replace(" to ", "/").replace("-", "/").replace(" ", ""))
        if interval:
            state, result = RANGE, fmt_yyyy(interval.start) + "/" + fmt_yyyy(interval.end)
    # Example Jan to Feb 1882, Jan-Feb/1882
    if state == NOT_RUN and jmatches(
            r"^[A-Za-z]+[.]{0,1}( et | & | and | to |[-][ ]{0,1}| [-] )[A-Za-z]+[.]{0,1}(, |[/ .])[0-9]{4}$", v):
        v = v.replace(", ", " ")
        if jmatches(r"^[A-Za-z]+[.]{0,1}[-][A-Za-z]+[.]{0,1}[.][0-9]{4}$", v):
            v = v[:-5] + "/" + v[-4:]
        if jmatches(r"^[A-Za-z]+[.]{0,1}[ ][-][ ]{1}[A-Za-z]+[.]{0,1}[/ .][0-9]{4}$", v):
            v = v.replace(" - ", "-")
        if jmatches(r"^[A-Za-z]+[.]{0,1}[-][ ]{1}[A-Za-z]+[.]{0,1}[/ .][0-9]{4}$", v):
            v = v.replace("- ", "-")
        disjunct = False
        if jmatches(r"^[A-Za-z]+[.]{0,1}( and | et | & ){1}[A-Za-z]+[.]{0,1}[/ .][0-9]{4}$", v):
            v = v.replace(" and ", "-").replace(" et ", "-").replace(" & ", "-")
            disjunct = True
        if jmatches(r"^[A-Za-z]+[.]{0,1} to {1}[A-Za-z]+[.]{0,1}[/ .][0-9]{4}$", v):
            v = v.replace(" to ", "-")
        patterns = ("dd'-'LLL'/'uuuu", "dd'-'LLLL'/'uuuu", "dd'-'LLL'./'uuuu", "dd'-'LLLL'./'uuuu")
        bits = jsplit(v.replace(" ", "/"), "-")
        if len(bits) == 2:
            year = v[-4:]
            start_bit = "01-" + clean_month(bits[0] + "/" + year).replace("./", "/")
            end_bit = "01-" + clean_month(bits[1]).replace("./", "/")
            start = try_patterns(patterns, start_bit)
            if start:
                for pattern in patterns:      # every matching end pattern is tried, as in Java
                    try:
                        end = jparse(pattern, end_bit)
                    except DateTimeParseError:
                        continue
                    state, result = RANGE, fmt_yyyy_mm(start) + "/" + fmt_yyyy_mm(end)
                    if disjunct:
                        interval = extract_interval(result)
                        if interval:
                            adjacent = (interval.start.month + 1 == interval.end.month
                                        and interval.start.year == interval.end.year)
                            state = RANGE if adjacent else DISJUNCT_RANGE
    # Example 05/Jan/1882-03/Feb/1885
    if state == NOT_RUN and jmatches(
            r"^[0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}( - |[-])[0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[/ -.][0-9]{4}$", v):
        if jmatches(r"^[0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[-][0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[-][0-9]{4}$", v):
            v = v[:-5] + "/" + v[-4:]
        if jmatches(r"^[0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[-][0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[.][0-9]{4}$", v):
            v = v[:-5] + "/" + v[-4:]
        bits = jsplit(v.replace(" - ", "-").replace(" ", "/"), "-")
        if len(bits) == 2:
            year = v[-4:]
            start_bit = clean_month(bits[0] + "-" + year).replace("/", "-").replace(" ", "-").replace(".", "-") \
                .replace("--", "-").replace("--", "-")
            end_bit = clean_month(bits[1]).replace("/", "-").replace(" ", "-").replace(".", "-") \
                .replace("--", "-").replace("--", "-")
            patterns = ("dd'-'LLL'-'uuuu", "dd'-'LLLL'-'uuuu", "ddLLL'-'uuuu", "ddLLLL'-'uuuu")
            start = try_patterns(patterns, start_bit)
            if start:
                end = try_patterns(patterns, end_bit)
                if end:
                    state, result = RANGE, fmt_yyyy(start) + start_end_mmdd(start) + "/" + fmt_yyyy(end) + start_end_mmdd(end)
    # Example Aug. 5 - Sept. 8, 1943
    if state == NOT_RUN and jmatches(
            r"^[A-Za-z]+[.]{0,1}[ ]{0,1}[0-9]{1,2}( - |[-]| to | and | et | & )[A-Za-z]+[.]{0,1}[ ]{0,1}[0-9]{1,2}"
            r"[/ .,][ ]{0,1}[0-9]{4}$", v):
        state, result = _two_part_range(v, state, result, (
            "LLL'/'d'/'uuuu", "LLL'./'d'/'uuuu", "LLL'./'d'-'uuuu", "LLL'.'d'/'uuuu",
            "LLLL'/'d'/'uuuu", "LLLL'./'d'/'uuuu", "LLLL'.'d'/'uuuu"))
    # Example 5 Aug. - 8 Sept,, 1943; 9.I-10.XII.2000
    if state == NOT_RUN and jmatches(
            r"^[0-9]{1,2}(.){0,1}[ ]*[A-Za-z]+[.]{0,1}( - |[.]|[-]| to | and | et | & ){0,1}[0-9]{1,2}[.]{0,1}[ ]*"
            r"[A-Za-z]+[.]{0,1}[/ .,][ ]*[0-9]{4}$", v):
        state, result = _two_part_range(v, state, result, (
            "d'/'LLL'/'uuuu", "d'/'LLL'./-'uuuu", "d'/'LLL'./'uuuu", "d'/'LLLL'/'uuuu", "d'/'LLLL'./'uuuu",
            "d'/'LLLL'./-'uuuu", "d'.'LLL'/'uuuu", "d'.'LLL'./-'uuuu", "d'.'LLL'./'uuuu", "d'.'LLLL'/'uuuu",
            "d'.'LLLL'./'uuuu", "d'.'LLLL'./-'uuuu"))
    # Example 11 et 14 VII 1910; 05-02 Jan./1992
    if state == NOT_RUN and jmatches(
            r"^[0-9]{1,2}([ ]{0,1}[-][ ]{0,1}| and | et | to | & )[0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[/ -.][0-9]{4}$", v):
        to_check = v.replace(" - ", "-").replace(" et ", "-").replace(" and ", "-").replace(" to ", "-")
        if jmatches(r"^[0-9]{1,2}[-][0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[-][0-9]{4}$", to_check):
            to_check = to_check[:-5] + "/" + to_check[-4:]
        if jmatches(r"^[0-9]{1,2}[-][0-9]{1,2}[ /.]{0,1}[A-Za-z]+[.]{0,1}[.][0-9]{4}$", to_check):
            to_check = to_check[:-5] + "/" + to_check[-4:]
        datebits = jsplit(to_check.replace(" ", "/"), "-")
        if len(datebits) == 2:
            patterns = []
            for m in ("LLL", "LLLL"):
                for suffix in ("'/'", "'./'"):
                    patterns += [f"d' '{m}{suffix}uuuu", f"d'.'{m}{suffix}uuuu", f"d'/'{m}{suffix}uuuu",
                                 f"d{m}{suffix}uuuu"]
            for pattern in patterns:          # Java never stops early here: the last match wins
                try:
                    end = jparse(pattern, clean_month(datebits[1]))
                except DateTimeParseError:
                    continue
                start_bit = datebits[0] + "/" + MONTH_NAMES["en"][1][end.month - 1] + "/" + fmt_yyyy(end)
                start = try_patterns(patterns, start_bit)
                if start is None:
                    continue
                state, result = RANGE, _ymd(start) + "/" + _ymd(end)
                if jmatches(r".*( and | et | & ).*", v):
                    interval = extract_interval(result)
                    if interval:
                        adjacent = (interval.end.day_of_year() - 1 == interval.start.day_of_year()
                                    and interval.start.month == interval.end.month
                                    and interval.start.year == interval.end.year)
                        state = RANGE if adjacent else DISJUNCT_RANGE
    # Example 05-XXX-1884
    if state == NOT_RUN and jmatches(r"^[0-9]{2}[-. ]XXX[-. ][0-9]{4}$", v):
        start = v[-4:] + "-01-" + v[0:2]
        end = v[-4:] + "-12-" + v[0:2]
        composite = extract_date_from_verbatim(start + "/" + end, years_before_suspect, assume_mmddyyyy)
        if composite[0] == RANGE:
            state, result = composite
    # Example 19 3/viii 25 (century, day/month, year)
    if state == NOT_RUN and jmatches(r"^[1-2][07-9] [0-9]{1,2}/([0-9]{1,2}|[IVXivx]{1,4}) [0-9]{2}$", v):
        cleaned = clean_month(v[3:-3] + "/" + v[0:2] + v[-2:])
        d = try_patterns(("d'/'M'/'uuuu", "d'/'LLLL'/'uuuu"), cleaned)
        if d:
            state, result = DATE, iso_local_date(d)
    # Example 1885-03/1886-04
    if state == NOT_RUN and jmatches(r"^[0-9]{4}-[0-9]{2}/[0-9]{4}-[0-9]{2}$", v):
        interval = extract_interval(v)
        if interval:
            state, result = RANGE, fmt_yyyy_mm(interval.start) + "/" + fmt_yyyy_mm(interval.end)
    # Example 1995-03-05/1996-05-08: a plain ISO date or interval
    if state == NOT_RUN and jmatches(r".*[0-9]{4}.*", v) and not jmatches(r"^[0-9]{1,2}/[0-9]{4}$", v):
        interval = extract_interval(v)
        if interval:
            if interval.is_single_day():
                state, result = DATE, iso_local_date(interval.start)
            else:
                state, result = RANGE, str(interval)
            if interval.start.year < years_before_suspect:
                state = SUSPECT
    # Example Jan,15-18 1882; Jan. 17 and 18 1882
    if state == NOT_RUN:
        cleaned = jtrim(v)
        if jmatches(r"^[A-Za-z.]+[ ,]+[0-9]{1,2}( and | et | & )[0-9]{0,2}[ ,]+[0-9]{4}$", v):
            cleaned = cleaned.replace(" and ", " to ").replace(" et ", " to ").replace(" & ", " to ")
        if jmatches(r"^[A-Za-z.]+[ ,]+[0-9]{1,2}-[0-9]{0,2}[ ,]+[0-9]{4}$", v):
            cleaned = cleaned.replace("-", " to ")
        if " to " in cleaned:
            bits = jsplit(cleaned, " to ")
            year_match = re.fullmatch(r".*([0-9]{4}).*", cleaned)
            month_match = re.fullmatch(r"([A-Za-z.]+).*", cleaned)
            if year_match and month_match and len(bits) == 2:
                year, month = year_match.group(1), month_match.group(1)
                if year not in bits[0]:
                    bits[0] = bits[0] + " " + year
                if year not in bits[1]:
                    bits[1] = bits[1] + " " + year
                if month not in bits[1]:
                    bits[1] = month + " " + bits[1]
                first = extract_date_from_verbatim(bits[0])
                if first[0] == DATE:
                    second = extract_date_from_verbatim(bits[1])
                    if second[0] == DATE:
                        state, result = RANGE, first[1] + "/" + second[1]
                        if jmatches(r".*( and | et | & ).*", v):
                            interval = extract_interval(result)
                            if interval:
                                adjacent = (interval.end.day_of_year() - 1 == interval.start.day_of_year()
                                            and interval.start.month == interval.end.month
                                            and interval.start.year == interval.end.year)
                                state = RANGE if adjacent else DISJUNCT_RANGE

    # Now test to see if the result is sane.
    if state != NOT_RUN:
        test = extract_interval(result)
        if test is None or test.start.year < years_before_suspect:
            state = SUSPECT
        # A four digit year must be present (except in the yy dd/mm yy form).
        if (not jmatches(r".*[0-9]{4}.*", v)
                and not jmatches(r"^[1-2][07-9] [0-9]{1,2}/([0-9]{1,2}|[IVXivx]{1,4}) [0-9]{2}$", v)
                and years_before_suspect > 999):
            state, result = NOT_RUN, None
    return state, result


def _ymd(d: JDate) -> str:
    """Pattern "yyyy-MM-dd"."""
    return f"{fmt_yyyy(d)}-{d.month:02d}-{d.day:02d}"


def start_end_mmdd(d: JDate) -> str:
    return f"-{d.month:02d}-{d.day:02d}"


def _two_part_range(v: str, state: str, result, patterns) -> tuple[str, str | None]:
    """The 'Aug. 5 - Sept. 8, 1943' and '5 Aug. - 8 Sept, 1943' blocks (same logic, different patterns)."""
    bits = jsplit(v.replace(" to ", "-").replace(" - ", "-").replace(" and ", "-").replace(" et ", "-")
                  .replace(", ", " ").replace(" ", "/"), "-")
    if len(bits) == 2:
        start_bit = bits[0] + "/" + v[-4:]
        for pattern in patterns:              # Java never stops early here: the last match wins
            try:
                start = jparse(pattern, clean_month(start_bit))
                end = jparse(pattern, clean_month(bits[1]))
            except DateTimeParseError:
                continue
            result = _ymd(start) + "/" + _ymd(end)
            state = DISJUNCT_RANGE if jmatches(r".*( and | et | & ).*", v) else RANGE
    return state, result


# --- LocalDateTimeInterval, used to measure the duration of a date-time ------------------

_NANOS_PER_DAY = 86_400 * 10**9
_ISO_DATE_TIME = re.compile(
    rf"{_DATE}T([01][0-9]|2[0-3]):([0-5][0-9])(:([0-5][0-9])(\.([0-9]{{0,9}}))?)?"
    rf"(({_OFFSET}|{_OFFSET_REQUIRED})({_ZONE})?)?")
# The uuuu-MM-dd'T'kk... formatters: clock hour 01-24, then optional zone/offset sections.
_KK_DATE_TIME = re.compile(rf"{_DATE}T([0-9]{{2}})(:([0-9]{{2}})(:([0-9]{{2}}))?)?(.*)")


def _local_date_time(bit: str) -> int:
    """LocalDateTime.parse(bit) with the formatters of LocalDateTimeInterval.parseDateBit,
    as nanoseconds since 1970-01-01T00:00 (an offset or zone is checked, then ignored)."""
    if jmatches(r"^.*T[0-9]{2}[.,][0-9]+$", bit) or jmatches(r"^.*T[0-9]{2}:[0-9]{2}[.,][0-9]+$", bit):
        bit = re.sub(r"[.,][0-9]+$", "", bit, count=1)
    m = _ISO_DATE_TIME.fullmatch(bit)
    if m:
        day = _checked_date(m.group(1), m.group(2), m.group(3), bit)
        fraction = (m.group(9) or "").ljust(9, "0")
        return (day.epoch_day() * _NANOS_PER_DAY
                + (int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(7) or 0)) * 10**9
                + int(fraction))
    m = _KK_DATE_TIME.fullmatch(bit)
    if not m:
        raise DateTimeParseError(bit)
    day = _checked_date(m.group(1), m.group(2), m.group(3), bit)
    hour, minute, second, rest = int(m.group(4)), m.group(6), m.group(8), m.group(9)
    if not 1 <= hour <= 24 or (minute is not None and int(minute) > 59) \
            or (second is not None and int(second) > 59):
        raise DateTimeParseError(bit)
    suffix = _ZONE_SUFFIX_NO_OOOO if second is not None else _ZONE_SUFFIX
    if re.fullmatch(suffix, rest) is None:
        raise DateTimeParseError(bit)
    hour %= 24                                  # clock hour 24 is midnight starting the same day
    return (day.epoch_day() * _NANOS_PER_DAY
            + (hour * 3600 + int(minute or 0) * 60 + int(second or 0)) * 10**9)


def _checked_date(year: str, month: str, day: str, text: str) -> JDate:
    if re.fullmatch(r"-0+", year):
        raise DateTimeParseError(text)
    try:
        return JDate(int(year), int(month), int(day))
    except ValueError:
        raise DateTimeParseError(text)


def _date_time_interval_seconds(text: str) -> int:
    """LocalDateTimeInterval(text).toDuration().getSeconds(). Every part must carry a time:
    the Java parses each part as a LocalDateTime, so a date-only part fails."""
    if is_empty(text):
        raise DateTimeParseError("empty")
    if "/" in text:
        bits = jsplit(text, "/")
        if len(bits) != 2:
            raise DateTimeParseError(text)
        start_bit, end_bit = bits
        if jmatches(r"^-[0-9]{3,4}", start_bit):
            start_bit = start_bit.replace("-", "") + " BCE"
        if jmatches(r"^-[0-9]{3,4}", end_bit):
            end_bit = end_bit.replace("-", "") + " BCE"
        if jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+$", start_bit) and jmatches(r"^T[0-9:.]+$", end_bit):
            end_bit = start_bit[0:10] + end_bit
        start, end = _date_time_bit(start_bit), _date_time_bit(end_bit)
        if end < start:
            raise DateTimeParseError(text)
        return (end - start) // 10**9
    if jmatches(r"^-[0-9]{3,4}", text):
        text = text.replace("-", "") + " BCE"
    _date_time_bit(text)
    return 0                                    # a single date-time: start and end are the same instant


def _date_time_bit(bit: str) -> int:
    if is_empty(bit) or jmatches(r"^[0-9]{1,3}$", bit) or "T" not in bit:
        raise DateTimeParseError(bit)
    return _local_date_time(bit)


def measure_duration_seconds(event_date) -> int:
    """Port of DateUtils.measureDurationSeconds; raises DateTimeParseError if it can't be read."""
    if is_empty(event_date):
        return 0
    if jmatches(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", event_date):
        iso_parse("ISO_LOCAL_DATE", event_date)
        return 86_400
    if jmatches(r"^[0-9]{4}-[0-9]{3}$", event_date):
        iso_parse("ISO_ORDINAL_DATE", event_date)
        return 86_400
    if "T" in event_date:
        return _date_time_interval_seconds(event_date)
    interval = LocalDateInterval(event_date)
    return interval.duration_seconds()

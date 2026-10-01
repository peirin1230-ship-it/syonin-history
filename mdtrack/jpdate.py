"""和暦（令和・平成）の日付文字列を datetime.date に変換する小さなユーティリティ。"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925}

_DATE_RE = re.compile(r"(令和|平成|昭和)\s*(元|\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")
_YM_RE = re.compile(r"(令和|平成|昭和)\s*(元|\d+)\s*年\s*(\d+)\s*月")


def normalize(s: str) -> str:
    """全角数字・記号を半角に（NFKC）。"""
    return unicodedata.normalize("NFKC", s or "")


def _year(era: str, y: str) -> int:
    return ERA_BASE[era] + (1 if y == "元" else int(y))


def parse_all(s: str) -> list[date]:
    s = normalize(s)
    out = []
    for era, y, m, d in _DATE_RE.findall(s):
        try:
            out.append(date(_year(era, y), int(m), int(d)))
        except ValueError:
            pass
    return out


def parse_first(s: str) -> date | None:
    r = parse_all(s)
    return r[0] if r else None


def parse_ym(s: str) -> tuple[int, int] | None:
    m = _YM_RE.search(normalize(s))
    if not m:
        return None
    era, y, mo = m.groups()
    return _year(era, y), int(mo)

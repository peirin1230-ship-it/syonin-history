"""通知の「決定機能区分」文字列を、特定器材マスターの特定器材コードに対応付ける。

通知: 「010 血管造影用ﾏｲｸﾛｶﾃｰﾃﾙ (1)ｵｰﾊﾞｰｻﾞﾜｲﾔｰ ①選択的ｱﾌﾟﾛｰﾁ型 ｱ ﾌﾞﾚｰﾄﾞあり」
マスター: 区分番号 010 / 基本漢字名称「血管造影用マイクロカテーテル・オーバーザワイヤー・選択的アプローチ型・ブレードあり」

区分番号（先頭3桁）で候補を絞り、列挙記号・空白・記号を除いた名称の類似度で選ぶ。
マスターは平成24年以降の全版の名称を候補に含めるので、改定で名称が変わっていても当時の名称で一致する。
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

SETTING_BEPPYO = {"医科": ("2", "1", "8", "9", "3"), "歯科": ("5", "6", "7", "4", "9"), "調剤": ("8",)}

_ENUM_PATTERNS = [
    r"[（(]\s*[0-9０-９]+\s*[）)]",          # (1)
    r"[①-⑳㉑-㉟]",                          # ①
    r"(?<![^\s　・･])[ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄアイウエオカキクケコサシスセソタチツテト](?=[\s　])",  # ア（列挙）
    r"[ⅰ-ⅿ]+",                              # ⅰ ⅱ（小文字ローマ数字の列挙）
    r"(?<![^\s　])(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)(?=[\s　])",
]
_ROMAN = {"Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5", "Ⅵ": "6", "Ⅶ": "7", "Ⅷ": "8", "Ⅸ": "9",
          "Ⅹ": "10", "Ⅺ": "11", "Ⅻ": "12"}


def _compact(t: str) -> str:
    for k, v in _ROMAN.items():
        t = t.replace(k, v)
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[\s・･,，、()（）\[\]「」]", "", t)
    return t.replace("―", "ー").replace("－", "ー").replace("-", "ー")


def norm_category(s: str) -> str:
    """通知の決定機能区分を比較用に正規化（区分番号・列挙記号を除き、重複する上位名を畳む）。"""
    if not s:
        return ""
    t = re.sub(r"^\s*[0-9０-９]{3}\s*", "", s)
    for p in _ENUM_PATTERNS:
        t = re.sub(p, "|", t)
    segs = [x for x in (_compact(seg) for seg in t.split("|")) if x]
    out = []
    for i, seg in enumerate(segs):
        if i + 1 < len(segs) and segs[i + 1].startswith(seg):
            continue  # 「植込型除細動器用カテーテル電極 (1)植込型除細動器用カテーテル電極(シングル)」
        out.append(seg)
    return "".join(out)


def norm_master(name: str) -> str:
    t = name or ""
    parts = [_compact(x) for x in re.split(r"[・･]", t)]
    parts = [x for x in parts if x]
    out = []
    for i, seg in enumerate(parts):
        if i + 1 < len(parts) and parts[i + 1].startswith(seg):
            continue
        out.append(seg)
    return "".join(out)


def category_no(s: str) -> str | None:
    m = re.match(r"^\s*(\d{3})", unicodedata.normalize("NFKC", s or ""))
    return m.group(1) if m else None


class CategoryMatcher:
    def __init__(self, master_rows):
        """master_rows: ssk.MasterRow のリスト（全版）。"""
        self.cands: dict[tuple[str, str], dict[str, set[str]]] = {}
        for r in master_rows:
            names = {norm_master(r.basic_name), norm_master(r.name)} - {""}
            d = self.cands.setdefault((r.beppyo, r.kubun_no), {})
            d.setdefault(r.code, set()).update(names)
        self.cache: dict[tuple, tuple] = {}

    def candidates(self, setting: str, category: str, top: int = 5) -> list[tuple[str, str, float]]:
        """(特定器材コード, 別表番号, スコア) の候補をスコア順に返す。"""
        key = (setting, category)
        if key in self.cache:
            return self.cache[key]
        no = category_no(category)
        target = norm_category(category)
        scored: dict[str, tuple[str, str, float]] = {}
        if no and target:
            bps = list(SETTING_BEPPYO.get(setting, ("2",)))
            bps += [b for b in ("1", "2", "3", "4", "5", "6", "7", "8", "9") if b not in bps]
            for bp in bps:
                penalty = 0.0 if bp in SETTING_BEPPYO.get(setting, ()) else 0.05
                for code, names in self.cands.get((bp, no), {}).items():
                    sc = max((1.0 if n == target else SequenceMatcher(None, target, n).ratio()) for n in names) - penalty
                    if code not in scored or sc > scored[code][2]:
                        scored[code] = (code, bp, sc)
        res = sorted(scored.values(), key=lambda x: -x[2])[:top]
        self.cache[key] = res
        return res

    def match(self, setting: str, category: str, price_ok=None) -> tuple[str | None, str | None, float]:
        """最良の候補。

        - 名称の類似度 0.75 以上 → 採用（僅差の候補があれば価格が一致する方）
        - 0.55 以上で、上位候補のうち掲載時の価格が一致するものが1つだけ → 採用
        """
        c = self.candidates(setting, category, top=8)
        if not c:
            return (None, None, 0.0)
        ok = [x for x in c if price_ok and x[2] >= 0.55 and price_ok(x[0])] if price_ok else []
        if c[0][2] >= 0.75:
            close = [x for x in ok if x[2] >= c[0][2] - 0.05]
            return close[0] if close else c[0]
        if len(ok) == 1:
            return ok[0]
        return (None, None, c[0][2])

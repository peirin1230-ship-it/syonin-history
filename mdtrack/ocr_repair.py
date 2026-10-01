"""OCR 結果の補正。

OCR の誤読（B↔8、Z↔2、桁の脱落・重複など）を、既知の承認番号・JAN と照合して直す。
辞書は (1) テキストPDF期の通知から得た承認番号・製品コード、(2) MEDIS の全登録（任意）、
(3) OCR 結果どうしの多数決。補正後の値で解析キャッシュを書き換え、元の読取値も残す。

補正結果（＝通知に印字されている承認番号・JAN）だけを保存し、MEDIS のデータそのものは保存しない。
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .parse_notice import gtin_ok


def levenshtein(a: str, b: str, limit: int = 3) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


# OCR で取り違えやすい文字（同じ字形群）。比較時の重みを下げる
_CONFUSE = [set("B83"), set("Z27"), set("O0DQ"), set("I1LT"), set("S5"), set("G6"), set("A4")]


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    for g in _CONFUSE:
        if a in g and b in g:
            return 0.4
    return 1.0


def weighted_distance(a: str, b: str, limit: float = 3.0) -> float:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = [float(i) for i in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)] + [0.0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + _sub_cost(ca, cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


class Dictionary:
    def __init__(self):
        self.approvals: set[str] = set()
        self.jan_to_appr: dict[str, set[str]] = defaultdict(set)
        self.appr_to_jans: dict[str, set[str]] = defaultdict(set)
        self.appr_name: dict[str, str] = {}
        self.appr_applicant: dict[str, str] = {}
        self.jan_name: dict[str, str] = {}
        self.jans_by_prefix: dict[str, set[str]] = defaultdict(set)
        self._block: dict[str, set[str]] = defaultdict(set)

    def add(self, appr: str, jan: str | None = None, name: str | None = None, applicant: str | None = None,
            product_name: str | None = None):
        if not appr:
            return
        if jan and product_name and jan not in self.jan_name:
            self.jan_name[jan] = product_name
        appr = appr.upper()
        if appr not in self.approvals:
            self.approvals.add(appr)
            for k in self._keys(appr):
                self._block[k].add(appr)
        if jan:
            self.jan_to_appr[jan].add(appr)
            self.appr_to_jans[appr].add(jan)
            self.jans_by_prefix[jan[:7]].add(jan)
        if name and appr not in self.appr_name:
            self.appr_name[appr] = name
        if applicant and appr not in self.appr_applicant:
            self.appr_applicant[appr] = applicant

    @staticmethod
    def _keys(s: str):
        # 前半・後半・中央の部分文字列でブロッキング（1〜2文字の誤りでもどれかは一致する）
        return {f"h{s[:6]}", f"t{s[-6:]}", f"m{s[5:11]}", f"a{s[2:8]}", f"b{s[8:14]}"}

    def candidates(self, s: str) -> set[str]:
        out = set()
        for k in self._keys(s):
            out |= self._block.get(k, set())
        return out


def load_dictionary(db_text: Path | None, medis_db: Path | None) -> Dictionary:
    d = Dictionary()
    if db_text and db_text.exists():
        con = sqlite3.connect(db_text)
        cols = [r[1] for r in con.execute("PRAGMA table_info(raw_rows)")]
        where = " WHERE ocr=0" if "ocr" in cols else ""
        for appr, code, name, applicant, pname in con.execute(
                "SELECT approval_no, product_code, sales_name, applicant, product_name FROM raw_rows" + where):
            d.add(appr, code, name, applicant, pname)
    if medis_db and medis_db.exists():
        con = sqlite3.connect(medis_db)
        snap = con.execute("SELECT MAX(snapshot) FROM medis_dict").fetchone()[0]
        cols = [r[1] for r in con.execute("PRAGMA table_info(medis_dict)")]
        pn = "product_name" if "product_name" in cols else "''"
        for appr, jan, name, maker, pname in con.execute(
                f"SELECT approval_no, jan, sales_name, maker, {pn} FROM medis_dict WHERE snapshot=?", (snap,)):
            if re.fullmatch(r"[0-9A-Z]{14,17}", appr or ""):
                d.add(appr, jan, name, None, pname)
    return d


def fix_approval(ocr: str, codes: list[str], d: Dictionary, context: set[str] | None = None) -> tuple[str, str]:
    """(補正後の承認番号, 補正方法)。方法: exact / jan / page / fuzzy / none

    context: 同じ頁で確実に読めた承認番号（同じ承認番号が続けて並ぶことが多い）
    """
    from .ocr_notice import fix_approval as _structural
    s = _structural(re.sub(r"[^0-9A-Z]", "", (ocr or "").upper()))
    if s in d.approvals:
        return s, "exact"
    if context:
        near = sorted(((weighted_distance(s, c, 4.5), c) for c in context), key=lambda x: x[0])
        if near and (near[0][0] <= 2.0 or (near[0][0] <= 4.5 and s[-8:] == near[0][1][-8:])) and \
                (len(near) == 1 or near[1][0] - near[0][0] >= 0.6):
            return near[0][1], "page"
    # 製品コードから承認番号が分かる場合（JAN → 承認番号）
    votes = Counter()
    for c in codes:
        for a in d.jan_to_appr.get(c, ()):
            votes[a] += 1
    if votes:
        best, n = votes.most_common(1)[0]
        if weighted_distance(s, best, 6) <= 6:
            return best, "jan"
    cands = d.candidates(s)
    scored = sorted(((weighted_distance(s, c, 2.5), c) for c in cands), key=lambda x: x[0])
    if scored and scored[0][0] <= 2.0 and (len(scored) == 1 or scored[1][0] - scored[0][0] >= 0.6):
        return scored[0][1], "fuzzy"
    return s, "none"


def fix_code(code: str, appr: str, d: Dictionary) -> tuple[str, str]:
    """(補正後の製品コード, 方法)。exact / pad / appr1 / prefix1 / none"""
    if code in d.jan_to_appr:
        return code, "exact"
    for alt in ("0" + code, code[1:] if code.startswith("0") else None):
        if alt and alt in d.jan_to_appr:
            return alt, "pad"
    pool = d.appr_to_jans.get(appr, set())

    def ham1(pool_):
        hits = [j for j in pool_ if len(j) == len(code) and sum(x != y for x, y in zip(j, code)) == 1]
        return hits[0] if len(hits) == 1 else None

    h = ham1(pool)
    if h:
        return h, "appr1"
    if not gtin_ok(code):
        h = ham1(d.jans_by_prefix.get(code[:7], set()))
        if h and gtin_ok(h):
            return h, "prefix1"
        # OCR で取り違えやすい数字の1文字置換で、チェックデジットが合う候補が1つだけなら採用
        if len(code) in (13, 14):
            cands = set()
            for i, ch in enumerate(code):
                for alt in _DIGIT_CONFUSE.get(ch, ""):
                    c2 = code[:i] + alt + code[i + 1:]
                    if gtin_ok(c2):
                        cands.add(c2)
            known = [c for c in cands if c in d.jan_to_appr or c[:7] in d.jans_by_prefix]
            pick = known if known else list(cands)
            if len(pick) == 1:
                return pick[0], "checkdigit"
        # 1桁脱落（12桁）: 1桁補って既知の事業者コードかつチェックデジットが合うもの
        if len(code) == 12:
            cands = {code[:i] + dg + code[i:] for i in range(13) for dg in "0123456789"}
            cands = [c for c in cands if gtin_ok(c) and (c in d.jan_to_appr or c[:7] in d.jans_by_prefix)]
            if len(cands) == 1:
                return cands[0], "insert"
    return code, "none"


_DIGIT_CONFUSE = {"1": "47", "4": "19", "7": "12", "5": "68", "6": "580", "8": "3560", "3": "8", "0": "86",
                  "2": "7", "9": "4"}


def repair_records(records: list[dict], d: Dictionary) -> dict:
    """OCR レコードを補正（その場で書き換え）。統計を返す。"""
    st = Counter()
    # 承認番号グループ（同じ OCR 値＋同じページ）ごとに、製品コードの情報も使って補正
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[(r.get("approval_ocr") or r["approval_no"], r.get("page"))].append(r)
    # 頁ごとの確実な承認番号（辞書と完全一致）
    page_ctx: dict = defaultdict(set)
    for (ocr, page), rs in groups.items():
        s0 = re.sub(r"[^0-9A-Z]", "", (ocr or "").upper())
        from .ocr_notice import fix_approval as _structural
        s0 = _structural(s0)
        if s0 in d.approvals:
            page_ctx[page].add(s0)
    for (ocr, page), rs in groups.items():
        codes = [r["product_code"] for r in rs if r.get("product_code")]
        fixed, how = fix_approval(ocr, codes, d, page_ctx.get(page))
        for r in rs:
            r.setdefault("approval_ocr", ocr)
            r["approval_no"] = fixed
            r["approval_fix"] = how
        st[f"appr_{how}"] += len(rs)
    # 同じ承認番号・頁の製品コードは事業者コード（先頭7桁）が揃うことが多い → 多数派で補正
    ctx: dict[tuple, Counter] = defaultdict(Counter)
    for r in records:
        c = r.get("code_ocr") or r.get("product_code")
        if c and gtin_ok(c):
            ctx[(r["approval_no"], r.get("page"))][c[:7]] += 1
    for r in records:
        c = r.get("code_ocr") or r.get("product_code")
        if not c:
            continue
        fixed, how = fix_code(c, r["approval_no"], d)
        if how == "none" and not gtin_ok(c) and len(c) == 13:
            pref = ctx.get((r["approval_no"], r.get("page")))
            if pref:
                p7 = pref.most_common(1)[0][0]
                c2 = p7 + c[7:]
                if c2 != c and sum(x != y for x, y in zip(p7, c[:7])) <= 2 and gtin_ok(c2):
                    fixed, how = c2, "prefix_ctx"
        r.setdefault("code_ocr", c)
        r["product_code"] = fixed
        r["code_fix"] = how
        st[f"code_{how}"] += 1
        # 名称は辞書（テキスト期の通知 → MEDIS）の表記を優先
    for r in records:
        pc = r.get("product_code")
        if pc and pc in d.jan_name and not (r.get("product_name") or "").strip():
            r["product_name"] = d.jan_name[pc]
        a = r["approval_no"]
        if a in d.appr_name:
            r.setdefault("sales_name_ocr", r.get("sales_name"))
            r["sales_name"] = d.appr_name[a]
        if a in d.appr_applicant:
            r.setdefault("applicant_ocr", r.get("applicant"))
            r["applicant"] = d.appr_applicant[a]
    return dict(st)

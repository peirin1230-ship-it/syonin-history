"""解析結果を SQLite にまとめ、訂正を反映し、承認番号ごとの変更イベントを作る。"""
from __future__ import annotations

import bisect
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import catmap, ssk
from .parse_notice import gtin_ok
from .fetch import NoticeLink

TARGET_KUBUN = re.compile(r"^(B|C|R)")  # 追跡対象（特定保険医療材料）

SCHEMA = """
DROP TABLE IF EXISTS notices; DROP TABLE IF EXISTS raw_rows; DROP TABLE IF EXISTS listing;
DROP TABLE IF EXISTS ssk_history; DROP TABLE IF EXISTS category_map; DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS approvals; DROP TABLE IF EXISTS corrections; DROP TABLE IF EXISTS meta;
DROP TABLE IF EXISTS notice_changes;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE notices(doc_id TEXT PRIMARY KEY, title TEXT, url TEXT, kind TEXT, notice_date TEXT,
  effective_date TEXT, fiscal_section TEXT, status TEXT, n_rows INTEGER, warnings TEXT);
CREATE TABLE raw_rows(doc_id TEXT, page INTEGER, setting TEXT, action TEXT, kubun TEXT, effective_date TEXT,
  approval_no TEXT, sales_name TEXT, product_name TEXT, product_code TEXT, annex TEXT, applicant TEXT,
  category TEXT, category_no TEXT, price_text TEXT, price REAL, price_unit TEXT, heading TEXT, side TEXT,
  ref_notice_date TEXT, ocr INTEGER DEFAULT 0);
CREATE TABLE listing(id INTEGER PRIMARY KEY, doc_id TEXT, effective_date TEXT, setting TEXT, action TEXT,
  kubun TEXT, approval_no TEXT, sales_name TEXT, product_name TEXT, product_code TEXT, applicant TEXT,
  category TEXT, category_norm TEXT, category_no TEXT, price REAL, price_unit TEXT, price_text TEXT,
  corrected_by TEXT, category_code TEXT, category_beppyo TEXT, match_score REAL, ocr INTEGER DEFAULT 0,
  category_ocr TEXT);
CREATE INDEX ix_listing_appr ON listing(approval_no);
CREATE INDEX ix_listing_code ON listing(product_code);
CREATE TABLE corrections(correction_doc TEXT, correction_date TEXT, target_doc TEXT, target_date TEXT,
  approval_no TEXT, side TEXT, product_code TEXT, category TEXT, price REAL, matched INTEGER);
CREATE TABLE ssk_history(code TEXT, valid_from TEXT, price REAL, unit TEXT, name TEXT, basic_name TEXT,
  beppyo TEXT, kubun_no TEXT, abolish_date TEXT);
CREATE INDEX ix_ssk_code ON ssk_history(code, valid_from);
CREATE TABLE category_map(setting TEXT, category TEXT, code TEXT, beppyo TEXT, score REAL,
  PRIMARY KEY(setting, category));
CREATE TABLE events(approval_no TEXT, date TEXT, type TEXT, title TEXT, detail TEXT, setting TEXT,
  kubun TEXT, category TEXT, category_code TEXT, price_before REAL, price_after REAL, doc_id TEXT,
  source TEXT);
CREATE INDEX ix_events_appr ON events(approval_no, date);
CREATE TABLE notice_changes(doc_id TEXT, effective_date TEXT, approval_no TEXT, sales_name TEXT, applicant TEXT,
  kubun TEXT, actions TEXT, change_types TEXT, summary TEXT, detail_json TEXT, ocr INTEGER);
CREATE INDEX ix_nc_doc ON notice_changes(doc_id);
CREATE TABLE approvals(approval_no TEXT PRIMARY KEY, sales_name TEXT, applicant TEXT, first_date TEXT,
  last_date TEXT, kubuns TEXT, n_products INTEGER, n_categories INTEGER, n_events INTEGER,
  current_json TEXT, flags TEXT);
"""


def cat_key(r: dict) -> str:
    """機能区分の同一性の判定キー。特定器材コードが分かればそれ、無ければ表記の正規化。"""
    return r.get("category_code") or norm_cat_text(r.get("category"))


def norm_cat_text(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


_APPR_OK = re.compile(r"^(?:\d{5}[A-Z]{3}\d{5}[0-9A-Z]{3}|\d{3}[A-Z]{5}\d{5}[0-9A-Z]\d{2}|\d{2}[0-9A-Z]{14})$")


def _usable_ocr_approval(r: dict) -> bool:
    """OCR の承認番号が使えるか（辞書で補正済み、または番号の形式として妥当）。"""
    if r.get("approval_fix") in ("exact", "jan", "fuzzy", "page"):
        return True
    return bool(_APPR_OK.match(r.get("approval_no") or ""))


def _sane_effective(eff: str | None, notice_date: str | None, default: str | None) -> str | None:
    """OCR で読んだ適用日が通知日から大きく外れていたら既定値（通知日の翌月1日）にする。"""
    if not eff or not notice_date:
        return default
    try:
        from datetime import date as _d
        a, b = _d.fromisoformat(eff), _d.fromisoformat(notice_date)
    except ValueError:
        return default
    return eff if -40 <= (a - b).days <= 70 else default


class OcrCategoryMatcher:
    """OCR 行の機能区分を、区分番号と価格から特定器材マスターのコードに当てる。

    1. 区分番号が同じで、適用日時点の価格が一致するコード（複数なら名称の近いもの）
    2. 価格だけが一致するコード（区分番号の誤読対策。名称の類似度が一定以上のもの）
    3. 区分番号＋名称の類似度
    当てられたら、表示名はマスターの名称（OCR の崩れた文字は category_ocr に残す）、価格はマスター値。
    """

    def __init__(self, matcher, sidx):
        self.m = matcher
        self.sidx = sidx
        self._by_price: dict[str, dict[float, list[str]]] = {}
        self.first_date = min((h[0][0] for h in sidx.hist.values() if h), default="2012-04-01")

    def _price_index(self, eff: str):
        if eff not in self._by_price:
            idx: dict[float, list[str]] = defaultdict(list)
            for code in self.sidx.hist:
                p = self.sidx.price_at(code, eff)
                if p:
                    idx[round(p)].append(code)
            self._by_price[eff] = idx
        return self._by_price[eff]

    def _at(self, code: str, eff: str):
        """適用日時点のマスター値。マスターの記録（平成24年4月〜）より前なら最初の版で代用（名称の比較用）。"""
        v = self.sidx.at(code, eff)
        if v is None and self.sidx.hist.get(code):
            v = self.sidx.hist[code][0][1]
        return v

    def _sim(self, text: str, code: str, eff: str) -> float:
        from difflib import SequenceMatcher
        v = self._at(code, eff)
        if not v or not text:
            return 0.0
        a = catmap.norm_category(text)
        b = catmap.norm_master(v[3] or v[2])
        return SequenceMatcher(None, a, b).ratio() if a and b else 0.0

    def apply(self, r: dict) -> None:
        eff = r.get("effective_date") or "9999-12-31"
        text, kno, price = r.get("category") or "", r.get("category_no"), r.get("price")
        allowed = set(catmap.SETTING_BEPPYO.get(r.get("setting") or "医科", ("2",)))
        best = None
        if not kno:
            m = re.match(r"^\D{0,2}(\d{3})", unicodedata.normalize("NFKC", text))
            kno = m.group(1) if m else None
        # 価格の候補: 読取値そのもの、先頭に単位（「1本」「1g」など）が数字として混ざった場合の末尾部分
        prices = []
        if price:
            ps = str(int(price)) if float(price).is_integer() else str(price)
            prices = [float(ps)] + [float(ps[k:]) for k in range(1, min(4, len(ps) - 1)) if ps[k:] and ps[k] != "0"]
        for pi, pv in enumerate(prices):
            allc = self._price_index(eff).get(round(pv), [])
            cands = [c for c in allc if (self.sidx.at(c, eff) or (None,) * 7)[4] in allowed]
            if not cands:  # 医科/歯科の判定（見出しのOCR）が誤っている場合
                cands = [c for c in allc if (self.sidx.at(c, eff) or (None,) * 7)[5] == kno]
            same_no = [c for c in cands if (self.sidx.at(c, eff) or (None,) * 7)[5] == kno]
            if same_no:
                best = (max(same_no, key=lambda c: self._sim(text, c, eff)), 0.9 if pi == 0 else 0.8)
                break
            if cands and pi == 0:
                c = max(cands, key=lambda c: self._sim(text, c, eff))
                if self._sim(text, c, eff) >= 0.35:
                    best = (c, 0.7)
                    break
        pre_master = eff < self.first_date
        if best is None and kno and price and not pre_master:
            # 価格の1桁誤読: 同じ区分番号で、価格が1文字違いのコードが1つだけなら採用
            ps = str(int(price)) if float(price).is_integer() else str(price)
            near = []
            for (bp, no), d in self.m.cands.items():
                if no != kno or bp not in allowed:
                    continue
                for c in d:
                    v = self.sidx.at(c, eff)
                    if not v or v[0] is None:
                        continue
                    ms = str(int(v[0])) if float(v[0]).is_integer() else str(v[0])
                    if len(ms) == len(ps) and sum(a != b for a, b in zip(ms, ps)) == 1:
                        near.append(c)
            if len(set(near)) == 1:
                best = (near[0], 0.6)
        if best is None and kno:
            pool = [(c, self._sim(text, c, eff)) for (bp, no), d in self.m.cands.items() if no == kno and bp in allowed
                    for c in d if self._at(c, eff)]
            if pool:
                c, sc = max(pool, key=lambda t: t[1])
                if sc >= 0.45:
                    best = (c, round(0.5 * sc, 2))
        r["category_ocr"] = text
        if r.get("kubun") == "R" and eff < "2018-04-01":  # 再製造（R）区分は平成30年度から。OCRの誤読
            r["kubun"] = "B"
        if best:
            code, score = best
            v = self._at(code, eff)
            r["category_code"], r["category_beppyo"], r["match_score"] = code, v[4], score
            r["setting"] = "歯科" if v[4] in ("4", "5", "6", "7") else "医科"
            r["category"] = f"{v[5]} {v[3] or v[2]}"
            r["category_no"] = v[5]
            # マスター記録より前（平成24年3月以前）は価格を確かめられないので OCR の読取値を残す
            if not pre_master or not price:
                r["price"] = v[0]
            elif v[0] and not (v[0] / 2.5 <= price <= v[0] * 2.5):
                r["price"] = None  # 平成24年の価格と桁違い → OCR の誤読とみなす
        else:
            r["category_code"], r["category_beppyo"], r["match_score"] = None, None, 0.0
            r["category"] = f"{kno or '???'} 機能区分未確定（OCR・{_fmt_price(price)}）"


def default_effective(link: NoticeLink) -> str | None:
    if link.effective_date:
        return link.effective_date
    if link.notice_date:
        y, m, _ = map(int, link.notice_date.split("-"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return date(y, m, 1).isoformat()
    return None


class SskIndex:
    """特定器材コードごとの（適用日 → 価格・名称・廃止日）の履歴。

    改定分ファイルは変更のあった項目だけが入っている（空欄 = 変更なし）ため、
    コードごとに適用日順・ファイル日付順に重ねて状態を復元する。
    """

    def __init__(self, rows: list[ssk.MasterRow]):
        by_code: dict[str, list[ssk.MasterRow]] = defaultdict(list)
        for r in rows:
            if r.code and r.change_date and r.change_date != "00000000":
                by_code[r.code].append(r)
        self.hist: dict[str, list[tuple]] = {}
        for code, rs in by_code.items():
            rs.sort(key=lambda r: (r.change_date, r.file_date))
            state = {"price": None, "unit": "", "name": "", "basic": "", "beppyo": "", "kubun": "", "abolish": ""}
            byd: dict[str, tuple] = {}
            for r in rs:
                st = dict(state)
                if r.price is not None:
                    st["price"] = r.price
                for k, v in (("unit", r.unit), ("name", r.name), ("basic", r.basic_name), ("beppyo", r.beppyo)):
                    if v:
                        st[k] = v
                if r.kubun_no and r.kubun_no != "000":
                    st["kubun"] = r.kubun_no
                if r.abolish_date:
                    ab = r.abolish_date
                    st["abolish"] = "" if ab in ("99999999", "00000000") else f"{ab[:4]}-{ab[4:6]}-{ab[6:]}"
                state = st
                d = f"{r.change_date[:4]}-{r.change_date[4:6]}-{r.change_date[6:]}"
                byd[d] = (st["price"], st["unit"], st["name"], st["basic"], st["beppyo"], st["kubun"], st["abolish"])
            collapsed = []
            for d, v in sorted(byd.items()):
                if collapsed and collapsed[-1][1] == v:
                    continue
                collapsed.append((d, v))
            self.hist[code] = collapsed
        self._dates = {c: [d for d, _ in h] for c, h in self.hist.items()}

    def at(self, code: str, d: str):
        h = self.hist.get(code)
        if not h:
            return None
        i = bisect.bisect_right(self._dates[code], d) - 1
        return h[i][1] if i >= 0 else None

    def price_at(self, code: str, d: str):
        v = self.at(code, d)
        return v[0] if v else None


def build(db_path: Path, manifest: list[NoticeLink], parsed_dir: Path, ssk_dir: Path, log=print) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    links = {m.doc_id: m for m in manifest}

    # ---- 1. 通知と解析結果の読込 -----------------------------------------
    raw_by_doc: dict[str, list[dict]] = {}
    for m in manifest:
        from .pipeline import read_parsed
        d = read_parsed(parsed_dir / f"{m.doc_id}.json.gz")
        status, n, warns = "未解析", 0, []
        if d is not None:
            if d.get("scanned") and not d.get("ocr"):
                status = "スキャン画像（未対応）"
            else:
                is_ocr = bool(d.get("ocr"))
                recs = d["records"]
                eff = default_effective(m)
                kept = []
                for r in recs:
                    r["ocr"] = 1 if is_ocr else 0
                    if is_ocr:
                        r["effective_date"] = _sane_effective(r.get("effective_date"), m.notice_date, eff)
                        if not _usable_ocr_approval(r):
                            continue
                        # 既知のJANと照合できず、チェックデジットも合わない製品コードは誤読とみなして捨てる
                        pc = r.get("product_code")
                        if pc and r.get("code_fix") == "none" and not gtin_ok(pc):
                            r["product_code"] = None
                    r["effective_date"] = r.get("effective_date") or eff
                    kept.append(r)
                raw_by_doc[m.doc_id] = kept
                status = "OCR（要確認）" if is_ocr else "解析済"
                n, warns = len(kept), d.get("warnings", [])
        con.execute("INSERT INTO notices VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (m.doc_id, m.title, m.url, m.kind, m.notice_date, default_effective(m), m.fiscal_section,
                     status, n, json.dumps(warns[:50], ensure_ascii=False)))
    cols = ["doc_id", "page", "setting", "action", "kubun", "effective_date", "approval_no", "sales_name",
            "product_name", "product_code", "annex", "applicant", "category", "category_no", "price_text", "price",
            "price_unit", "heading", "side", "ref_notice_date", "ocr"]
    for did, recs in raw_by_doc.items():
        con.executemany(f"INSERT INTO raw_rows VALUES ({','.join('?' * len(cols))})",
                        [tuple(r.get(c) for c in cols) for r in recs])

    # ---- 2. 訂正の反映 ----------------------------------------------------
    by_date_notice: dict[str, list[str]] = defaultdict(list)
    for m in manifest:
        if m.kind == "notice" and m.notice_date:
            by_date_notice[m.notice_date].append(m.doc_id)
    listing: dict[str, list[dict]] = {}
    for did, recs in raw_by_doc.items():
        k = links[did].kind
        if k in ("notice", "replacement", "amendment"):
            listing[did] = [dict(r, corrected_by=None) for r in recs if r.get("side") in (None, "added", "right")]

    def key(r):
        if "_k" not in r:
            r["_k"] = (r["approval_no"], r.get("product_code"), norm_cat_text(r.get("category")))
        return r["_k"]

    def target_of(recs):
        for r in recs:
            ids = by_date_notice.get(r.get("ref_notice_date") or "", [])
            if ids:
                return ids[0]
        return None

    # 別紙の差し替え: 対象通知の掲載を差し替え後の内容で置き換える
    for m in manifest:
        if m.kind == "replacement" and m.doc_id in listing:
            tgt = target_of(raw_by_doc[m.doc_id])
            if tgt and tgt in listing:
                listing[tgt] = []

    corr_docs = sorted([m for m in manifest if m.kind == "correction" and m.doc_id in raw_by_doc],
                       key=lambda m: m.notice_date or "")
    n_corr = 0
    corr_rows = []
    for m in corr_docs:
        recs = raw_by_doc[m.doc_id]
        by_tgt: dict[str | None, list[dict]] = defaultdict(list)
        for r in recs:
            ids = by_date_notice.get(r.get("ref_notice_date") or "", [])
            by_tgt[ids[0] if ids else None].append(r)
        for tgt, rs in by_tgt.items():
            rows = listing.get(tgt) if tgt else None
            dels = [r for r in rs if r.get("side") in ("wrong", "deleted")]
            adds = [r for r in rs if r.get("side") in ("right", "added")]
            removed_kubun = {}
            if rows is not None and dels:
                full = {key(r) for r in dels if r.get("product_code")}
                part = {(r["approval_no"], norm_cat_text(r.get("category"))) for r in dels if not r.get("product_code")}
                keep, hit = [], defaultdict(int)
                for x in rows:
                    k = key(x)
                    if k in full:
                        hit[k] += 1
                        removed_kubun[x["approval_no"]] = x.get("kubun")
                    elif (k[0], k[2]) in part:
                        hit[(k[0], k[2])] += 1
                        removed_kubun[x["approval_no"]] = x.get("kubun")
                    else:
                        keep.append(x)
                rows[:] = keep
            for r in dels:
                k = key(r)
                mt = (hit.get(k, 0) if r.get("product_code") else hit.get((k[0], k[2]), 0)) if rows is not None else 0
                corr_rows.append((m.doc_id, m.notice_date, tgt, r.get("ref_notice_date"), r["approval_no"],
                                  r.get("side"), r.get("product_code"), r.get("category"), r.get("price"), int(mt > 0)))
            for r in adds:
                if rows is not None:
                    nr = dict(r, corrected_by=m.doc_id, doc_id=tgt)
                    nr.pop("_k", None)
                    if not nr.get("kubun"):
                        nr["kubun"] = removed_kubun.get(r["approval_no"]) or next(
                            (x.get("kubun") for x in rows if x["approval_no"] == r["approval_no"]), None)
                    rows.append(nr)
                corr_rows.append((m.doc_id, m.notice_date, tgt, r.get("ref_notice_date"), r["approval_no"],
                                  r.get("side"), r.get("product_code"), r.get("category"), r.get("price"),
                                  int(rows is not None)))
            n_corr += len(rs)
    con.executemany("INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?)", corr_rows)
    log(f"  訂正 {len(corr_docs)}本 / {n_corr}行を反映")

    # ---- 3. 特定器材マスター ---------------------------------------------
    mrows = ssk.parse_all(ssk_dir) if ssk_dir.exists() else []
    sidx = SskIndex(mrows)
    for code, h in sidx.hist.items():
        con.executemany("INSERT INTO ssk_history VALUES (?,?,?,?,?,?,?,?,?)",
                        [(code, d, v[0], v[1], v[2], v[3], v[4], v[5], v[6]) for d, v in h])
    matcher = catmap.CategoryMatcher(mrows)
    log(f"  特定器材マスター {len(mrows):,}行 / コード {len(sidx.hist):,}件")

    # ---- 4. 掲載行（B/C区分）と機能区分の対応付け ---------------------------
    final = []
    for did, rows in listing.items():
        for r in rows:
            if not r.get("kubun") or not TARGET_KUBUN.match(r["kubun"]):
                continue
            final.append(r)
    final.sort(key=lambda r: (r.get("effective_date") or "", r["doc_id"]))
    # 同じ表記でも時期によって対応するコードが変わる（改定でのコード再利用）ため、適用日ごとに判定する
    cmap: dict[tuple, tuple] = {}
    ocr_matcher = OcrCategoryMatcher(matcher, sidx)
    for r in final:
        if r.get("ocr"):
            ocr_matcher.apply(r)
            continue
        if not r.get("category"):
            r["category_code"], r["category_beppyo"], r["match_score"] = None, None, 0.0
            continue
        eff = r.get("effective_date") or "9999-12-31"
        k = (r.get("setting") or "医科", r["category"], eff, r.get("price"))
        if k not in cmap:
            price = r.get("price")

            def ok(code, eff=eff, price=price):
                p = sidx.price_at(code, eff)
                return price is not None and p is not None and abs(p - price) < 0.5

            cmap[k] = matcher.match(k[0], k[1], price_ok=ok)
        r["category_code"], r["category_beppyo"], r["match_score"] = cmap[k]
    best: dict[tuple, tuple] = {}
    for (st, cat, eff, price), v in cmap.items():  # OCR 行は含めない
        if (st, cat) not in best or (v[2] or 0) > (best[(st, cat)][2] or 0):
            best[(st, cat)] = v
    con.executemany("INSERT INTO category_map VALUES (?,?,?,?,?)", [(k[0], k[1], *v) for k, v in best.items()])
    con.executemany(
        "INSERT INTO listing(doc_id,effective_date,setting,action,kubun,approval_no,sales_name,product_name,"
        "product_code,applicant,category,category_norm,category_no,price,price_unit,price_text,corrected_by,"
        "category_code,category_beppyo,match_score,ocr,category_ocr) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["doc_id"], r.get("effective_date"), r.get("setting"), r.get("action"), r.get("kubun"), r["approval_no"],
          r.get("sales_name"), r.get("product_name"), r.get("product_code"), r.get("applicant"), r.get("category"),
          norm_cat_text(r.get("category")), r.get("category_no"), r.get("price"), r.get("price_unit"),
          r.get("price_text"), r.get("corrected_by"), r.get("category_code"), r.get("category_beppyo"),
          r.get("match_score"), r.get("ocr", 0), r.get("category_ocr")) for r in final])
    matched = sum(1 for v in best.values() if v[0])
    n_ok = sum(1 for r in final if r.get("category_code"))
    log(f"  掲載行 {len(final):,}（マスター対応 {n_ok / max(1, len(final)):.1%}）/ 機能区分 {len(best):,}種（対応 {matched:,}）")
    n_ocr = sum(1 for r in final if r.get("ocr"))
    if n_ocr:
        n_ocr_ok = sum(1 for r in final if r.get("ocr") and r.get("category_code"))
        log(f"  うちOCR由来 {n_ocr:,}行（機能区分を特定 {n_ocr_ok / n_ocr:.1%}）")

    # ---- 5. イベント生成 ---------------------------------------------------
    make_events(con, final, sidx, links)
    con.execute("INSERT INTO meta VALUES ('built_at', datetime('now','localtime'))")
    con.commit()
    con.close()


def _fmt_price(p, unit=None):
    if p is None:
        return "—"
    s = f"¥{p:,.0f}" if float(p).is_integer() else f"¥{p:,.2f}"
    return f"{unit} {s}" if unit else s


def _norm_kubun(k: str) -> str:
    """区分の比較用。平成30年度より前の「区分B」は現在の B1 に当たる。数字の無い C は OCR の読み落とし。"""
    k = (k or "").strip()
    if k == "B":
        return "B1"
    if k == "C":
        return "C?"
    return k


def _nm(s) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    t = re.sub(r"[‐‑‒–—―−－~〜～]", "-", t)
    return re.sub(r"[\s　・･]", "", t).upper()


def _cat_label(c: dict) -> str:
    return f"{c.get('category')}（{_fmt_price(c.get('price'), c.get('price_unit'))}）"


def make_events(con: sqlite3.Connection, final: list[dict], sidx: SskIndex, links: dict) -> None:
    """承認番号ごとの変更イベントと現在の状態を作る。

    機能区分は製品コード単位で追跡する。ある製品が後の通知で同じ区分番号（先頭3桁）の別の機能区分に
    掲載されたら、それは「機能区分の変更」（例: 改定時の区分見直しによる移行）とみなし、旧区分は現在の
    状態から外す。区分番号が異なる追加掲載は「機能区分の追加」として旧区分と併存させる。
    """
    today = date.today().isoformat()
    by_appr: dict[str, list[dict]] = defaultdict(list)
    for r in final:
        by_appr[r["approval_no"]].append(r)
    ev_rows, ap_rows, nc_rows = [], [], []
    corr = defaultdict(list)
    for row in con.execute("SELECT correction_doc, correction_date, target_date, approval_no, side, category, price, "
                           "product_code FROM corrections"):
        corr[row[3]].append(row)

    for appr, rows in by_appr.items():
        rows.sort(key=lambda r: (r.get("effective_date") or "", r["doc_id"]))
        groups = defaultdict(list)
        for r in rows:
            groups[(r.get("effective_date") or "", r["doc_id"])].append(r)
        prod_state: dict[str, dict[str, dict]] = {}   # 製品コード -> {機能区分(正規化): 行}
        nocode_state: dict[str, dict] = {}            # 製品コードの無い掲載（承認番号単位）
        seen_cats: dict[str, dict] = {}               # これまでに掲載された機能区分
        prod_name: dict[str, str] = {}                # 製品コード -> 製品名（直近の掲載）
        seen_kubun: set[str] = set()
        last_sales = None
        evs = []
        first = True
        for (eff, did), g in sorted(groups.items()):
            kub = sorted({x.get("kubun") or "" for x in g})
            src = "notice_ocr" if any(x.get("ocr") for x in g) else "notice"
            # この通知の直前の状態（通知ごとの変更点の「変更前」）
            before = {}
            for cs in prod_state.values():
                for k, c in cs.items():
                    before.setdefault(k, c)
            for k, c in nocode_state.items():
                before.setdefault(k, c)
            known_codes = set(prod_state)
            cats = {}
            for x in g:
                if x.get("category"):
                    cats.setdefault(cat_key(x), x)
            by_code: dict[str, dict[str, dict]] = defaultdict(dict)
            for x in g:
                if not x.get("category"):
                    if x.get("product_code"):
                        prod_state.setdefault(x["product_code"], {})
                    continue
                if x.get("product_code"):
                    by_code[x["product_code"]][cat_key(x)] = x
                else:
                    nocode_state[cat_key(x)] = x
            new_codes = set(by_code) - set(prod_state)
            moves: dict[tuple[str, str], int] = defaultdict(int)
            for code, newcats in by_code.items():
                old = prod_state.get(code, {})
                for on, orow in list(old.items()):
                    if on in newcats:
                        continue
                    same = [nn for nn, nrow in newcats.items() if nrow.get("category_no") and
                            nrow.get("category_no") == orow.get("category_no")]
                    if same:
                        for nn in same:
                            moves[(on, nn)] += 1
                        del old[on]
                old.update(newcats)
                prod_state[code] = old
            moved_to = {nn for (_, nn) in moves}
            if first:
                detail = "; ".join(_cat_label(c) for c in cats.values())
                evs.append((appr, eff, "listing_new", f"保険適用（区分{'/'.join(kub)}）",
                            f"{detail}／製品{len(new_codes)}件", g[0].get("setting"), "/".join(kub), None, None,
                            None, None, did, src))
            else:
                for (on, nn), n in sorted(moves.items(), key=lambda t: -t[1]):
                    o, c = seen_cats.get(on) or {}, cats[nn]
                    evs.append((appr, eff, "category_changed", f"機能区分の変更（区分{c.get('kubun')}）",
                                f"{o.get('category', on)} → {c.get('category')}（{n}製品）",
                                c.get("setting"), c.get("kubun"), c.get("category"), c.get("category_code"),
                                o.get("price"), c.get("price"), did, src))
                for cn, c in cats.items():
                    if cn not in seen_cats and cn not in moved_to:
                        evs.append((appr, eff, "category_added", f"機能区分の追加（区分{c.get('kubun')}）",
                                    _cat_label(c), c.get("setting"), c.get("kubun"), c.get("category"),
                                    c.get("category_code"), None, c.get("price"), did, src))
                    elif cn in seen_cats:
                        prev = seen_cats[cn]
                        if (c.get("price") is not None and prev.get("price") is not None
                                and abs(c["price"] - prev["price"]) >= 0.5):
                            code = c.get("category_code")
                            explained = code and sidx.price_at(code, eff) is not None and \
                                abs(sidx.price_at(code, eff) - c["price"]) < 0.5
                            if not explained:
                                evs.append((appr, eff, "notice_price_diff", "通知上の償還価格が前回と異なる",
                                            f"{c['category']}: {_fmt_price(prev['price'])} → {_fmt_price(c['price'])}",
                                            c.get("setting"), c.get("kubun"), c.get("category"), code,
                                            prev["price"], c["price"], did, src))
                if new_codes:
                    names = sorted({(x.get("product_name") or "")[:40] for x in g if x.get("product_code") in new_codes})
                    evs.append((appr, eff, "products_added", f"製品の追加（{len(new_codes)}件）",
                                ", ".join(names)[:300], g[0].get("setting"), "/".join(kub), None, None, None, None,
                                did, src))
            # ---- 通知ごとの変更点 ----
            is_ocr = src == "notice_ocr"
            types, parts = [], []
            kub_n = {_norm_kubun(k) for k in kub if k}
            moves_l = [{"from": (seen_cats.get(on) or {}).get("category", on), "to": cats[nn].get("category"),
                        "n": n, "price_from": (seen_cats.get(on) or {}).get("price"), "price_to": cats[nn].get("price")}
                       for (on, nn), n in sorted(moves.items(), key=lambda t: -t[1])]
            added_l = [c.get("category") for cn, c in cats.items() if cn not in seen_cats and cn not in moved_to]
            if first:
                types.append("新規")
                parts.append(f"区分{'/'.join(kub)}で保険適用（機能区分{len(cats)}・製品{len(new_codes)}件）")
            else:
                newk = {k for k in kub_n if k not in seen_kubun and not k.endswith("?")}
                if newk and seen_kubun:
                    types.append("区分変更")
                    parts.append(f"区分{'/'.join(sorted(kub_n))}で掲載（これまで {'/'.join(sorted(seen_kubun))}）")
                if moves_l:
                    types.append("機能区分の変更")
                    parts += [f"{m['from']} → {m['to']}（{m['n']}製品）" for m in moves_l[:3]]
                if added_l:
                    types.append("機能区分の追加")
                    parts += [f"追加: {a}" for a in added_l[:3]]
                if new_codes:
                    types.append("製品追加")
                    parts.append(f"製品 {len(new_codes)}件 追加")
                if not is_ocr:
                    renamed = [(cd, prod_name[cd], x.get("product_name")) for cd in set(by_code) & known_codes
                               for x in [next(iter(by_code[cd].values()))]
                               if cd in prod_name and _nm(prod_name[cd]) != _nm(x.get("product_name"))
                               and x.get("product_name")]
                    if renamed:
                        types.append("製品名変更")
                        parts.append(f"製品名の変更 {len(renamed)}件（例: {renamed[0][1][:20]} → {renamed[0][2][:20]}）")
                    sales = g[0].get("sales_name")
                    if last_sales and sales and _nm(sales) != _nm(last_sales):
                        types.append("販売名変更")
                        parts.append(f"販売名 {last_sales[:25]} → {sales[:25]}")
                if any(e[11] == did and e[2] == "notice_price_diff" for e in evs):
                    types.append("価格差")
                if not types:
                    types.append("変更なし")
                    parts.append("既存の製品・機能区分の再掲載（製品名・製品コードの記載変更など）")
            nc_rows.append((did, eff, appr, g[0].get("sales_name"), g[0].get("applicant"), "/".join(kub),
                            "/".join(sorted({x.get("action") or "" for x in g})), ",".join(types), "／".join(parts),
                            json.dumps({"before": sorted({c.get("category") for c in before.values() if c.get("category")}),
                                        "after": sorted({c.get("category") for c in cats.values()}),
                                        "moves": moves_l, "added": added_l, "n_products": len(by_code),
                                        "n_new": len(new_codes),
                                        "prices": {c.get("category"): c.get("price") for c in cats.values()}},
                                       ensure_ascii=False), 1 if is_ocr else 0))
            for x in g:
                if x.get("product_code") and x.get("product_name"):
                    prod_name[x["product_code"]] = x["product_name"]
            seen_kubun |= {k for k in kub_n if not k.endswith("?")}
            last_sales = g[0].get("sales_name") or last_sales
            for cn, c in cats.items():
                seen_cats[cn] = c
            first = False
        first_date = rows[0].get("effective_date") or ""

        # 現在の機能区分（製品コード単位の最新状態の和集合）
        cur_cats: dict[str, dict] = {}
        n_by_cat: dict[str, int] = defaultdict(int)
        for code, cs in prod_state.items():
            for cn, c in cs.items():
                cur_cats.setdefault(cn, c)
                n_by_cat[cn] += 1
        for cn, c in nocode_state.items():
            if cn not in cur_cats and not any(
                    x.get("category_no") == c.get("category_no") and x.get("effective_date", "") > c.get("effective_date", "")
                    for x in cur_cats.values()):
                cur_cats[cn] = c
        # 最初の掲載日（その機能区分に初めて載った日）
        first_seen = {}
        for r in rows:
            if r.get("category"):
                first_seen.setdefault(cat_key(r), r.get("effective_date"))

        # 機能区分（マスター）側の価格改定・名称変更・廃止
        codes = {}
        for cn, c in cur_cats.items():
            if c.get("category_code"):
                codes.setdefault(c["category_code"], (cn, c))
        for code, (cn, c) in codes.items():
            since = first_seen.get(cn) or first_date
            h = sidx.hist.get(code, [])
            prev = None
            for d, v in h:
                if prev is not None and since < d <= today:
                    if prev[0] != v[0]:
                        evs.append((appr, d, "price_revision", "償還価格の改定",
                                    f"{v[3] or v[2]}: {_fmt_price(prev[0])} → {_fmt_price(v[0])}",
                                    c.get("setting"), c.get("kubun"), c.get("category"), code, prev[0], v[0],
                                    None, "ssk"))
                    if prev[3] and v[3] and prev[3] != v[3]:
                        evs.append((appr, d, "category_renamed", "機能区分の名称変更",
                                    f"{prev[3]} → {v[3]}", c.get("setting"), c.get("kubun"), c.get("category"),
                                    code, None, None, None, "ssk"))
                prev = v
            if h and h[-1][1][6]:
                ab = h[-1][1][6]
                evs.append((appr, ab, "category_abolished",
                            "機能区分の廃止" if ab <= today else "機能区分の廃止予定",
                            f"{h[-1][1][3] or h[-1][1][2]}（{ab} まで）", c.get("setting"), c.get("kubun"),
                            c.get("category"), code, h[-1][1][0], None, None, "ssk"))
        evs.extend(_correction_events(appr, corr.get(appr, [])))
        evs.sort(key=lambda e: (e[1], EV_SORT.get(e[2], 9)))
        ev_rows.extend(evs)

        cur = []
        for cn, c in cur_cats.items():
            code = c.get("category_code")
            v = sidx.at(code, today) if code else None
            cur.append({
                "category": c.get("category"), "kubun": c.get("kubun"), "setting": c.get("setting"),
                "code": code, "master_name": (v[3] or v[2]) if v else None,
                "price_now": v[0] if v else None, "price_listed": c.get("price"), "unit": c.get("price_unit"),
                "abolished": (v[6] if v and v[6] and v[6] <= today else None),
                "since": first_seen.get(cn), "n_products": n_by_cat.get(cn, 0),
            })
        cur.sort(key=lambda x: (x.get("category") or ""))
        flags = []
        if any(r.get("ocr") for r in rows):
            flags.append("OCR由来の掲載あり")
        if any(x["code"] is None for x in cur):
            flags.append("マスター未対応の機能区分あり")
        if any(x["abolished"] for x in cur):
            flags.append("廃止済の機能区分あり")
        last = rows[-1]
        ap_rows.append((appr, last.get("sales_name"), last.get("applicant"), first_date,
                        rows[-1].get("effective_date"), "/".join(sorted({r.get("kubun") or "" for r in rows})),
                        len(prod_state), len(cur), len(evs), json.dumps(cur, ensure_ascii=False), ";".join(flags)))
    con.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ev_rows)
    con.executemany("INSERT INTO notice_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)", nc_rows)
    con.executemany("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)", ap_rows)


EV_SORT = {"listing_new": 0, "category_changed": 1, "category_added": 2, "products_added": 3,
           "notice_price_diff": 4, "price_revision": 5, "category_renamed": 6, "category_abolished": 7,
           "correction": 8}


def _correction_events(appr: str, rows: list[tuple]) -> list[tuple]:
    """訂正通知1本ごとに、（誤）と（正）の差分を1件のイベントにまとめる。"""
    by_doc: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_doc[r[0]].append(r)
    out = []
    for cdoc, rs in by_doc.items():
        cdate, tdate = rs[0][1] or "", rs[0][2] or ""
        W = [r for r in rs if r[4] in ("wrong", "deleted")]
        R = [r for r in rs if r[4] in ("right", "added")]
        wc = {(norm_cat_text(r[5]), r[6]) for r in W}
        rc = {(norm_cat_text(r[5]), r[6]) for r in R}
        wp = {r[7] for r in W if r[7]}
        rp = {r[7] for r in R if r[7]}
        catname = {norm_cat_text(r[5]): r[5] for r in rs}
        parts = []
        if R and not W:
            title = "追加掲載（訂正通知）"
            parts.append("、".join(sorted({f"{catname[c]}（{_fmt_price(p)}）" for c, p in rc}))[:200])
            if rp:
                parts.append(f"製品{len(rp)}件")
        elif W and not R:
            title = "掲載の削除（訂正通知）"
            parts.append("、".join(sorted({f"{catname[c]}（{_fmt_price(p)}）" for c, p in wc}))[:200])
            if wp:
                parts.append(f"製品{len(wp)}件")
        else:
            title = "掲載内容の訂正"
            wcat, rcat = {c for c, _ in wc}, {c for c, _ in rc}
            for c in sorted(wcat - rcat):
                parts.append(f"機能区分 削除: {catname[c]}")
            for c in sorted(rcat - wcat):
                parts.append(f"機能区分 追加: {catname[c]}")
            for c in sorted(wcat & rcat):
                a = {p for cc, p in wc if cc == c}
                b = {p for cc, p in rc if cc == c}
                if a != b:
                    parts.append(f"{catname[c]}: {'/'.join(_fmt_price(x) for x in sorted(a, key=lambda v: v or 0))} → "
                                 f"{'/'.join(_fmt_price(x) for x in sorted(b, key=lambda v: v or 0))}")
            if wp != rp:
                if rp - wp:
                    parts.append(f"製品コード 追加{len(rp - wp)}件")
                if wp - rp:
                    parts.append(f"製品コード 削除{len(wp - rp)}件")
            if not parts:
                parts.append("販売名・承認番号・製品名などの記載の訂正")
        out.append((appr, cdate, "correction", title, f"{tdate} 付通知について: " + "／".join(parts), None, None,
                    None, None, None, None, cdoc, "correction"))
    return out


def _dedupe_corrections(evs: list[tuple]) -> list[tuple]:
    out, seen = [], set()
    for e in evs:
        if e[2] == "correction":
            k = (e[1], e[3], e[7], e[10], e[11])
            if k in seen:
                continue
            seen.add(k)
        out.append(e)
    return out

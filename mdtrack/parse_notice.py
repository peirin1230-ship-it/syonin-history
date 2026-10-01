"""「医療機器の保険適用について」通知PDF（テキストPDF）の表を解析する。

平成28年度頃以降の通知は罫線付きの表（Excel由来）で、pdfplumber の表検出で
セル単位に取り出せる。1行 = 1製品（製品コード）で、承認番号・販売名・保険適用希望者・
決定機能区分・償還価格は縦に結合されたセル（続き行では None）になっている。

出力は「承認番号 × 製品コード × 決定機能区分」単位のレコード。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field, asdict

import pdfplumber

from . import jpdate

PARSER_VERSION = 4  # 解析ロジックを変えたら上げる（キャッシュを作り直す）

# 承認番号・認証番号・届出番号（例: 30800BZX00176000, 308AABZX00017000, 13B1X10166001027, 229ABBZX00080Z00）
APPROVAL_RE = re.compile(r"^[0-9A-Z]{3,5}[0-9A-Z]{2,4}[0-9A-Z]{6,10}$")
CODE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{8,14})(?![0-9A-Za-z])")
ANNEX_RE = re.compile(r"別表\s*([0-9０-９]+)\s*の?とおり")
KUBUN_RE = re.compile(r"区分\s*([ABCEFRＡＢＣＥＦＲ])\s*([0-9０-９]?)")
PRICE_RE = re.compile(r"[¥￥]\s*([0-9,，]+(?:\.[0-9]+)?)")


@dataclass
class Section:
    setting: str = "医科"          # 医科 / 歯科
    action: str = "new"            # new（新たな保険適用）/ add（製品名・製品コードの追加・変更）/ other
    kubun: str | None = None       # A2 / B / B1 / B2 / B3 / C1 / C2 / R ...
    effective_date: str | None = None
    heading: str = ""
    columns: list[str] = field(default_factory=list)


@dataclass
class Record:
    doc_id: str
    page: int
    setting: str
    action: str
    kubun: str | None
    effective_date: str | None
    approval_no: str
    sales_name: str
    product_name: str
    product_code: str | None
    annex: str | None              # 「別表Nのとおり」の N
    applicant: str
    category: str                  # 決定機能区分（B/C）または特定診療報酬算定医療機器の区分（A2）
    category_no: str | None        # 決定機能区分の先頭3桁（材料価格基準の区分番号）
    price_text: str
    price: float | None
    price_unit: str | None         # 「1㎠当たり」「1g」など
    heading: str
    side: str | None = None        # 訂正通知の（誤）=wrong /（正）=right
    ref_notice_date: str | None = None  # 訂正通知が対象とする元通知の日付

    def as_dict(self):
        return asdict(self)


def clean(s: str | None) -> str:
    if s is None:
        return ""
    s = s.replace("　", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def oneline(s: str | None) -> str:
    """セル内改行を除去（日本語は詰め、英数字の間は空白）。"""
    s = clean(s)
    parts = [p.strip() for p in s.split("\n") if p.strip()]
    out = ""
    for p in parts:
        if out and re.search(r"[0-9A-Za-z)）]$", out) and re.match(r"^[0-9A-Za-z(（]", p):
            out += " " + p
        else:
            out += p
    return out


def norm_approval(s: str) -> str:
    return re.sub(r"\s+", "", jpdate.normalize(s or "")).upper()


def is_approval(s: str | None) -> bool:
    v = norm_approval(s or "")
    return bool(v) and len(v) >= 14 and bool(APPROVAL_RE.match(v)) and any(ch.isdigit() for ch in v[:3])


def gtin_ok(code: str) -> bool:
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = 0
    for i, d in enumerate(reversed(body)):
        total += d * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def split_products(cell: str) -> tuple[list[tuple[str, str | None]], str | None]:
    """「製品名 製品コード」セルを (製品名, 製品コード) のリストに分解する。

    - セル内で製品コードが折り返されている場合（13桁+1桁）はGS1チェックデジットで連結判定
    - 1セルに複数コードが並ぶ場合は複数製品として返す
    - 「別表Nのとおり」は annex 番号を返す
    """
    txt = clean(cell)
    m = ANNEX_RE.search(jpdate.normalize(txt))
    if m:
        return [], jpdate.normalize(m.group(1))
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    # 折り返しコードの連結
    merged: list[str] = []
    for l in lines:
        if merged and re.fullmatch(r"\d{1,2}", l):
            prev = merged[-1]
            mm = re.search(r"(\d{8,13})$", prev)
            if mm:
                cand = mm.group(1) + l
                if len(cand) <= 14 and (gtin_ok(cand) or not gtin_ok(mm.group(1))):
                    merged[-1] = prev + l
                    continue
        merged.append(l)
    products: list[tuple[str, str | None]] = []
    name_buf: list[str] = []
    last_name = ""
    for l in merged:
        codes = CODE_RE.findall(l)
        if codes:
            name_part = CODE_RE.sub("", l).strip()
            nm = oneline("\n".join(name_buf + ([name_part] if name_part else [])))
            name_buf = []
            if not nm:
                nm = last_name
            for c in codes:
                products.append((nm, c))
            last_name = nm
        else:
            # コードのない行：製品名の折り返し（前後どちらに付くかは不明なので、
            # 直前の製品にコードが付いた後なら直前の製品名に追記する）
            if products and not name_buf and products[-1][0] == last_name and len(products) == 1:
                n, c = products[-1]
                products[-1] = (oneline(n + "\n" + l), c)
                last_name = products[-1][0]
            else:
                name_buf.append(l)
    if name_buf:
        nm = oneline("\n".join(name_buf))
        if products and len(products) == 1:
            n, c = products[0]
            products[0] = (oneline(n + "\n" + nm), c)
        else:
            products.append((nm, None))
    return products, None


def parse_price(s: str) -> tuple[float | None, str | None]:
    t = jpdate.normalize(oneline(s))
    m = PRICE_RE.search(t)
    if not m:
        return None, None
    v = float(m.group(1).replace(",", ""))
    unit = t[: m.start()].strip() or None
    return v, unit


def parse_heading(text: str, sec: Section) -> Section:
    """表の上・表のタイトル行のテキストからセクション情報を更新する。"""
    t = jpdate.normalize(text)
    new = Section(**{**asdict(sec), "columns": list(sec.columns)})
    if re.search(r"[12１２][.．]\s*医科|^\s*医科\s*$", t, re.M):
        new.setting = "医科"
    if re.search(r"[12１２３][.．]\s*歯科|^\s*歯科\s*$", t, re.M):
        new.setting = "歯科"
    if re.search(r"[3３][.．]\s*調剤", t):
        new.setting = "調剤"
    changed = False
    if "新たな保険適用" in t:
        new.action, changed = "new", True
    elif "追加・変更" in t or "追加･変更" in t:
        new.action, changed = "add", True
    km = KUBUN_RE.search(t)
    if km:
        new.kubun = jpdate.normalize(km.group(1) + km.group(2))
        changed = True
    if "保険適用開始年月日" in t or "適用開始" in t:
        d = jpdate.parse_first(t[t.find("適用開始"):])
        if d:
            new.effective_date = d.isoformat()
    if changed:
        new.heading = re.sub(r"\s+", " ", t).strip()[:200]
    return new


HEADER_KEYS = {
    "approval": ("承認番号",),
    "sales": ("販売名",),
    "product": ("製品名",),
    "code": ("製品コード",),
    "applicant": ("保険適用希望者", "希望者"),
    "category": ("決定機能区分", "特定診療報酬算定医療機器の区分", "機能区分"),
    "price": ("償還価格",),
}


def map_header(row: list[str | None]) -> dict[str, int] | None:
    cells = [jpdate.normalize(clean(c)).replace("\n", "") for c in row]
    if not any("承認番号" in c for c in cells):
        return None
    mp: dict[str, int] = {}
    for i, c in enumerate(cells):
        for k, keys in HEADER_KEYS.items():
            if k in mp:
                continue
            if any(key in c for key in keys):
                mp[k] = i
                # 「製品名 製品コード」が1セルのとき
                if k == "product" and "製品コード" in c:
                    mp["code"] = i
    return mp


def _center_in(c, x0, top, x1, bottom) -> bool:
    cx = (c["x0"] + c["x1"]) / 2
    cy = (c["top"] + c["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def chars_text(chars) -> str:
    if not chars:
        return ""
    return pdfplumber.utils.extract_text(list(chars), x_tolerance=1.5, y_tolerance=3) or ""


class Grid:
    """pdfplumber の表を「列のx範囲 × 行のy範囲」に直し、結合セル（罫線欠落を含む）を復元する。"""

    def __init__(self, page, table):
        self.page = page
        self.table = table
        self.rows = table.rows
        x0, top, x1, bottom = table.bbox
        self.bbox = table.bbox
        chars = [c for c in page.chars if _center_in(c, x0 - 1, top - 1, x1 + 1, bottom + 1)]
        chars.sort(key=lambda c: (c["top"] + c["bottom"]) / 2)
        self.chars = chars
        self._cy = [(c["top"] + c["bottom"]) / 2 for c in chars]
        self.bands = []
        for r in self.rows:
            cells = [c for c in r.cells if c]
            if not cells:
                self.bands.append(None)
                continue
            self.bands.append((min(c[1] for c in cells), min(c[3] for c in cells)))

    def text(self, x0, top, x1, bottom) -> str:
        lo = bisect.bisect_left(self._cy, top)
        hi = bisect.bisect_right(self._cy, bottom)
        sel = [c for c in self.chars[lo:hi] if x0 <= (c["x0"] + c["x1"]) / 2 <= x1]
        sel.sort(key=lambda c: (round(c["top"]), c["x0"]))
        return chars_text(sel)

    def band_text(self, i) -> str:
        b = self.bands[i]
        if not b:
            return ""
        return self.text(self.bbox[0], b[0], self.bbox[2], b[1])

    def cell_in_col(self, i, x0, x1):
        """行 i のうち、列 [x0,x1] に重なるセル（無ければ None）。"""
        best = None
        for c in self.rows[i].cells:
            if not c:
                continue
            ov = min(c[2], x1) - max(c[0], x0)
            if ov > (x1 - x0) * 0.5:
                best = c
                break
        return best


class NoticeParser:
    def __init__(self, doc_id: str, default_effective: str | None = None):
        self.doc_id = doc_id
        self.sec = Section(effective_date=default_effective)
        self.colkeys: list[str] | None = None     # 列順のキー（ヘッダー無しページ用）
        self.records: list[Record] = []
        self.annex: dict[str, list[tuple[str, str]]] = {}
        self._annex_labels: list[str] = []
        self.headings: list[str] = []
        self._last: dict | None = None
        self.warnings: list[str] = []
        self.side: str | None = None
        self.ref_notice_date: str | None = None

    # ------------------------------------------------------------------
    def parse(self, path: str) -> "NoticeParser":
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                self._parse_page(page, pno)
        self._expand_annex()
        return self

    def _parse_page(self, page, pno: int) -> None:
        ptxt = jpdate.normalize(page.extract_text() or "")
        if "承認番号" not in ptxt and "製品コード" in ptxt and (
                re.search(r"(^|\s)別表\s*[0-9]+(\s|$)", ptxt, re.M) or getattr(self, "_annex_carry", None)):
            self._parse_annex_page(page, pno)
            return
        try:
            tables = page.find_tables()
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"p{pno}: 表検出失敗 {e}")
            return
        if not tables:
            if ptxt.strip():
                self._on_text(page.extract_text() or "")
            return
        tables = sorted(tables, key=lambda t: (round(t.bbox[1] / 5), t.bbox[0]))
        prev_bottom = 0
        for tb in tables:
            x0, top, x1, bottom = tb.bbox
            if top - prev_bottom > 2:
                above = chars_text([c for c in page.chars if _center_in(c, 0, prev_bottom, page.width, top)])
                if above.strip():
                    self._on_text(above, page, prev_bottom, top)
            prev_bottom = max(prev_bottom, bottom)
            self._on_table(Grid(page, tb), pno)

    def _on_text(self, text: str, page=None, top=None, bottom=None) -> None:
        t = jpdate.normalize(text)
        marks = [(m.start(), m.group(1)) for m in re.finditer(r"[(（]\s*(誤|正)\s*[)）]", t)]
        marks += [(m.start(), m.group(2).replace(" ", "")) for m in
                  re.finditer(r"(^|\n|\d\.)\s*(追\s*加|削\s*除)(?![・･\u30fb])", t)]
        if marks:
            last = sorted(marks)[-1][1]
            self.side = {"誤": "wrong", "正": "right", "追加": "added", "削除": "deleted"}[last]
            self._last = None
        ref = re.search(r"((?:令和|平成)\s*(?:元|\d+)\s*年\s*\d+\s*月\s*\d+\s*日)\s*付", t)
        if ref:
            d = jpdate.parse_first(ref.group(1))
            if d:
                self.ref_notice_date = d.isoformat()
        labels = re.findall(r"別表\s*([0-9]+)", t)
        if labels and len(labels) >= 1 and "のとおり" not in t:
            # 別表見出し（x 位置付きで記録）
            if page is not None:
                self._annex_labels = []
                for w in page.extract_words():
                    if top - 1 <= w["top"] <= bottom + 1:
                        m = re.match(r"^別表\s*([0-9]+)$", jpdate.normalize(w["text"]))
                        if m:
                            self._annex_labels.append((m.group(1), w["x0"]))
        sec = parse_heading(text, self.sec)
        if sec.heading != self.sec.heading or sec.setting != self.sec.setting:
            if sec.heading and sec.heading != self.sec.heading:
                self.headings.append(sec.heading)
            self._last = None
        self.sec = sec

    # ------------------------------------------------------------------
    def _on_table(self, g: Grid, pno: int) -> None:
        n = len(g.rows)
        hidx = None
        label = None
        for i in range(n):
            bt = jpdate.normalize(g.band_text(i)).replace(" ", "").replace("\n", "")
            m = re.fullmatch(r"別表([0-9]+)", bt)
            if m:
                label = m.group(1)
                continue
            if "承認番号" in bt:
                hidx = i
                self._annex_mode = None
                self._annex_carry = []
                break
            if "製品名" in bt and "製品コード" in bt:
                if bt.count("製品コード") >= 2:
                    self._on_annex(g, i)
                    return
                lab = label or (self._annex_labels[-1][0] if self._annex_labels else None)
                if lab:
                    self._on_annex_table(g, i, lab)
                    return
        if hidx is not None:
            for i in range(hidx):
                bt = g.band_text(i)
                if bt.strip():
                    # タイトル行はセル幅で切れることがあるのでページ幅で取り直す
                    b = g.bands[i]
                    full = chars_text([c for c in g.page.chars if _center_in(c, 0, b[0], g.page.width, b[1])])
                    self._on_text(full or bt)
            start = hidx + 1
            while start < n:
                raw = g.band_text(start)
                bt = jpdate.normalize(raw).replace(" ", "").replace("\n", "")
                if any(is_approval(tok) for tok in jpdate.normalize(raw).split()):
                    break
                if bt and (bt in ("(円)", "円") or "承認番号又は" in bt or "製品コード" in bt or "販売名" in bt):
                    start += 1
                    continue
                break
            cols = self._header_columns(g, hidx, start)
            if not cols:
                self.warnings.append(f"p{pno}: ヘッダー解釈失敗")
                return
            self.colkeys = [k for k, _, _ in cols]
        else:
            if getattr(self, "_annex_mode", None):
                self._on_annex_table(g, -1, self._annex_mode)
                return
            if not self.colkeys:
                return
            cols = self._columns_by_index(g)
            if not cols:
                self.warnings.append(f"p{pno}: ヘッダー無しの表で列対応が取れない")
                return
            start = 0
        self._parse_body(g, cols, start, pno)

    def _header_columns(self, g: Grid, hidx: int, end: int | None = None):
        end = end or hidx + 1
        spans: dict[tuple[int, int], list] = {}
        for i in range(hidx, end):
            for c in g.rows[i].cells:
                if c:
                    spans.setdefault((round(c[0]), round(c[2])), list(c))
        # 他の列を内包する幅広セル（タイトル等）は除外
        keys_ = sorted(spans)
        cells = [spans[k] for k in keys_ if not any(o != k and k[0] <= o[0] and o[1] <= k[1] for o in keys_)]
        top = g.bands[hidx][0]
        bot = g.bands[end - 1][1]
        cols = []
        for c in cells:
            txt = jpdate.normalize(g.text(c[0], top, c[2], bot)).replace("\n", "").replace(" ", "")
            key = None
            if "承認番号" in txt or "認証番号" in txt:
                key = "approval"
            elif "販売名" in txt and "製品" not in txt:
                key = "sales"
            elif "製品名" in txt and "製品コード" in txt:
                key = "product+code"
            elif "製品名" in txt:
                key = "product"
            elif "製品コード" in txt:
                key = "code"
            elif "希望者" in txt:
                key = "applicant"
            elif "機能区分" in txt or "医療機器の区分" in txt or "算定医療機器" in txt:
                key = "category"
            elif "償還価格" in txt or "価格" in txt:
                key = "price"
            else:
                key = f"x{len(cols)}"
            cols.append((key, c[0], c[2]))
        keys = [k for k, _, _ in cols]
        if "approval" not in keys or not any(k.startswith("product") for k in keys):
            return None
        return cols

    def _columns_by_index(self, g: Grid):
        best = max(g.rows, key=lambda r: sum(1 for c in r.cells if c))
        cells = sorted([c for c in best.cells if c], key=lambda c: c[0])
        if len(cells) != len(self.colkeys):
            return None
        return [(k, c[0], c[2]) for k, c in zip(self.colkeys, cells)]

    def _col_spans(self, g: Grid, x0, x1, rows: list[int], splits: set[int] | None):
        """列内の縦結合範囲: 行番号 -> (開始行, top, bottom)。"""
        out: dict[int, tuple[int, float, float]] = {}
        i = 0
        bottom_all = g.bbox[3]
        while i < len(rows):
            r = rows[i]
            cell = g.cell_in_col(r, x0, x1)
            if cell:
                j = i
                while j < len(rows) and g.bands[rows[j]] and (g.bands[rows[j]][0] + g.bands[rows[j]][1]) / 2 < cell[3]:
                    out[rows[j]] = (r, cell[1], cell[3])
                    j += 1
                i = max(j, i + 1)
                continue
            # セルが無い（罫線欠落）: 次のセル開始 or 分割点まで
            j = i + 1
            while j < len(rows) and not g.cell_in_col(rows[j], x0, x1) and not (splits and rows[j] in splits):
                j += 1
            top = g.bands[r][0]
            bot = g.bands[rows[j]][0] if j < len(rows) else bottom_all
            for k in range(i, j):
                out[rows[k]] = (r, top, bot)
            i = j
        return out

    def _parse_body(self, g: Grid, cols, start: int, pno: int) -> None:
        rows = [i for i in range(start, len(g.rows)) if g.bands[i]]
        if not rows:
            return
        colx = {k: (a, b) for k, a, b in cols}
        ax0, ax1 = colx["approval"]
        # 罫線欠落部の分割点: 承認番号らしき文字がある行
        splits = set()
        for r in rows:
            b = g.bands[r]
            if not g.cell_in_col(r, ax0, ax1):
                t = g.text(ax0, b[0], ax1, b[1])
                if is_approval(t.replace("\n", "")):
                    splits.add(r)
        spans = {k: self._col_spans(g, a, b, rows, splits) for k, (a, b) in colx.items()
                 if k in ("approval", "sales", "applicant", "category", "price")}
        cache: dict[tuple, str] = {}

        def span_text(key, r):
            sp = spans.get(key, {}).get(r)
            if not sp:
                return "", None
            ck = (key, sp)
            if ck not in cache:
                a, b = colx[key]
                cache[ck] = g.text(a, sp[1], b, sp[2])
            return cache[ck], sp[0]

        prod_key = "product+code" if "product+code" in colx else "product"
        for r in rows:
            b = g.bands[r]
            nt0 = jpdate.normalize(g.band_text(r)).replace(" ", "").replace("\n", "")
            if re.fullmatch(r"別表[0-9]+", nt0):
                self._pending_label = nt0[2:]
                continue
            if "製品名" in nt0 and "製品コード" in nt0 and "承認番号" not in nt0:
                lab = getattr(self, "_pending_label", None) or (self._annex_labels[-1][0] if self._annex_labels else None)
                if lab:
                    self._on_annex_table(g, r, lab)
                    self._pending_label = None
                    return
            appr_txt, appr_start = span_text("approval", r)
            appr_val = norm_approval(appr_txt.replace("\n", ""))
            vals = {}
            for k in ("sales", "applicant", "category", "price"):
                vals[k] = oneline(span_text(k, r)[0])
            if is_approval(appr_val):
                parent = {"approval_no": appr_val, "sales_name": vals["sales"], "applicant": vals["applicant"],
                          "category": vals["category"], "price_text": vals["price"]}
            elif not appr_txt.strip() and self._last is not None:
                parent = dict(self._last)
                if vals["category"]:
                    parent["category"], parent["price_text"] = vals["category"], vals["price"]
            else:
                rt = g.band_text(r).strip()
                nt = jpdate.normalize(rt).replace(" ", "")
                if not rt:
                    continue
                if (re.search(r"[(（](誤|正)[)）]", nt) or re.fullmatch(r"(\d\.)?(追加|削除|訂正.*)", nt)
                        or "区分" in nt or "保険適用" in nt or "承認番号" in nt or nt in ("(円)", "円")):
                    self._on_text(rt)
                elif re.fullmatch(r"別表[0-9]+", nt):
                    self._pending_label = nt[2:]
                elif "製品名" in nt and "製品コード" in nt and getattr(self, "_pending_label", None):
                    sub = Grid.__new__(Grid)
                    sub.__dict__.update(g.__dict__)
                    self._on_annex_table(g, r, self._pending_label)
                    self._pending_label = None
                    return
                else:
                    self.warnings.append(f"p{pno}: 承認番号を特定できない行 {rt[:60]!r}")
                continue
            self._last = parent
            # 製品
            px0, px1 = colx[prod_key]
            ptxt = g.text(px0, b[0], px1, b[1])
            if prod_key == "product" and "code" in colx:
                cx0, cx1 = colx["code"]
                ctxt = g.text(cx0, b[0], cx1, b[1])
                m = ANNEX_RE.search(jpdate.normalize(ptxt + ctxt))
                if m:
                    products, annex = [], jpdate.normalize(m.group(1))
                else:
                    codes = CODE_RE.findall(ctxt.replace("\n", ""))
                    products, annex = [(oneline(ptxt), c) for c in codes] or [(oneline(ptxt), None)], None
            else:
                products, annex = split_products(ptxt)
            self._emit(parent, products, annex, pno)

    def _emit(self, parent, products, annex, pno) -> None:
        price, unit = parse_price(parent["price_text"])
        cat = parent["category"]
        cm = re.match(r"^\s*(\d{3})", jpdate.normalize(cat))
        base = dict(doc_id=self.doc_id, page=pno, setting=self.sec.setting, action=self.sec.action,
                    kubun=self.sec.kubun, effective_date=self.sec.effective_date,
                    approval_no=parent["approval_no"], sales_name=parent["sales_name"],
                    applicant=parent["applicant"], category=cat, category_no=cm.group(1) if cm else None,
                    price_text=parent["price_text"], price=price, price_unit=unit, heading=self.sec.heading,
                    side=self.side, ref_notice_date=self.ref_notice_date)
        if annex:
            self.records.append(Record(product_name="", product_code=None, annex=annex, **base))
        for name, code in products:
            if not name and not code:
                continue
            self.records.append(Record(product_name=name, product_code=code, annex=None, **base))

    # ------------------------------------------------------------------
    def _annex_key(self, lab: str) -> str:
        return f"{self.side}:{lab}" if self.side else lab

    def _on_annex_table(self, g: Grid, hrow: int, lab: str) -> None:
        """縦1列の別表（訂正通知などで表の直後に置かれる）。hrow=-1 は前ページからの続き。"""
        ncol = ccol = None
        if hrow >= 0:
            cells = sorted([c for c in g.rows[hrow].cells if c], key=lambda c: c[0])
            for c in cells:
                t = jpdate.normalize(g.text(c[0], c[1], c[2], c[3])).replace(" ", "")
                if "製品名" in t and ncol is None:
                    ncol = (c[0], c[2])
                elif "製品コード" in t and ccol is None:
                    ccol = (c[0], c[2])
        else:
            best = max(g.rows, key=lambda r: sum(1 for c in r.cells if c))
            cells = sorted([c for c in best.cells if c], key=lambda c: c[0])
            if len(cells) == 2:
                ncol, ccol = (cells[0][0], cells[0][2]), (cells[1][0], cells[1][2])
            elif len(cells) == 1:
                ccol = (cells[0][0], cells[0][2])
                ncol = (cells[0][0], cells[0][0])
        if not ncol or not ccol:
            return
        self._annex_mode = lab
        rows = [i for i in range(hrow + 1, len(g.rows)) if g.bands[i]]
        spans = self._col_spans(g, ncol[0], ncol[1], rows, None)
        key = self._annex_key(lab)
        for r in rows:
            b = g.bands[r]
            sp = spans.get(r)
            name = oneline(g.text(ncol[0], sp[1], ncol[1], sp[2])) if sp else ""
            for c in CODE_RE.findall(g.text(ccol[0], b[0], ccol[1], b[1]).replace("\n", "")):
                self.annex.setdefault(key, []).append((name, c))

    def _on_annex(self, g: Grid, hrow: int) -> None:
        # 表検出経由で別表に当たった場合（データ表と同じページ）: その表の上端から下をページ単位で解析
        if getattr(self, "_annex_page_done", None) == id(g.page):
            return
        self._parse_annex_page(g.page, 0, top=max(0, g.bbox[1] - 40))

    def _parse_annex_page(self, page, pno: int, top: float = 0) -> None:
        """別表を単語座標で読む。

        見出し行（「製品名」「製品コード」…）ごとに列グループを作り、製品コード（8〜14桁の数字）は
        x座標が最も近い「製品コード」見出しの列グループに、直前の文字列を製品名として割り当てる。
        「別表N」はその見出し行の上にある見出しから、無ければ前ページの並び順を引き継ぐ。
        """
        self._annex_page_done = id(page)
        words = [w for w in page.extract_words(x_tolerance=1.5) if w["top"] >= top]
        for w in words:
            w["n"] = jpdate.normalize(w["text"])
        labels = [w for w in words if re.match(r"^別表\s*[0-9]+$", w["n"])]
        hdrs = [w for w in words if w["n"] in ("製品名", "製品コード", "製品名称")]
        hrows: list[list[dict]] = []
        for w in sorted(hdrs, key=lambda w: (w["top"], w["x0"])):
            if hrows and abs(hrows[-1][0]["top"] - w["top"]) < 3:
                hrows[-1].append(w)
            else:
                hrows.append([w])
        if not hrows:
            return
        carry = getattr(self, "_annex_carry", [])
        lines: list[list[dict]] = []
        body = [w for w in words if w not in hdrs and w not in labels]
        for w in sorted(body, key=lambda w: (w["top"], w["x0"])):
            if lines and abs(lines[-1][0]["top"] - w["top"]) < 3:
                lines[-1].append(w)
            else:
                lines.append([w])
        prev_top = top
        for ri, hr in enumerate(hrows):
            r_top = hr[0]["top"]
            r_end = hrows[ri + 1][0]["top"] - 1 if ri + 1 < len(hrows) else page.height + 1
            names = sorted([w for w in hr if w["n"] != "製品コード"], key=lambda w: w["x0"])
            codes_h = sorted([w for w in hr if w["n"] == "製品コード"], key=lambda w: w["x0"])
            groups = []
            for k, n in enumerate(names):
                nx = names[k + 1]["x0"] if k + 1 < len(names) else page.width + 1
                chs = [c for c in codes_h if n["x0"] < c["x0"] < nx]
                labs = [l for l in labels if prev_top - 1 <= l["top"] < r_top and l["x0"] <= n["x0"] + 5]
                lab = max(labs, key=lambda l: l["x0"])["n"][2:].strip() if labs else None
                if lab is None and k < len(carry):
                    lab = carry[k]
                groups.append({"lab": lab, "nx": (n["x0"] + n["x1"]) / 2,
                               "cx": [(c["x0"] + c["x1"]) / 2 for c in chs], "last": None})
            prev_top = r_top
            if not groups:
                continue
            allc = [(cx, g) for g in groups for cx in g["cx"]]
            if not allc:
                continue
            for ln in lines:
                if not (r_top + 2 < ln[0]["top"] < r_end):
                    continue
                acc: list[str] = []
                has_code = False
                for w in sorted(ln, key=lambda w: w["x0"]):
                    if re.fullmatch(r"\d{8,14}", w["n"]):
                        has_code = True
                        cxw = (w["x0"] + w["x1"]) / 2
                        g = min(allc, key=lambda t: abs(t[0] - cxw))[1]
                        nm = oneline(" ".join(acc)) or (g["last"] or "")
                        acc = []
                        if g["lab"]:
                            key = self._annex_key(g["lab"])
                            self.annex.setdefault(key, []).append((nm, w["n"]))
                            g["last"] = nm
                    else:
                        acc.append(w["text"])
                if not has_code and acc:
                    pass  # 製品名の折り返し行（コード無し）は無視
            self._annex_carry = [g["lab"] for g in groups if g["lab"]]

    def _expand_annex(self) -> None:
        out = []
        for r in self.records:
            if r.annex:
                items = self.annex.get(f"{r.side}:{r.annex}") if r.side else None
                items = items or self.annex.get(r.annex)
                if not items:
                    self.warnings.append(f"別表{r.annex} が見つからない（{r.approval_no}）")
                    out.append(r)
                    continue
                for name, code in items:
                    d = r.as_dict()
                    d.update(product_name=name, product_code=code)
                    out.append(Record(**d))
            else:
                out.append(r)
        self.records = out


def has_text(path: str, pages: int = 3) -> bool:
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages[:pages]:
            if len(p.chars) > 20:
                return True
    return False


def parse_notice(path: str, doc_id: str, default_effective: str | None = None) -> NoticeParser:
    return NoticeParser(doc_id, default_effective).parse(path)

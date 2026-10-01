"""スキャン画像の通知PDF（平成20〜28年度）を OCR で読む。

1. ページ画像を取り出し、向きを補正（文字行の向き＋数字OCRの確信度）
2. 罫線（横線・縦線）を検出して表と列を復元
3. 表の上の見出しを OCR して区分（B/C/A2…）・新規/追加・適用日・訂正の（誤）（正）を判定
4. B・C区分の表（6列: 承認番号/販売名/製品名・製品コード/希望者/決定機能区分/償還価格）だけ、
   セルごとに OCR。承認番号・製品コード・価格は英数字モデル＋文字種制限で読み、形式チェックで弾く
5. 別表（「別表N」＋ 製品名/製品コード の2列表）も読む

出力は parse_notice.Record と同じ形（ocr=True 相当の印は呼び出し側で付ける）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np
import pypdfium2 as pdfium

from . import jpdate
from .parse_notice import Record, Section, gtin_ok, norm_approval, parse_heading
from .tess import PSM_SINGLE_BLOCK, PSM_SINGLE_LINE, PSM_SPARSE, Tess

TARGET_LONG = 3500  # ページ長辺をこの画素数に揃える（文字高 18〜22px 程度）
APPROVAL_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
APPROVAL_RE = re.compile(r"^(?:\d{5}[A-Z]{3}\d{8}|\d{3}[A-Z]{5}\d{5}[0-9A-Z]\d{2}|\d{2}[A-Z0-9]{2}[A-Z0-9]{12}|[0-9A-Z]{16})$")


class Engines:
    def __init__(self):
        self.appr = Tess("eng", psm=PSM_SINGLE_LINE, whitelist=APPROVAL_CHARS)
        self.digits = Tess("eng", psm=PSM_SINGLE_BLOCK, whitelist="0123456789")
        self.digits_sparse = Tess("eng", psm=PSM_SPARSE, whitelist="0123456789")
        self.price = Tess("eng", psm=PSM_SINGLE_LINE, whitelist="0123456789,.")
        self.jpn = Tess("jpn_fast" if _has("jpn_fast") else "jpn", psm=PSM_SINGLE_BLOCK)

    def close(self):
        for t in (self.appr, self.digits, self.digits_sparse, self.price, self.jpn):
            t.close()


def _has(lang):
    from pathlib import Path
    from .tess import default_datapath
    return Path(default_datapath(), f"{lang}.traineddata").exists()


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------
def page_image(pdf: pdfium.PdfDocument, i: int) -> np.ndarray | None:
    pg = pdf[i]
    imgs = [o for o in pg.get_objects() if type(o).__name__ == "PdfImage"]
    if len(imgs) == 1:
        try:
            pil = imgs[0].get_bitmap(render=False).to_pil().convert("L")
            return np.array(pil)
        except Exception:  # noqa: BLE001
            pass
    pil = pg.render(scale=200 / 72).to_pil().convert("L")
    return np.array(pil)


def normalize(gray: np.ndarray) -> np.ndarray:
    s = TARGET_LONG / max(gray.shape)
    if abs(s - 1) < 0.05:
        return gray
    return cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)


def _binarize(gray):
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)


def _lines(bw):
    H, W = bw.shape
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, W // 45), 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(40, H // 50))))
    return hl, vl


def text_axis(gray) -> str:
    s = 1600 / max(gray.shape)
    im = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(im, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 60)))
    txt = cv2.subtract(bw, cv2.bitwise_or(hl, vl))
    txt = cv2.morphologyEx(txt, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnt = {}
    for name, k in (("h", (9, 1)), ("v", (1, 9))):
        d = cv2.dilate(txt, cv2.getStructuringElement(cv2.MORPH_RECT, k))
        _, _, st, _ = cv2.connectedComponentsWithStats(d)
        w, h = st[1:, 2], st[1:, 3]
        cnt[name] = int(((w > 5 * h) & (w > 40)).sum()) if name == "h" else int(((h > 5 * w) & (h > 40)).sum())
    return "h" if cnt["h"] >= cnt["v"] else "v"


def deskew(gray: np.ndarray, max_deg: float = 3.0) -> tuple[np.ndarray, float]:
    """スキャンの傾き（数度以内）を罫線・文字行の投影プロファイルが最も鋭くなる角度で補正する。"""
    s = 1000 / max(gray.shape)
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = bw.shape
    best, best_a = -1.0, 0.0
    for a in np.arange(-max_deg, max_deg + 0.01, 0.25):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        r = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = r.sum(axis=1).astype(np.float64)
        sc = float(np.var(prof))
        if sc > best:
            best, best_a = sc, float(a)
    # 0.25度刻みの最良点の周りを細かく
    for a in np.arange(best_a - 0.2, best_a + 0.21, 0.05):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        r = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        sc = float(np.var(r.sum(axis=1).astype(np.float64)))
        if sc > best:
            best, best_a = sc, float(a)
    if abs(best_a) < 0.1:
        return gray, 0.0
    H, W = gray.shape
    M = cv2.getRotationMatrix2D((W / 2, H / 2), best_a, 1.0)
    return cv2.warpAffine(gray, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=255), best_a


ROT = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _rot(gray, d):
    return gray if ROT[d] is None else cv2.rotate(gray, ROT[d])


def orient(gray: np.ndarray, eng: Engines, hint: int | None = None) -> tuple[np.ndarray, int]:
    """向きを判定して正立させる。hint（同じ文書の直前ページの向き）を先に試す。"""
    axis = text_axis(gray)
    cands = [0, 180] if axis == "h" else [90, 270]
    if hint in cands:
        cands.remove(hint)
        cands.insert(0, hint)

    def score(d):
        im = _rot(gray, d)
        s = 1800 / max(im.shape)
        im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        H, W = im.shape
        crop = im[int(H * 0.08):int(H * 0.6), int(W * 0.05):int(W * 0.6)]
        words = eng.digits_sparse.tsv(crop)
        good = [w for w in words if len(w["text"]) >= 6 and w["conf"] > 50]
        return len(good) * 10 + (np.mean([w["conf"] for w in words]) if words else 0)

    s0 = score(cands[0])
    if s0 >= 60:
        return _rot(gray, cands[0]), cands[0]
    s1 = score(cands[1])
    d = cands[0] if s0 >= s1 else cands[1]
    return _rot(gray, d), d


# ---------------------------------------------------------------------------
# 表
# ---------------------------------------------------------------------------
@dataclass
class Table:
    x0: int
    y0: int
    x1: int
    y1: int
    xs: list[int]                  # 縦線（列境界）
    seps: list[list[int]]          # 列ごとの横線 y


def _cluster(vals, gap=6):
    out, cur = [], []
    for v in sorted(vals):
        if cur and v - cur[-1] > gap:
            out.append(int(np.mean(cur)))
            cur = []
        cur.append(v)
    if cur:
        out.append(int(np.mean(cur)))
    return out


def find_tables(gray) -> list[Table]:
    bw = _binarize(gray)
    hl, vl = _lines(bw)
    grid = cv2.dilate(cv2.bitwise_or(hl, vl), np.ones((5, 5), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(grid)
    H, W = gray.shape
    tables = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if w < W * 0.15 or h < 60:
            continue
        sub_v = cv2.dilate(vl[y:y + h, x:x + w], cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)))
        colsum = (sub_v > 0).sum(axis=0)
        xs = _cluster([x + j for j in np.where(colsum > h * 0.33)[0]], gap=12)
        if len(xs) < 3:
            continue
        sub_h = hl[y:y + h, x:x + w]
        seps = []
        for a, b in zip(xs, xs[1:]):
            seg = sub_h[:, max(0, a - x + 8):max(1, b - x - 8)]
            seg = cv2.dilate(seg, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))) if seg.size else seg
            if seg.shape[1] <= 0:
                seps.append([])
                continue
            cov = (seg > 0).sum(axis=1)
            ys = _cluster([y + j for j in np.where(cov > seg.shape[1] * 0.6)[0]])
            seps.append(ys)
        tables.append(Table(x, y, x + w, y + h, xs, seps))
    tables.sort(key=lambda t: (t.y0 // 40, t.x0))
    return tables


def _looks_flipped(tables: list[Table]) -> bool:
    """表の構造から上下逆さを判定する。

    製品名・製品コード列は行の区切り（横線）が最も多い。6列表なら左から3列目、7列表なら3〜4列目にあるはずで、
    右から3列目にあれば逆さ。判定できないときは、価格欄（細い列）が左端だけにあるかで見る。
    """
    votes = 0
    for t in tables:
        n = len(t.xs) - 1
        w = max(1, t.xs[-1] - t.xs[0])
        cnt = [len(x) for x in t.seps]
        if n >= 5 and max(cnt) >= 5:
            top = max(range(n), key=lambda i: cnt[i])
            if cnt.count(cnt[top]) == 1:
                if top == 2 or (n == 7 and top == 3):
                    votes -= 1
                    continue
                if top == n - 3 or (n == 7 and top == 4):
                    votes += 1
                    continue
        first = (t.xs[1] - t.xs[0]) / w
        last = (t.xs[-1] - t.xs[-2]) / w
        if n >= 5:
            if first < 0.12 <= last:
                votes += 1
            elif last < 0.12 <= first:
                votes -= 1
        elif n == 2:
            if first < last * 0.8:
                votes += 1
            elif last < first * 0.8:
                votes -= 1
    return votes > 0


def _ink(img) -> int:
    if img.size == 0:
        return 0
    return int((img < 128).sum())


def _cell(gray, x0, y0, x1, y1, pad=6):
    return gray[max(0, y0 + pad):max(0, y1 - pad), max(0, x0 + pad):max(0, x1 - pad)]


def _up(img, f):
    if f == 1 or img.size == 0:
        return img
    return cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def _clean(img, first_line: bool = False):
    """二値化し、セル内に残った罫線・点ノイズを除いて文字部分だけに切り詰める（白背景・黒文字）。

    first_line=True なら最初の1行だけを返す（承認番号・価格など、結合セルの上端に1行だけ書かれる項目）。
    戻り値が空配列なら文字なし。
    """
    if img.size == 0:
        return img
    inv = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = inv.shape
    lines = cv2.bitwise_or(
        cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, w // 3), 1))),
        cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, int(h * 0.8))))))
    inv = cv2.subtract(inv, lines)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    if n <= 1:
        return np.zeros((0, 0), np.uint8)
    keep = np.zeros(n, bool)
    keep[1:] = st[1:, 4] >= 8
    inv[~keep[lab]] = 0
    rows = np.where(inv.sum(axis=1) > 0)[0]
    cols = np.where(inv.sum(axis=0) > 0)[0]
    if rows.size == 0:
        return np.zeros((0, 0), np.uint8)
    y0, y1 = rows[0], rows[-1] + 1
    if first_line:
        # 最初の文字行: 空白行が 6px 以上続くところまで
        prof = inv[y0:y1].sum(axis=1) > 0
        end, gap = len(prof), 0
        for i, v in enumerate(prof):
            gap = 0 if v else gap + 1
            if gap >= 6 and i > 8:
                end = i - gap + 1
                break
        y1 = y0 + end
        cols = np.where(inv[y0:y1].sum(axis=0) > 0)[0]
        if cols.size == 0:
            return np.zeros((0, 0), np.uint8)
    x0, x1 = cols[0], cols[-1] + 1
    out = 255 - inv[y0:y1, x0:x1]
    return cv2.copyMakeBorder(out, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


def _auto(c, target: float = 27.0, lo: float = 1.0, hi: float = 4.0):
    """文字の高さが target px 前後になるよう拡大する（小さく印刷された頁への対策）。"""
    if c.size == 0:
        return c
    inv = (c < 128).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    hs = st[1:, 3]
    hs = hs[(hs >= 5) & (hs < c.shape[0])]
    if hs.size == 0:
        return c
    med = float(np.percentile(hs, 75))
    f = float(np.clip(target / max(med, 1.0), lo, hi))
    return _up(c, f) if f > 1.05 else c


# ---------------------------------------------------------------------------
# 値の読み取り
# ---------------------------------------------------------------------------
_APPR_FIX = str.maketrans({"O": "0", "I": "1", "S": "5", "B": "8", "G": "6", "Z": "2", "Q": "0", "D": "0"})


def read_approval(eng: Engines, img) -> tuple[str, float]:
    if _ink(img) < 30:
        return "", 0.0
    c = _clean(img, first_line=True)
    if c.size == 0:
        return "", 0.0
    t = eng.appr.text(_auto(c, 30)).strip().replace(" ", "")
    t = re.sub(r"[^0-9A-Z]", "", t.upper())
    conf = eng.appr.conf()
    if len(t) != 16 or conf < 80:
        t2 = re.sub(r"[^0-9A-Z]", "", eng.appr.text(_auto(c, 40)).strip().upper())
        c2 = eng.appr.conf()
        if (len(t2) == 16 and len(t) != 16) or (len(t2) == len(t) and c2 > conf):
            t, conf = t2, c2
    return fix_approval(t), conf


def fix_approval(t: str) -> str:
    """承認番号の文字種の位置規則で誤読を補正する。

    - 承認番号: 5桁数字 + BZ? + 5桁数字 + 枝番3字（例 21800BZX10056000, 20600BZZ00666A01）
    - 認証番号: 3桁数字 + A?BZ? + 5桁数字 + 3字（例 220ADBZX00121000）
    - 届出番号など上記以外はそのまま
    """
    if len(t) != 16:
        return t
    dig = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "T": "1", "S": "5", "B": "8",
                         "G": "6", "Z": "2", "A": "4"})
    let = str.maketrans({"8": "B", "2": "Z", "0": "O", "1": "I", "5": "S", "6": "G", "4": "A"})
    if t[3] in "A4" and (t[4].isalpha() or t[4] in "84"):
        tail3 = t[13:16]
        tail3 = (tail3[0] + tail3[1:].translate(dig) if tail3[0].isalpha() and tail3[0] not in "ODQ"
                 else tail3.translate(dig))
        return t[:3].translate(dig) + t[3:8].translate(let) + t[8:13].translate(dig) + tail3
    if t[5:7] in ("BZ", "8Z", "B2", "82") or (t[5] in "B8" and t[7] in "XZYGI2"):
        tail3 = t[13:16]
        tail3 = (tail3[0] + tail3[1:].translate(dig) if tail3[0].isalpha() and tail3[0] not in "ODQ"
                 else tail3.translate(dig))
        return t[:5].translate(dig) + ("BZ" + t[7].translate(let)) + t[8:13].translate(dig) + tail3
    return t


def read_codes(eng: Engines, img) -> list[str]:
    if _ink(img) < 30:
        return []
    c = _clean(img)
    if c.size == 0:
        return []
    t = eng.digits.text(_auto(c, 28))
    out = []
    for tok in re.findall(r"\d{8,14}", t.replace(" ", "")):
        out.append(tok)  # チェックデジットの検証・補正は DB 構築時に既知コードと照合して行う
    return out


def read_digits_all(eng: Engines, img) -> list[str]:
    if _ink(img) < 30:
        return []
    c = _clean(img)
    if c.size == 0:
        return []
    t = eng.digits.text(_auto(c, 28))
    return re.findall(r"\d{8,14}", t.replace(" ", ""))


def read_price(eng: Engines, img) -> tuple[float | None, str]:
    if _ink(img) < 20:
        return None, ""
    c = _clean(img, first_line=True)
    if c.size == 0:
        return None, ""
    t = eng.price.text(_auto(c, 30)).strip()
    nums = re.findall(r"\d[\d,]*", t)
    if not nums:
        return None, t
    v = nums[-1].replace(",", "")
    try:
        return float(v), t
    except ValueError:
        return None, t


def read_jpn(eng: Engines, img, scale=1.3) -> str:
    if _ink(img) < 30:
        return ""
    c = _clean(img)
    if c.size == 0:
        return ""
    t = eng.jpn.text(_auto(c, 30 * scale / 1.3))
    lines = [re.sub(r"\s+", " ", l).strip(" |") for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def read_kubun_no(eng: Engines, img) -> str | None:
    """決定機能区分セルの先頭3桁（材料価格基準の区分番号）。"""
    if _ink(img) < 30:
        return None
    c = _clean(img, first_line=True)
    if c.size == 0:
        return None
    first = c[:, : max(40, int(c.shape[1] * 0.16))]
    t = eng.digits.text(_auto(first, 32))
    m = re.search(r"\d{3}", t.replace(" ", ""))
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# 文書の解析
# ---------------------------------------------------------------------------
class OcrNoticeParser:
    def __init__(self, doc_id: str, default_effective: str | None, eng: Engines, log=None):
        self.doc_id = doc_id
        self.eng = eng
        self.sec = Section(effective_date=default_effective)
        self.side = None
        self.ref_notice_date = None
        self.records: list[Record] = []
        self.annex: dict[str, list[tuple[str, str]]] = {}
        self.headings: list[str] = []
        self.warnings: list[str] = []
        self.log = log
        self._rot = None
        self._last = None
        self.stats = {"pages": 0, "tables_bc": 0, "tables_skip": 0, "cells": 0}

    def _on_text(self, text: str):
        t = jpdate.normalize(text)
        marks = [(m.start(), m.group(1)) for m in re.finditer(r"[(（]\s*(誤|正)\s*[)）]", t)]
        marks += [(m.start(), m.group(2).replace(" ", "")) for m in
                  re.finditer(r"(^|\n|\d\.)\s*(追\s*加|削\s*除)(?![・･])", t)]
        if marks:
            self.side = {"誤": "wrong", "正": "right", "追加": "added", "削除": "deleted"}[sorted(marks)[-1][1]]
            self._last = None
        ref = re.search(r"((?:平成|令和)\s*(?:元|\d+)\s*年\s*\d+\s*月\s*\d+\s*日)\s*付", t)
        if ref:
            d = jpdate.parse_first(ref.group(1))
            if d:
                self.ref_notice_date = d.isoformat()
        text = re.sub(r"区分\s*[日8]", "区分B", text)
        sec = parse_heading(text, self.sec)
        # OCR では「区分Ｂ」の判定が揺れるので、価格欄の有無（列数）でも補う
        if sec.heading != self.sec.heading:
            self.headings.append(sec.heading)
            self._last = None
        self.sec = sec

    def parse(self, path: str, max_pages: int | None = None) -> "OcrNoticeParser":
        pdf = pdfium.PdfDocument(path)
        n = len(pdf) if max_pages is None else min(len(pdf), max_pages)
        for i in range(n):
            try:
                self._page(pdf, i)
            except Exception as e:  # noqa: BLE001
                self.warnings.append(f"p{i + 1}: OCR失敗 {e!r}")
        self._expand_annex()
        return self

    def _page(self, pdf, i):
        gray = page_image(pdf, i)
        if gray is None:
            return
        gray = normalize(gray)
        # 向き: 文字行の軸（縦横）は画像処理で判定し、上下（0/180, 90/270）は表の構造で判定する。
        # 表が無い頁だけ数字OCRの確信度で判定する（遅いので）。
        axis = text_axis(gray)
        cand = (self._rot if self._rot in (0, 180) else 0) if axis == "h" else (self._rot if self._rot in (90, 270) else 270)
        g1, _ = deskew(_rot(gray, cand))
        tables = find_tables(g1)
        if tables:
            gray, self._rot = g1, cand
        else:
            gray, self._rot = orient(gray, self.eng, self._rot)
            gray, _ = deskew(gray)
            tables = find_tables(gray)
        self.stats["pages"] += 1
        if _looks_flipped(tables):
            # 価格欄（細い列）が左端にある＝上下逆。180度回して取り直す
            gray = cv2.rotate(gray, cv2.ROTATE_180)
            self._rot = (self._rot + 180) % 360
            tables = find_tables(gray)
        H, W = gray.shape
        if not tables:
            # 鑑（表紙）: 訂正の対象通知日などを拾う
            if i <= 1:
                self._on_text(read_jpn(self.eng, gray[: int(H * 0.7)], 1.0))
            return
        prev_bottom = 0
        for tb in tables:
            if tb.y0 - prev_bottom > 25:
                band = gray[prev_bottom:tb.y0, max(0, tb.x0 - 20):min(W, tb.x1 + 20)]
                band = band[max(0, band.shape[0] - 260):]  # 表の直上 260px（見出し2〜3行分）
                txt = read_jpn(self.eng, band, 1.0)
                if txt:
                    self._on_text(txt)
            prev_bottom = max(prev_bottom, tb.y1)
            self._table(gray, tb, i + 1)

    # -- 表 -----------------------------------------------------------------
    def _table(self, gray, tb: Table, pno: int):
        ncol = len(tb.xs) - 1
        if ncol == 2:
            self._annex_table(gray, tb)
            return
        width = tb.xs[-1] - tb.xs[0]
        has_price = (tb.xs[-1] - tb.xs[-2]) / max(1, width) < 0.12
        if not has_price or ncol not in (6, 7):  # A2 など価格欄のない表は対象外
            self.stats["tables_skip"] += 1
            return
        self.stats["tables_bc"] += 1
        xs = tb.xs
        # 列の役割: 7列 = 承認番号/販売名/製品名/製品コード/希望者/機能区分/価格（平成20年代前半）
        #           6列 = 承認番号/販売名/製品名・製品コード/希望者/機能区分/価格
        if ncol == 7:
            C = {"appr": 0, "sales": 1, "pname": 2, "pcode": 3, "appl": 4, "cat": 5, "price": 6}
        else:
            C = {"appr": 0, "sales": 1, "pname": 2, "pcode": None, "appl": 3, "cat": 4, "price": 5}
        rows_y = tb.seps[C["pname"]]
        if len(rows_y) < 2:
            return
        cells = list(zip(rows_y, rows_y[1:]))

        def span(col, yc):
            ys = tb.seps[col]
            for a, b in zip(ys, ys[1:]):
                if a - 2 <= yc <= b + 2:
                    return a, b
            return None
        cache = {}

        def read(col, a, b, kind):
            k = (col, a, b, kind)
            if k in cache:
                return cache[k]
            img = _cell(gray, xs[col], a, xs[col + 1], b)
            self.stats["cells"] += 1
            if kind == "appr":
                v = read_approval(self.eng, img)
            elif kind == "price":
                v = read_price(self.eng, img)
            elif kind == "cat":
                v = (read_jpn(self.eng, img, 1.4), read_kubun_no(self.eng, img))
            else:
                v = read_jpn(self.eng, img, 1.3)
            cache[k] = v
            return v

        for ri, (a, b) in enumerate(cells):
            yc = (a + b) // 2
            sp0 = span(C["appr"], yc)
            if not sp0:
                continue
            appr, aconf = read(C["appr"], *sp0, "appr")
            if ri == 0 and not re.search(r"\d{3}", appr or ""):
                continue  # ヘッダー
            if appr and len(appr) >= 14:
                parent = {"approval_no": norm_approval(appr), "approval_conf": aconf}
                for key, role in (("sales_name", "sales"), ("applicant", "appl")):
                    sp = span(C[role], yc)
                    parent[key] = read(C[role], *sp, "jpn").replace("\n", "") if sp else ""
                sp = span(C["cat"], yc)
                cat, kno = read(C["cat"], *sp, "cat") if sp else ("", None)
                sp = span(C["price"], yc)
                price, ptxt = read(C["price"], *sp, "price") if sp else (None, "")
                parent.update(category=cat.replace("\n", " "), category_no=kno, price=price, price_text=ptxt)
                self._last = parent
            elif not appr and self._last is not None:
                parent = self._last
            else:
                if appr:
                    self.warnings.append(f"p{pno}: 承認番号の読取不良 {appr!r}")
                continue
            px0, px1 = xs[C["pname"]], xs[C["pname"] + 1]
            pimg = _cell(gray, px0, a, px1, b)
            # 製品名の OCR は時間がかかるので、製品コードが読めなかったセル（「別表Nのとおり」など）だけ行う。
            # 製品名は補正時に JAN から辞書（テキスト期の通知・MEDIS）で補う。
            if C["pcode"] is not None:
                codes = read_codes(self.eng, _cell(gray, xs[C["pcode"]], a, xs[C["pcode"] + 1], b))
                name = "" if codes else read_jpn(self.eng, pimg, 1.3)
            else:
                wcode = int((px1 - px0) * 0.32)
                codes = read_codes(self.eng, pimg[:, max(0, pimg.shape[1] - wcode - 10):])
                name = "" if codes else read_jpn(self.eng, pimg[:, : pimg.shape[1] - wcode + 10], 1.3)
            nm = jpdate.normalize(name)
            annex = None
            if not codes:
                m = re.search(r"(\d{1,3})\s*[のの]\s*[と0-9]", nm) or re.search(r"別\s*表\s*(\d{1,3})", nm)
                if m and ("のと" in nm or "とお" in nm or "とぬ" in nm or "通り" in nm or "別表" in nm):
                    annex = m.group(1)
            self._emit(parent, name.split("\n")[0] if name else "", codes, annex, pno)

    def _emit(self, parent, name, codes, annex, pno):
        cat = parent.get("category") or ""
        kub = self.sec.kubun if self.sec.kubun and re.match(r"^(B|C|R)", self.sec.kubun) else "B"
        kno = parent.get("category_no") or (re.match(r"^\s*(\d{3})", jpdate.normalize(cat)) or [None, None])[1]
        base = dict(doc_id=self.doc_id, page=pno, setting=self.sec.setting, action=self.sec.action,
                    kubun=kub, effective_date=self.sec.effective_date,
                    approval_no=parent["approval_no"], sales_name=parent.get("sales_name", ""),
                    applicant=parent.get("applicant", ""), category=cat, category_no=kno,
                    price_text=parent.get("price_text", ""), price=parent.get("price"), price_unit=None,
                    heading=self.sec.heading, side=self.side, ref_notice_date=self.ref_notice_date)
        if annex:
            self.records.append(Record(product_name="", product_code=None, annex=annex, **base))
            return
        if not codes:
            self.records.append(Record(product_name=name, product_code=None, annex=None, **base))
        for c in codes:
            self.records.append(Record(product_name=name, product_code=c, annex=None, **base))

    # -- 別表 ---------------------------------------------------------------
    def _annex_table(self, gray, tb: Table):
        H, W = gray.shape
        lab_img = gray[max(0, tb.y0 - 70):tb.y0 + 4, max(0, tb.x0 - 10):min(W, tb.x0 + 260)]
        t = jpdate.normalize(read_jpn(self.eng, lab_img, 1.4))
        m = re.search(r"別\s*表\s*(\d+)", t)
        lab = m.group(1) if m else getattr(self, "_annex_last", None)
        if not lab:
            return
        self._annex_last = lab
        x0, x1 = tb.xs[1], tb.xs[2]
        col = gray[tb.y0 + 4:tb.y1 - 4, x0 + 4:x1 - 4]
        names_img = gray[tb.y0 + 4:tb.y1 - 4, tb.xs[0] + 4:x0 - 4]
        codes = read_codes(self.eng, col)
        key = f"{self.side}:{lab}" if self.side else lab
        # 製品名は行ごとの対応付けが難しいので、先頭の製品名を代表名として使う
        nm = read_jpn(self.eng, names_img[: min(names_img.shape[0], 120)], 1.3).split("\n")
        rep = next((x for x in nm if x and "製品" not in x), "")
        for c in codes:
            self.annex.setdefault(key, []).append((rep, c))

    def _expand_annex(self):
        out = []
        for r in self.records:
            if r.annex:
                items = (self.annex.get(f"{r.side}:{r.annex}") if r.side else None) or self.annex.get(r.annex)
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

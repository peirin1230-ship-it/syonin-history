"""MEDIS 医療機器データベースのダウンロードファイル（YYYYMMDD_SB_DlAll.lzh / .txt）を取り込む。

1行 = JANコード。償還情報は（医科）（在宅）（調剤）ごとに「特定保険医療材料名称01〜36」の
名称・償還価格・告示日・医事コード（= 特定器材コード9桁）を持つ。
ここでは償還情報のある行だけを、JAN × 材料（スロット）単位の縦持ちに展開して保存する。

※ MEDIS データは利用規約により再配布が制限されている可能性があるため、取り込んだデータは
  リポジトリに含めない（data/medis/ は .gitignore 対象）。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
from pathlib import Path

SETTINGS = ("医科", "在宅", "調剤")

COLS = {
    "jan": "ＪＡＮコード",
    "product_name": "商品名",
    "sales_name": "薬事申請書上の販売名",
    "spec": "規格",
    "product_no": "製品番号",
    "approval_no": "薬事法承認（認証）番号または届出番号",
    "maker": "製造販売業者：企業略称名",
    "registrant": "データ登録企業　略称名",
    "jmdn": "ＪＭＤＮコード",
    "generic_name": "一般的名称",
    "end_date": "販売終了日",
    "updated": "最終更新日",
    "claim_kubun": "材料償還請求区分",
    "input_kubun": "特材入力区分",
    "new_jan": "新ＪＡＮコード",
    "old_jan": "旧ＪＡＮコード",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS medis_snapshots(
  snapshot TEXT PRIMARY KEY, source_file TEXT, n_rows INTEGER, n_items INTEGER);
CREATE TABLE IF NOT EXISTS medis_items(
  snapshot TEXT, jan TEXT, approval_no TEXT, sales_name TEXT, product_name TEXT, spec TEXT, product_no TEXT,
  maker TEXT, end_date TEXT, updated TEXT, claim_kubun TEXT, setting TEXT, slot INTEGER,
  material_name TEXT, material_short TEXT, price REAL, notice_date TEXT, receipt_code TEXT, receipt_name TEXT);
CREATE INDEX IF NOT EXISTS ix_medis_appr ON medis_items(approval_no, snapshot);
CREATE INDEX IF NOT EXISTS ix_medis_jan ON medis_items(jan, snapshot);
CREATE INDEX IF NOT EXISTS ix_medis_code ON medis_items(receipt_code, snapshot);
CREATE TABLE IF NOT EXISTS medis_dict(
  snapshot TEXT, jan TEXT, approval_no TEXT, sales_name TEXT, maker TEXT, product_name TEXT);
"""


def _open_text(path: Path):
    """.txt（CSV）または .lzh を開いて文字列ストリームを返す。"""
    if path.suffix.lower() in (".lzh", ".lha"):
        try:
            import lhafile  # type: ignore
        except ImportError as e:
            raise SystemExit("LZHの展開には `pip install lhafile` が必要です（または展開済みの .txt を指定）") from e
        f = lhafile.Lhafile(str(path))
        names = [i.filename for i in f.infolist() if i.filename.lower().endswith((".txt", ".csv"))]
        if not names:
            raise SystemExit("LZH内にtxt/csvが見つかりません")
        data = f.read(names[0])
        return io.TextIOWrapper(io.BytesIO(data), encoding="cp932", errors="replace", newline="")
    return open(path, encoding="cp932", errors="replace", newline="")


def snapshot_from_name(path: Path) -> str:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", path.name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else path.stem


def _num(s: str) -> float | None:
    s = (s or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def import_file(path: Path, db: sqlite3.Connection, snapshot: str | None = None, log=print) -> tuple[int, int]:
    snapshot = snapshot or snapshot_from_name(path)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM medis_items WHERE snapshot=?", (snapshot,))
    fh = _open_text(path)
    rd = csv.reader(fh)
    header = next(rd)
    idx = {k: header.index(v) for k, v in COLS.items() if v in header}
    missing = [v for k, v in COLS.items() if v not in header and k in ("jan", "approval_no")]
    if missing:
        raise SystemExit(f"必須列がありません: {missing}")
    slots = []  # (setting, slot, name_i, short_i, price_i, date_i, code_i, rname_i)
    for st in SETTINGS:
        for n in range(1, 37):
            nn = "".join(chr(ord("０") + int(d)) for d in f"{n:02d}")
            base = f"（{st}）特定保険医療材料名称{nn}"
            if base in header:
                slots.append((st, n, header.index(base), header.index(base + "の略称名"),
                              header.index(base + "の償還価格"), header.index(base + "の告示日"),
                              header.index(base + "の医事コード"), header.index(base + "の医事名称")))
    first_names = [s[2] for s in slots if s[1] == 1]
    n_rows = n_items = 0
    batch = []
    dict_batch = []
    db.execute("DELETE FROM medis_dict WHERE snapshot=?", (snapshot,))
    ji, ai, si, mi = idx["jan"], idx["approval_no"], idx.get("sales_name"), idx.get("maker")
    pi_ = idx.get("product_name")
    for row in rd:
        n_rows += 1
        if len(row) < len(header):
            continue
        # 承認番号・JAN の辞書（OCR の補正に使う。償還の有無を問わず全行）
        if row[ai].strip():
            dict_batch.append((snapshot, row[ji].strip(), row[ai].replace(" ", "").strip().upper(),
                               row[si].strip() if si is not None else "", row[mi].strip() if mi is not None else "",
                               row[pi_].strip() if pi_ is not None else ""))
            if len(dict_batch) >= 50000:
                db.executemany("INSERT INTO medis_dict VALUES (?,?,?,?,?,?)", dict_batch)
                dict_batch.clear()
        if not any(row[i] for i in first_names):
            continue
        g = {k: row[i].strip() for k, i in idx.items()}
        for st, n, ni, si, pi, di, ci, ri in slots:
            name = row[ni].strip()
            code = row[ci].strip()
            if not name and not code:
                continue
            batch.append((snapshot, g.get("jan"), g.get("approval_no", "").replace(" ", "").upper(), g.get("sales_name"),
                          g.get("product_name"), g.get("spec"), g.get("product_no"), g.get("maker"),
                          g.get("end_date"), g.get("updated"), g.get("claim_kubun"), st, n, name, row[si].strip(),
                          _num(row[pi]), row[di].strip(), code, row[ri].strip()))
            n_items += 1
        if len(batch) >= 20000:
            db.executemany("INSERT INTO medis_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
        if n_rows % 200000 == 0:
            log(f"  {n_rows:,}行処理…（償還情報 {n_items:,}件）")
    if batch:
        db.executemany("INSERT INTO medis_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    if dict_batch:
        db.executemany("INSERT INTO medis_dict VALUES (?,?,?,?,?,?)", dict_batch)
    db.execute("CREATE INDEX IF NOT EXISTS ix_medis_dict_appr ON medis_dict(approval_no)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_medis_dict_jan ON medis_dict(jan)")
    db.execute("INSERT OR REPLACE INTO medis_snapshots VALUES (?,?,?,?)", (snapshot, path.name, n_rows, n_items))
    db.commit()
    return n_rows, n_items


if __name__ == "__main__":
    con = sqlite3.connect(sys.argv[2] if len(sys.argv) > 2 else "data/medis/medis.sqlite")
    print(import_file(Path(sys.argv[1]), con))

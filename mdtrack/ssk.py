"""支払基金（現：医療情報基盤・診療報酬審査支払機構）の特定器材マスターを収集・解析する。

現行ページと、改定年度ごとのアーカイブページ（平成24年〜）に「全件分」「改定分」ファイルが
並んでいる。全ファイルを取り込み、特定器材コードごとの（変更年月日, 価格, 名称）の履歴を作る。
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.hpdx.or.jp/seikyushiharai/tensuhyo/kihonmasta/"
ARCHIVES = ["", "r06/", "r04/", "r02/", "r01/", "h30/", "h28/", "h26/", "h24/"]
UA = "mdtrack/1.0 (reimbursement history research tool)"

BEPPYO = {"1": "Ⅰ在宅", "2": "Ⅱ医科", "3": "Ⅲフィルム", "4": "Ⅳ歯科注射", "5": "Ⅴ歯科処置等",
          "6": "Ⅵ歯冠修復", "7": "Ⅶ歯科矯正", "8": "Ⅷ調剤", "9": "Ⅸ経過措置"}


@dataclass
class MasterRow:
    file: str
    file_date: str        # ファイル名の日付（YYYYMMDD）
    change_flag: str      # 0:同じ 1:抹消 3:新規 5:変更 9:廃止
    code: str             # 特定器材コード（9桁）
    name: str
    unit: str
    price_kind: str
    price: float | None
    change_date: str      # 変更年月日
    abolish_date: str     # 廃止年月日
    beppyo: str           # 告示 別表番号
    kubun_no: str         # 告示 区分番号（3桁ゼロ埋め）
    basic_name: str       # 基本漢字名称


def list_files(session: requests.Session | None = None) -> list[tuple[str, str]]:
    """(URL, ファイル名) の一覧。"""
    s = session or requests.Session()
    out, seen = [], set()
    for a in ARCHIVES:
        url = urljoin(BASE, a + "kihonmasta_05.html")
        try:
            r = s.get(url, headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
        except Exception:  # noqa: BLE001
            continue
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.find_all("a", href=True):
            h = el["href"]
            if not h.lower().endswith((".zip", ".csv")):
                continue
            fu = urljoin(url, h)
            name = Path(h).name
            if name in seen:
                continue
            seen.add(name)
            out.append((fu, name))
    return out


def download(files: list[tuple[str, str]], dest: Path, delay: float = 0.5, log=print) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    for url, name in files:
        p = dest / name
        if p.exists() and p.stat().st_size > 0:
            continue
        r = s.get(url, headers={"User-Agent": UA}, timeout=120)
        if r.status_code != 200:
            log(f"  失敗 {url} {r.status_code}")
            continue
        p.write_bytes(r.content)
        log(f"  取得: {name}")
        time.sleep(delay)


def _file_date(name: str) -> str:
    m = re.search(r"(\d{8})", name)
    return m.group(1) if m else ""


def _iter_csv_bytes(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    yield n, z.read(n)
    else:
        yield path.name, path.read_bytes()


def parse_file(path: Path) -> list[MasterRow]:
    rows = []
    for inner, data in _iter_csv_bytes(path):
        text = data.decode("cp932", errors="replace")
        for rec in csv.reader(io.StringIO(text, newline="")):
            if len(rec) < 32 or rec[1] != "T":
                continue
            try:
                price = float(rec[11]) if rec[11] else None
            except ValueError:
                price = None
            rows.append(MasterRow(
                file=path.name, file_date=_file_date(path.name) or _file_date(inner),
                change_flag=rec[0], code=rec[2], name=rec[4], unit=rec[9], price_kind=rec[10], price=price,
                change_date=rec[27], abolish_date=rec[29], beppyo=rec[30].lstrip("0") or rec[30],
                kubun_no=rec[31].zfill(3), basic_name=rec[36] if len(rec) > 36 else "",
            ))
    return rows


def parse_all(src: Path) -> list[MasterRow]:
    out = []
    for p in sorted(src.glob("*")):
        if p.suffix.lower() in (".zip", ".csv"):
            out.extend(parse_file(p))
    return out

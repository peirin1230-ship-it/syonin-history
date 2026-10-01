"""地方厚生局の「医療機器の保険適用関係」ページから通知PDFを収集する。

東海北陸厚生局のページは平成20年度以降の通知がすべて1ページに並んでいるため、
既定ではこれを使う。別の厚生局のページを使う場合は --index-url で指定する。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import jpdate

DEFAULT_INDEX_URL = "https://kouseikyoku.mhlw.go.jp/tokaihokuriku/iryo_hoken/iryokiki/index.html"
UA = "mdtrack/1.0 (reimbursement history research tool)"


@dataclass
class NoticeLink:
    doc_id: str          # ファイル名由来のID（例: 000500114）
    title: str
    url: str
    kind: str            # notice / correction / replacement / amendment
    notice_date: str | None   # 通知日（YYYY-MM-DD）
    effective_date: str | None  # 標題に「…から新たに適用」があればその日付
    fiscal_section: str | None  # ページ上の見出し（令和8年度通知 など）


def classify(title: str) -> str | None:
    t = jpdate.normalize(title)
    if "医療機器の保険適用について" not in t:
        return None
    if "一部訂正" in t:
        return "correction"
    if "差し替え" in t or "差替" in t:
        return "replacement"
    if "一部改正" in t:
        return "amendment"
    if re.match(r"^「?医療機器の保険適用について」?\s*[（(]", t):
        return "notice"
    return None


def parse_index(html: str, base_url: str) -> list[NoticeLink]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[NoticeLink] = []
    seen = set()
    section = None
    for el in soup.find_all(["h2", "h3", "h4", "a"]):
        if el.name != "a":
            txt = el.get_text(strip=True)
            if "年度" in txt:
                section = txt
            continue
        href = el.get("href", "")
        if not href.lower().endswith(".pdf"):
            continue
        title = el.get_text(" ", strip=True)
        kind = classify(title)
        if not kind:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        dates = jpdate.parse_all(title)
        eff = None
        nd = dates[-1] if dates else None
        if "から" in title and len(dates) >= 2:
            eff = dates[0]
        stem = Path(href).stem
        # 古い documents/0084.pdf 形式は年度ごとに番号が重複しうるのでパスも含める
        doc_id = stem if re.fullmatch(r"\d{9}", stem) else re.sub(r"[^0-9A-Za-z]+", "_", href.strip("/"))
        out.append(NoticeLink(doc_id, title, url, kind,
                              nd.isoformat() if nd else None,
                              eff.isoformat() if eff else None, section))
    return out


def fetch_index(index_url: str = DEFAULT_INDEX_URL, session: requests.Session | None = None) -> list[NoticeLink]:
    s = session or requests.Session()
    r = s.get(index_url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return parse_index(r.text, index_url)


def download_all(links: list[NoticeLink], pdf_dir: Path, delay: float = 1.0,
                 session: requests.Session | None = None, log=print) -> list[NoticeLink]:
    """未取得のPDFだけをダウンロードする（取得済みはスキップ）。"""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    s = session or requests.Session()
    got = []
    for ln in links:
        dest = pdf_dir / f"{ln.doc_id}.pdf"
        if dest.exists() and dest.stat().st_size > 0:
            continue
        for attempt in range(3):
            try:
                r = s.get(ln.url, headers={"User-Agent": UA}, timeout=120)
                r.raise_for_status()
                if not r.content.startswith(b"%PDF"):
                    raise ValueError("PDFではない応答")
                dest.write_bytes(r.content)
                got.append(ln)
                log(f"  取得: {ln.title[:60]}")
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    log(f"  失敗: {ln.url} ({e})")
                time.sleep(3 * (attempt + 1))
        time.sleep(delay)
    return got


def save_manifest(links: list[NoticeLink], path: Path) -> None:
    path.write_text(json.dumps([asdict(x) for x in links], ensure_ascii=False, indent=1), encoding="utf-8")


def load_manifest(path: Path) -> list[NoticeLink]:
    return [NoticeLink(**d) for d in json.loads(path.read_text(encoding="utf-8"))]

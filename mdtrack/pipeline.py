"""収集 → 解析 → DB構築 → 出力 の各ステップ。"""
from __future__ import annotations

import gzip
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import fetch, parse_notice, ssk


class Paths:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.pdf = self.data / "pdf"
        self.parsed = self.data / "parsed"
        self.ssk = self.data / "ssk"
        self.medis = self.data / "medis"
        self.manifest = self.data / "manifest.json"
        self.db = self.data / "syonin.sqlite"
        self.out = root / "output"


def step_fetch(p: Paths, index_url: str = fetch.DEFAULT_INDEX_URL, log=print) -> list[fetch.NoticeLink]:
    log("通知一覧を取得中…")
    links = fetch.fetch_index(index_url)
    old = {m.doc_id: m for m in fetch.load_manifest(p.manifest)} if p.manifest.exists() else {}
    new = [m for m in links if m.doc_id not in old]
    merged = links + [m for d, m in old.items() if d not in {x.doc_id for x in links}]
    p.data.mkdir(parents=True, exist_ok=True)
    fetch.save_manifest(merged, p.manifest)
    log(f"  通知 {len(links)}本（新規 {len(new)}本）")
    # 解析済み（キャッシュが現行バージョン）の通知はPDFを再取得しない
    need = []
    for m in merged:
        try:
            d = read_parsed(parsed_path(p, m.doc_id))
        except Exception:  # noqa: BLE001
            d = None
        if not d or d.get("version") != parse_notice.PARSER_VERSION:
            need.append(m)
    got = fetch.download_all(need, p.pdf, log=log)
    log(f"  PDF取得 {len(got)}本")
    return new


def step_ssk(p: Paths, log=print) -> None:
    log("特定器材マスターを取得中…")
    files = ssk.list_files()
    ssk.download(files, p.ssk, log=log)
    log(f"  ファイル {len(files)}件")


def parsed_path(p: "Paths", doc_id: str) -> Path:
    return p.parsed / f"{doc_id}.json.gz"


def write_parsed(path: Path, d: dict) -> None:
    path.write_bytes(gzip.compress(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9))


def read_parsed(path: Path) -> dict | None:
    if path.exists():
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    alt = path.with_suffix("")  # 旧形式 .json
    if alt.exists():
        return json.loads(alt.read_text(encoding="utf-8"))
    return None


def _parse_one(args):
    pdf_path, out_path, doc_id, eff = args
    try:
        if not parse_notice.has_text(pdf_path):
            d = {"version": parse_notice.PARSER_VERSION, "scanned": True}
        else:
            r = parse_notice.parse_notice(pdf_path, doc_id, eff)
            d = {"version": parse_notice.PARSER_VERSION, "scanned": False,
                 "records": [x.as_dict() for x in r.records], "warnings": r.warnings, "headings": r.headings}
    except Exception as e:  # noqa: BLE001
        d = {"version": parse_notice.PARSER_VERSION, "scanned": False, "records": [], "error": repr(e),
             "warnings": [f"解析エラー: {e!r}"]}
    write_parsed(Path(out_path), d)
    return doc_id, len(d.get("records", [])), d.get("scanned"), d.get("error")


def step_parse(p: Paths, workers: int = 2, force: bool = False, log=print) -> None:
    manifest = fetch.load_manifest(p.manifest)
    p.parsed.mkdir(parents=True, exist_ok=True)
    jobs = []
    for m in manifest:
        pdf = p.pdf / f"{m.doc_id}.pdf"
        out = parsed_path(p, m.doc_id)
        if not pdf.exists():
            continue
        if not force:
            try:
                d = read_parsed(out)
                if d and d.get("version") == parse_notice.PARSER_VERSION:
                    continue
            except Exception:  # noqa: BLE001
                pass
        jobs.append((str(pdf), str(out), m.doc_id, m.effective_date))
    log(f"PDF解析: {len(jobs)}本（解析済みはスキップ）")
    if not jobs:
        return
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for doc_id, n, scanned, err in ex.map(_parse_one, jobs, chunksize=1):
            done += 1
            if err:
                log(f"  [{done}/{len(jobs)}] {doc_id}: エラー {err}")
            elif done % 20 == 0 or done == len(jobs):
                log(f"  [{done}/{len(jobs)}] 完了")


def step_build(p: Paths, log=print) -> None:
    from . import db
    log("データベースを構築中…")
    manifest = fetch.load_manifest(p.manifest)
    db.build(p.db, manifest, p.parsed, p.ssk, log=log)
    log(f"  → {p.db}")

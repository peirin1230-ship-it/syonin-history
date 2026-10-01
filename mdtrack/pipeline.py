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
    # 手元のPDFから取り込んだ通知（add-pdf）は、厚生局に同じ通知が載ったら公式版に置き換える
    official = {fetch.same_notice_key(m) for m in links}
    replaced = [m for d, m in old.items() if d.startswith(fetch.LOCAL_PREFIX) and fetch.same_notice_key(m) in official]
    for m in replaced:
        for f in (parsed_path(p, m.doc_id), p.pdf / f"{m.doc_id}.pdf"):
            f.unlink(missing_ok=True)
        log(f"  手元PDFの通知を公式版に置換: {m.title[:50]}")
    drop = {x.doc_id for x in links} | {m.doc_id for m in replaced}
    merged = links + [m for d, m in old.items() if d not in drop]
    p.data.mkdir(parents=True, exist_ok=True)
    fetch.save_manifest(merged, p.manifest)
    n_local = sum(1 for m in merged if m.doc_id.startswith(fetch.LOCAL_PREFIX))
    log(f"  通知 {len(links)}本（新規 {len(new)}本）" + (f"／厚生局に未掲載で手元PDFから取込済み {n_local}本" if n_local else ""))
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


def step_add_pdf(p: Paths, pdf_path: Path, log=print) -> fetch.NoticeLink:
    """厚生局のページにまだ載っていない通知を、手元のPDFから取り込む。

    PDF の表紙（保医発の番号・通知日・「…から新たに保険適用」）から通知の種類と日付を読み、
    manifest に doc_id「local_YYYYMMDD_番号」で登録して解析する。厚生局に同じ通知（種類・通知日・
    適用日が同じ）が載ると、次の update で公式版に置き換わる。
    """
    import shutil
    link = fetch.link_from_local_pdf(pdf_path, parse_notice.cover_text(pdf_path))
    manifest = fetch.load_manifest(p.manifest) if p.manifest.exists() else []
    if any(fetch.same_notice_key(m) == fetch.same_notice_key(link) and not m.doc_id.startswith(fetch.LOCAL_PREFIX)
           for m in manifest):
        log(f"同じ通知が厚生局のページから取得済みです: {link.title}")
        return link
    manifest = [m for m in manifest if m.doc_id != link.doc_id] + [link]
    fetch.save_manifest(manifest, p.manifest)
    p.pdf.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf_path, p.pdf / f"{link.doc_id}.pdf")
    p.parsed.mkdir(parents=True, exist_ok=True)
    doc_id, n, scanned, err = _parse_one((str(p.pdf / f"{link.doc_id}.pdf"), str(parsed_path(p, link.doc_id)),
                                          link.doc_id, link.effective_date))
    log(f"手元PDFを取込: {link.title}")
    log(f"  doc_id {doc_id} / 種類 {link.kind} / 通知日 {link.notice_date} / 適用日 {link.effective_date} / "
        f"{'スキャン画像（要OCR）' if scanned else f'{n}行'}" + (f" / エラー {err}" if err else ""))
    return link


def step_ssk(p: Paths, log=print) -> None:
    log("特定器材マスターを取得中…")
    errors: list = []
    files = ssk.list_files(errors=errors)
    ssk.download(files, p.ssk, log=log)
    log(f"  ファイル {len(files)}件")
    # 掲載から外れた古いファイル（差し替えられた全件ファイルなど）を消して、新規取得と同じ状態にする。
    # 一覧ページを1つでも取得できなかったときは消さない
    if files and not errors:
        listed = {name for _, name in files}
        for f in p.ssk.glob("*"):
            if f.is_file() and f.name not in listed:
                f.unlink()
                log(f"  掲載終了のため削除: {f.name}")
    elif errors:
        log(f"  一覧を取得できなかったページ: {len(errors)}件")


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
                if d and (d.get("version") == parse_notice.PARSER_VERSION or d.get("ocr")):
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


OCR_VERSION = 1
_ENG = None


def _ocr_init():
    import os
    os.environ["OMP_THREAD_LIMIT"] = "1"
    global _ENG
    from .ocr_notice import Engines
    _ENG = Engines()


def _ocr_one(args):
    pdf_path, out_path, doc_id, eff = args
    import time
    from .ocr_notice import OcrNoticeParser
    t0 = time.time()
    try:
        r = OcrNoticeParser(doc_id, eff, _ENG).parse(pdf_path)
        d = {"version": parse_notice.PARSER_VERSION, "scanned": True, "ocr": True, "ocr_version": OCR_VERSION,
             "records": [x.as_dict() for x in r.records], "warnings": r.warnings, "headings": r.headings,
             "stats": r.stats, "seconds": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        d = {"version": parse_notice.PARSER_VERSION, "scanned": True, "ocr": False, "error": repr(e)}
    write_parsed(Path(out_path), d)
    return doc_id, len(d.get("records", [])), d.get("seconds"), d.get("error")


def step_ocr(p: Paths, workers: int = 2, force: bool = False, only: list[str] | None = None, log=print) -> None:
    """スキャン画像の通知を OCR する（要 tesseract と jpn 学習データ）。時間がかかる（1頁 約5秒）。"""
    manifest = fetch.load_manifest(p.manifest)
    jobs = []
    for m in manifest:
        if only and m.doc_id not in only:
            continue
        out = parsed_path(p, m.doc_id)
        d = read_parsed(out)
        if not d or not d.get("scanned"):
            continue
        if d.get("ocr") and d.get("ocr_version") == OCR_VERSION and not force:
            continue
        pdf = p.pdf / f"{m.doc_id}.pdf"
        if not pdf.exists():
            continue
        jobs.append((str(pdf), str(out), m.doc_id, m.effective_date))
    log(f"OCR: {len(jobs)}本")
    if not jobs:
        return
    done = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_ocr_init) as ex:
        for doc_id, n, sec, err in ex.map(_ocr_one, jobs, chunksize=1):
            done += 1
            log(f"  [{done}/{len(jobs)}] {doc_id}: {n}行 {sec}秒" + (f" エラー {err}" if err else ""))


OCR_FIX_VERSION = 1


def step_ocr_fix(p: Paths, log=print) -> None:
    """OCR 結果の承認番号・製品コードを既知の番号と照合して補正し、解析キャッシュに書き戻す。

    辞書: テキストPDF期の通知（data/syonin.sqlite）＋ MEDIS（data/medis/medis.sqlite があれば）
    ＋ OCR 結果どうしで3回以上一致した承認番号。
    """
    import re
    from collections import Counter
    from . import ocr_repair
    manifest = fetch.load_manifest(p.manifest)
    docs = []
    for m in manifest:
        d = read_parsed(parsed_path(p, m.doc_id))
        if d and d.get("ocr"):
            docs.append((m, d))
    if not docs:
        log("OCR結果がありません")
        return
    log(f"OCR補正: {len(docs)}本 / 辞書を読み込み中…")
    dic = ocr_repair.load_dictionary(p.db, p.medis / "medis.sqlite")
    # OCR 結果どうしの多数決（構造が正しい承認番号が別々の通知で3回以上）
    seen: dict[str, set] = {}
    for m, d in docs:
        for r in d["records"]:
            a = r.get("approval_ocr") or r["approval_no"]
            if re.fullmatch(r"\d{5}[A-Z]{3}\d{5}[0-9A-Z]\d{2}|\d{3}[A-Z]{5}\d{5}[0-9A-Z]\d{2}", a or ""):
                seen.setdefault(a, set()).add(m.doc_id)
    for a, ds in seen.items():
        if len(ds) >= 3:
            dic.add(a)
    total = Counter()
    for m, d in docs:
        st = ocr_repair.repair_records(d["records"], dic)
        total.update(st)
        d["repaired"] = OCR_FIX_VERSION
        d["repair_stats"] = st
        write_parsed(parsed_path(p, m.doc_id), d)
    log("  " + ", ".join(f"{k} {v:,}" for k, v in sorted(total.items())))


def step_build(p: Paths, log=print) -> None:
    from . import db
    log("データベースを構築中…")
    manifest = fetch.load_manifest(p.manifest)
    db.build(p.db, manifest, p.parsed, p.ssk, log=log)
    log(f"  → {p.db}")

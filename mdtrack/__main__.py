"""コマンドライン:  python -m mdtrack <コマンド>

  update            収集 → 解析 → DB構築 → 出力 をまとめて実行（毎月これだけでOK）
  fetch             通知PDFと特定器材マスターを取得（差分のみ）
  parse [--force]   PDFを解析（解析済みはスキップ）
  build             SQLite データベースを構築
  medis FILE        MEDISダウンロードファイル（.lzh / .txt）を取り込む
  export            Excel・HTMLビューア・JSONを出力
  show 承認番号      承認番号の変更履歴をターミナルに表示
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import Paths, step_build, step_fetch, step_parse, step_ssk


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mdtrack", description="医療機器 承認番号別 償還区分 履歴トラッカー")
    ap.add_argument("--root", default=".", help="作業フォルダ（既定: カレント）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("update")
    sub.add_parser("fetch")
    sp = sub.add_parser("parse")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--workers", type=int, default=2)
    sub.add_parser("build")
    sm = sub.add_parser("medis")
    sm.add_argument("file")
    sm.add_argument("--snapshot", help="スナップショット日付（既定: ファイル名の日付）")
    sub.add_parser("export")
    ss = sub.add_parser("show")
    ss.add_argument("approval_no")
    a = ap.parse_args(argv)
    p = Paths(Path(a.root).resolve())

    if a.cmd in ("update", "fetch"):
        step_fetch(p)
        step_ssk(p)
    if a.cmd in ("update", "parse"):
        step_parse(p, workers=getattr(a, "workers", 2), force=getattr(a, "force", False))
    if a.cmd in ("update", "build"):
        step_build(p)
    if a.cmd == "medis":
        import sqlite3
        from . import medis
        p.medis.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p.medis / "medis.sqlite")
        n_rows, n_items = medis.import_file(Path(a.file), con, a.snapshot)
        print(f"MEDIS取込: {n_rows:,}行中 償還情報 {n_items:,}件")
    if a.cmd in ("update", "export", "medis"):
        from . import export
        export.export_all(p)
    if a.cmd == "show":
        from . import export
        export.print_history(p, a.approval_no)
    if not a.cmd:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

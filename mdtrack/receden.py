"""receden-history（レセ電コード変更履歴ツール）との連携。

https://github.com/peirin1230-ship-it/receden-history は、各改定世代の全件マスター（S/Y/T/C/B/Z）を
突き合わせてコード単位の新設・変更・廃止を復元するツール。ここでは特定器材（T）について:

1. 全件ファイルの取込: receden-history に置かれた特定器材の全件ファイル（t_ALL*.csv）のうち、
   こちらに無いもの（支払基金のサイトから既に消えた古い版など）を data/ssk に追加する。
   全件ファイルが増えるほど、「マスターから消えた時期」による廃止の推定が細かくなる。
2. 突き合わせ: receden-history の履歴DB（`receden build-history` の data/db/masters.sqlite）と、
   こちらの特定器材マスター履歴（価格改定・廃止）を比べて、食い違いを一覧にする（開発・検証用）。

コードの個別ページ（https://peirin1230-ship-it.github.io/receden-history/#/T/コード）へのリンクは
ビューアから張る。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

REPO_URL = "https://github.com/peirin1230-ship-it/receden-history"
SITE_URL = "https://peirin1230-ship-it.github.io/receden-history/"


def _all_dates(ssk_dir: Path) -> set[str]:
    return {m.group(1) for f in ssk_dir.glob("*") if (m := re.search(r"_ALL(\d{8})", f.name))}


def import_snapshots(ssk_dir: Path, src: Path | None = None, log=print) -> list[str]:
    """receden-history の特定器材の全件ファイルのうち、こちらに無い日付のものを ssk_dir にコピーする。

    src を省略すると、リポジトリから特定器材の全件ファイルだけを取得する（sparse checkout、数秒）。
    取得できなくても処理は止めない（空のリストを返す）。
    """
    tmp = None
    try:
        if src is None:
            tmp = Path(tempfile.mkdtemp(prefix="receden_"))
            src = tmp / "repo"
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", REPO_URL,
                            str(src)], check=True, timeout=300)
            subprocess.run(["git", "-C", str(src), "sparse-checkout", "set", "--no-cone",
                            "/data/raw/*/t_ALL*", "/data/raw/t_ALL*"], check=True, timeout=300)
        have = _all_dates(ssk_dir)
        added = []
        for f in sorted(src.glob("data/raw/**/t_ALL*")):
            m = re.search(r"_ALL(\d{8})", f.name)
            if not m or m.group(1) in have or f.suffix.lower() not in (".csv", ".zip"):
                continue
            ssk_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, ssk_dir / f.name)
            have.add(m.group(1))
            added.append(f.name)
            log(f"  receden-history から全件ファイルを追加: {f.name}")
        return added
    except Exception as e:  # noqa: BLE001
        log(f"  receden-history から取得できませんでした（{e}）。手元のファイルだけで続けます")
        return []
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def compare(our_db: Path, receden_db: Path, out_csv: Path | None = None) -> dict:
    """receden-history の特定器材の履歴（events）と、こちらの ssk_history を比べる。

    receden-history は改定世代ごとの全件ファイルの差分なので、世代内の途中の改定はまとめて1回に見える。
    こちらは改定分ファイルも使うため細かい。比べるのは:
      - 価格改定: receden の（日付・新価格）が、こちらの同じ日の価格と一致するか
      - 廃止: 両方にあるか、日付が近いか（receden の era_boundary は世代の施行日で代用された日付）
      - 新設: 最初の版の日付
    """
    o = sqlite3.connect(our_db)
    r = sqlite3.connect(receden_db)
    hist: dict[str, list] = defaultdict(list)
    cols = [x[1] for x in o.execute("PRAGMA table_info(ssk_history)")]
    src_col = "abolish_source" if "abolish_source" in cols else "''"
    for code, vf, price, ab, src in o.execute(
            f"SELECT code, valid_from, price, abolish_date, {src_col} FROM ssk_history ORDER BY code, valid_from"):
        hist[code].append((vf, price, ab or "", src or ""))
    ev: dict[str, list] = defaultdict(list)
    for code, et, d, prec, cf in r.execute("SELECT code, event_type, event_date, date_precision, changed_fields "
                                           "FROM events WHERE master='T'"):
        ev[code].append((et, d, prec, json.loads(cf) if cf else []))
    st, rows = Counter(), []

    def price_on(code, d):
        v = None
        for x in hist[code]:
            if x[0] <= d:
                v = x
        return v[1] if v else None

    for code, es in ev.items():
        if code not in hist:
            st["receden にだけあるコード"] += 1
            rows.append((code, "receden_only_code", "", "", ""))
            continue
        ab = hist[code][-1][2]
        for et, d, prec, cf in es:
            if et == "changed":
                for f in cf:
                    if f.get("field") != "price":
                        continue
                    p = price_on(code, d)
                    if p is not None and abs(p - float(f["new"])) < 0.5:
                        st["価格: 一致"] += 1
                    else:
                        st["価格: 不一致"] += 1
                        rows.append((code, "price", d, f"{f['old']}→{f['new']}", p))
            elif et == "abolished":
                if not ab:
                    st["廃止: receden のみ"] += 1
                    rows.append((code, "abolished_receden_only", d, prec, ""))
                elif ab == d or (prec == "era_boundary" and ab < d and int(d[:4]) - int(ab[:4]) <= 1):
                    st["廃止: 一致（世代境界の近似を含む）"] += 1
                else:
                    st["廃止: 日付が異なる"] += 1
                    rows.append((code, "abolished_date", d, prec, ab))
            elif et == "new":
                st["新設: 日付一致" if hist[code][0][0] == d else "新設: 日付が異なる"] += 1
    rab = {c for c, es in ev.items() if any(e[0] == "abolished" for e in es)}
    for code, h in hist.items():
        if h[-1][2] and code not in rab:
            st["廃止: こちらのみ"] += 1
            rows.append((code, "abolished_ours_only", h[-1][2], h[-1][3], ""))
    st["こちらにだけあるコード"] = len(set(hist) - set(ev))
    if out_csv:
        import csv
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["特定器材コード", "種類", "receden の日付", "receden の内容", "こちらの値"])
            w.writerows(rows)
    return dict(st)

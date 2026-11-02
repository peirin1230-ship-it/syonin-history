"""手元PDFから取り込んだ通知（add-pdf）の PDF を docs/pdf/ に置いてビューアからリンクする処理。"""
from mdtrack import fetch
from mdtrack.export import PAGES_URL, notice_url
from mdtrack.pipeline import Paths, local_pdf_path, sync_local_pdfs

LOCAL = "local_20261030_notice_2"


def _paths(tmp_path, doc_ids):
    p = Paths(tmp_path)
    p.data.mkdir(parents=True)
    fetch.save_manifest([fetch.NoticeLink(d, d, "" if d.startswith(fetch.LOCAL_PREFIX) else f"https://x/{d}.pdf",
                                          "notice", "2026-10-30", "2026-11-01", None) for d in doc_ids], p.manifest)
    return p


def test_sync_copies_data_pdf_to_docs_and_removes_replaced(tmp_path):
    p = _paths(tmp_path, [LOCAL, "000502873"])
    p.pdf.mkdir(parents=True)
    (p.pdf / f"{LOCAL}.pdf").write_bytes(b"%PDF-local")
    p.local_pdf.mkdir(parents=True)
    stale = p.local_pdf / "local_20260930_notice_4.pdf"  # 公式版に置き換わって manifest から消えた手元PDF
    stale.write_bytes(b"%PDF-old")
    sync_local_pdfs(p, log=lambda *a: None)
    assert local_pdf_path(p, LOCAL).read_bytes() == b"%PDF-local"
    assert not stale.exists()
    assert not (p.local_pdf / "000502873.pdf").exists()  # 厚生局の通知は置かない


def test_sync_restores_data_pdf_from_docs(tmp_path):
    # 新しく clone した環境（data/pdf/ は Git に含めない）でも再解析できるように戻す
    p = _paths(tmp_path, [LOCAL])
    p.local_pdf.mkdir(parents=True)
    local_pdf_path(p, LOCAL).write_bytes(b"%PDF-local")
    sync_local_pdfs(p, log=lambda *a: None)
    assert (p.pdf / f"{LOCAL}.pdf").read_bytes() == b"%PDF-local"


def test_notice_url(tmp_path):
    p = _paths(tmp_path, [LOCAL])
    assert notice_url(p, "000502873", "https://x/000502873.pdf", p.docs) == "https://x/000502873.pdf"
    assert notice_url(p, LOCAL, "", p.docs) == ""  # PDF が無ければリンクなし
    p.local_pdf.mkdir(parents=True)
    local_pdf_path(p, LOCAL).write_bytes(b"%PDF-local")
    assert notice_url(p, LOCAL, "", p.docs) == f"pdf/{LOCAL}.pdf"            # docs/index.html から
    assert notice_url(p, LOCAL, None, p.out) == f"../docs/pdf/{LOCAL}.pdf"  # output/viewer.html から
    assert notice_url(p, LOCAL, "", None) == f"{PAGES_URL}docs/pdf/{LOCAL}.pdf"  # Artifact 用の断片

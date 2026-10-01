"""libtesseract の C API を ctypes で直接呼ぶ薄いラッパー。

pytesseract はセルごとにプロセスを起動し、毎回モデルを読み込むため遅い。
ここではプロセス内で一度だけ初期化し、画像（numpy 配列）を次々に渡す。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

import numpy as np

PSM_SINGLE_BLOCK = 6
PSM_SINGLE_LINE = 7
PSM_SPARSE = 11


def _lib():
    name = ctypes.util.find_library("tesseract") or "libtesseract.so.5"
    lib = ctypes.CDLL(name)
    lib.TessBaseAPICreate.restype = ctypes.c_void_p
    lib.TessBaseAPIInit3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.TessBaseAPIInit3.restype = ctypes.c_int
    lib.TessBaseAPISetPageSegMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.TessBaseAPISetVariable.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.TessBaseAPISetVariable.restype = ctypes.c_int
    lib.TessBaseAPISetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int]
    lib.TessBaseAPISetSourceResolution.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.TessBaseAPIGetUTF8Text.argtypes = [ctypes.c_void_p]
    lib.TessBaseAPIGetUTF8Text.restype = ctypes.c_void_p
    lib.TessBaseAPIGetTsvText.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.TessBaseAPIGetTsvText.restype = ctypes.c_void_p
    lib.TessBaseAPIMeanTextConf.argtypes = [ctypes.c_void_p]
    lib.TessBaseAPIMeanTextConf.restype = ctypes.c_int
    lib.TessDeleteText.argtypes = [ctypes.c_void_p]
    lib.TessBaseAPIClear.argtypes = [ctypes.c_void_p]
    lib.TessBaseAPIEnd.argtypes = [ctypes.c_void_p]
    lib.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
    return lib


_LIB = None


def default_datapath() -> str:
    for p in (os.environ.get("MDTRACK_TESSDATA"), str(Path(__file__).resolve().parent.parent / "tessdata"),
              "/home/claude/tessdata", "/usr/share/tesseract-ocr/5/tessdata"):
        if p and Path(p, "jpn.traineddata").exists():
            return p
    raise SystemExit("jpn.traineddata が見つかりません。README の「OCR の準備」を参照してください。")


class Tess:
    def __init__(self, lang: str = "jpn", datapath: str | None = None, psm: int = PSM_SINGLE_BLOCK,
                 whitelist: str | None = None, dpi: int = 300):
        global _LIB
        if _LIB is None:
            _LIB = _lib()
        self.lib = _LIB
        self.h = self.lib.TessBaseAPICreate()
        dp = (datapath or default_datapath()).encode()
        if self.lib.TessBaseAPIInit3(self.h, dp, lang.encode()) != 0:
            raise RuntimeError(f"tesseract 初期化失敗 lang={lang}")
        self.lib.TessBaseAPISetPageSegMode(self.h, psm)
        if whitelist:
            self.lib.TessBaseAPISetVariable(self.h, b"tessedit_char_whitelist", whitelist.encode())
        self.lib.TessBaseAPISetVariable(self.h, b"preserve_interword_spaces", b"1")
        self.dpi = dpi

    def _set(self, img: np.ndarray):
        img = np.ascontiguousarray(img, dtype=np.uint8)
        h, w = img.shape[:2]
        bpp = 1 if img.ndim == 2 else img.shape[2]
        self._keep = img
        self.lib.TessBaseAPISetImage(self.h, img.ctypes.data, w, h, bpp, w * bpp)
        self.lib.TessBaseAPISetSourceResolution(self.h, self.dpi)

    def text(self, img: np.ndarray) -> str:
        self._set(img)
        p = self.lib.TessBaseAPIGetUTF8Text(self.h)
        if not p:
            return ""
        s = ctypes.string_at(p).decode("utf-8", "replace")
        self.lib.TessDeleteText(p)
        return s

    def tsv(self, img: np.ndarray) -> list[dict]:
        """単語ごとの (left, top, width, height, conf, text, line_key)。"""
        self._set(img)
        p = self.lib.TessBaseAPIGetTsvText(self.h, 0)
        if not p:
            return []
        s = ctypes.string_at(p).decode("utf-8", "replace")
        self.lib.TessDeleteText(p)
        out = []
        for ln in s.splitlines():
            f = ln.split("\t")
            if len(f) < 12 or f[0] != "5":
                continue
            out.append({"block": int(f[2]), "par": int(f[3]), "line": int(f[4]), "left": int(f[6]),
                        "top": int(f[7]), "width": int(f[8]), "height": int(f[9]), "conf": float(f[10]),
                        "text": f[11]})
        return out

    def conf(self) -> int:
        return self.lib.TessBaseAPIMeanTextConf(self.h)

    def close(self):
        if self.h:
            self.lib.TessBaseAPIEnd(self.h)
            self.lib.TessBaseAPIDelete(self.h)
            self.h = None

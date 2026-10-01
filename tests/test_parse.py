from datetime import date

from mdtrack import catmap, jpdate
from mdtrack.parse_notice import Section, gtin_ok, is_approval, parse_heading, parse_price, split_products


def test_jpdate():
    assert jpdate.parse_first("令和８年９月１日から") == date(2026, 9, 1)
    assert jpdate.parse_first("平成31年3月29日") == date(2019, 3, 29)
    assert jpdate.parse_first("令和元年8月30日") == date(2019, 8, 30)


def test_approval():
    for s in ("30800BZX00176000", "308AABZX00017000", "13B1X10166001027", "229ABBZX00080Z00", "22500BZX00021A03"):
        assert is_approval(s), s
    assert not is_approval("承認番号又は認証番号")
    assert not is_approval("4580580441388")


def test_gtin():
    assert gtin_ok("4580580441388")
    assert gtin_ok("00843997000666") or not gtin_ok("0084399700066")


def test_split_products_basic():
    prods, annex = split_products("ULTRABRIDGEキット 4580387600308")
    assert prods == [("ULTRABRIDGEキット", "4580387600308")] and annex is None


def test_split_products_multi_code_and_annex():
    prods, _ = split_products("PICO創傷治療システム PICO7-T 陰圧維持管理装置 4580443838317\n4580443838324")
    assert [c for _, c in prods] == ["4580443838317", "4580443838324"]
    assert split_products("別表３のとおり") == ([], "3")


def test_price():
    assert parse_price("¥29,600") == (29600.0, None)
    v, unit = parse_price("1㎠当たり¥452")
    assert v == 452.0 and unit.startswith("1")
    assert parse_price("2枚1組¥17,600")[0] == 17600.0


def test_heading():
    s = parse_heading("製品（販売）名・製品コードに追加・変更があったものの保険適用（区分Ｂ１）保険適用開始年月日：令和8年9月1日",
                      Section())
    assert (s.action, s.kubun, s.effective_date) == ("add", "B1", "2026-09-01")
    s = parse_heading("２．歯科\n新たな保険適用 区分Ｃ１（新機能）", s)
    assert (s.setting, s.action, s.kubun) == ("歯科", "new", "C1")


def test_norm_category():
    a = catmap.norm_category("010 血管造影用ﾏｲｸﾛｶﾃｰﾃﾙ (1)ｵｰﾊﾞｰｻﾞﾜｲﾔｰ ①選択的ｱﾌﾟﾛｰﾁ型 ｱ ﾌﾞﾚｰﾄﾞあり")
    b = catmap.norm_master("血管造影用マイクロカテーテル・オーバーザワイヤー・選択的アプローチ型・ブレードあり")
    assert a == b


def test_ocr_repair():
    from mdtrack import ocr_repair
    d = ocr_repair.Dictionary()
    d.add("22700BZI00025000", "4580000000000")
    d.add("20600BZZ00666A01", "4547531706101")
    assert ocr_repair.fix_approval("22700B2I00025000", [], d)[0] == "22700BZI00025000"
    assert ocr_repair.fix_approval("22700BZI0002500", [], d) == ("22700BZI00025000", "fuzzy")
    assert ocr_repair.fix_approval("2270082X0025000", ["4580000000000"], d)[0] == "22700BZI00025000"
    assert ocr_repair.fix_approval("20600BZZ00666A01", [], d)[1] == "exact"
    assert ocr_repair.fix_code("4547531706101", "20600BZZ00666A01", d) == ("4547531706101", "exact")
    assert ocr_repair.fix_code("4547531706181", "20600BZZ00666A01", d) == ("4547531706101", "appr1")


def test_ocr_fix_approval_positions():
    from mdtrack.ocr_notice import fix_approval
    assert fix_approval("2O6OOBZZOO666AO1") == "20600BZZ00666A01"
    assert fix_approval("22OADBZXOO121OOO") == "220ADBZX00121000"
    assert fix_approval("21800B2X10056000") == "21800BZX10056000"


def test_notice_change_helpers():
    from mdtrack.db import _nm, _norm_kubun
    assert _norm_kubun("B") == "B1" and _norm_kubun("C1") == "C1" and _norm_kubun("C") == "C?"
    assert _nm("ＫＺＲ－ＣＡＤ ファイバーブロック") == _nm("ＫＺＲ―ＣＡＤ　ファイバーブロック")

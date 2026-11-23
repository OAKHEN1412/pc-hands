"""Tests for the ocr tool. Run: python -m pytest test_ocr.py -q   (or python test_ocr.py).
Pure parts (fuzzy match, coordinate mapping, region clip) + the confirm gate on OCR ids + one engine smoke test on a
rendered image. Read-only: nothing here clicks or types."""
import sys

import pytest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from logic import clip_region, fuzzy_rank, fuzzy_score, poly_to_rect, reading_order  # noqa: E402


def test_fuzzy_exact_and_case():
    assert fuzzy_score("kim5821", "kim5821") == 1.0
    assert fuzzy_score("KIM5821", "  kim5821 ") == 1.0
    assert fuzzy_score("plum", "Plum Market Westgate") == 1.0
    assert fuzzy_score("ตั้งค่า", "การตั้งค่า LINE") == 1.0


def test_fuzzy_tolerates_ocr_errors():
    assert fuzzy_score("kirn521", "kim5821") >= 0.7            # mn/m confusion + dropped digit
    assert fuzzy_score("กลุ่มเพื่อนร่วมงาน", "กลุ่มเพื่อนร่วมงาบ ทีม...") >= 0.8
    assert fuzzy_score("รายการเครื่องมือ", "รายการเครืองมือทดสอบ") >= 0.85  # lost tone mark
    assert fuzzy_score("ไลน์", "ไลน") >= 0.6


def test_fuzzy_rejects_unrelated():
    assert fuzzy_score("kim5821", "sales.desk") < 0.6
    assert fuzzy_score("ส่ง", "ตารางเทียบ") == 0.0           # short query = exact substring only
    assert fuzzy_score("", "abc") == 0.0 and fuzzy_score("abc", "") == 0.0


def test_fuzzy_rank_order():
    items = [((0, 0, 1, 1), "Plum Market Westgate"), ((0, 20, 1, 1), "kim5821"), ((0, 40, 1, 1), "kim58214 group"),
             ((0, 60, 1, 1), "sales.desk")]
    r = fuzzy_rank("kim5821", items)
    assert [it[1] for _, it in r] == ["kim5821", "kim58214 group"]  # exact first, shorter text first on tie
    assert fuzzy_rank("zzzzzz", items) == []


def test_poly_to_rect_offsets_and_negative_monitor():
    poly = [[10.2, 20.4], [110.6, 20.0], [110.4, 40.1], [10.0, 40.3]]
    assert poly_to_rect(poly, 0, 0) == (10, 20, 101, 20)
    assert poly_to_rect(poly, -1920, 60) == (-1910, 80, 101, 20)   # the left monitor sits at x<0
    assert poly_to_rect([[5, 5], [5, 5], [5, 5], [5, 5]]) == (5, 5, 1, 1)  # never zero-size


def test_clip_region_virtual_screen():
    vs = (-1920, 0, 3840, 1140)
    assert clip_region(-8, -8, 1936, 1048, vs) == (-8, 0, 1928, 1040)        # maximized window overhang (top clipped; x=-8 is still valid screen, the left monitor is left)
    assert clip_region(-2000, 100, 200, 100, vs) == (-1920, 100, 120, 100)
    assert clip_region(5000, 0, 10, 10, vs) is None


def test_reading_order():
    items = [((50, 100, 1, 1), "c"), ((10, 10, 1, 1), "a"), ((90, 10, 1, 1), "b")]
    assert [t for _, t in reading_order(items)] == ["a", "b", "c"]


def test_ocr_id_gate_and_lookup():
    """A #oN id whose text is a send-type label must return needs_confirm without clicking; unknown ids are an error."""
    import tools
    import winio
    fg = winio.foreground_info()
    box = tools.OcrBox("ส่ง", (10, 10, 30, 20), fg["hwnd"], fg["pid"], fg["process"])
    i = tools.OCRS.put(box)
    assert i.startswith("#o")
    orig, tools.check_forbidden = tools.check_forbidden, lambda w: None
    try:
        r = tools.run_tool("click", {"id": i})
        assert r.get("needs_confirm") and not r["ok"], r
        r2 = tools.run_tool("read_text", {"id": i})
        assert r2["ok"] and r2["result"] == "ส่ง", r2
    finally:
        tools.check_forbidden = orig
    assert not tools.run_tool("click", {"id": "#o99999"})["ok"]


def test_engine_reads_rendered_thai_english():
    np = pytest.importorskip("numpy")
    pytest.importorskip("rapidocr")  # optional OCR extra (see README)
    from PIL import Image, ImageDraw, ImageFont
    import ocr
    f = ImageFont.truetype(r"C:\Windows\Fonts\tahoma.ttf", 22)
    im = Image.new("RGB", (520, 130), "white")
    d = ImageDraw.Draw(im)
    d.text((10, 10), "kim5821", font=f, fill="black")
    d.text((10, 50), "กลุ่มเพื่อนร่วมงาน", font=f, fill="black")
    d.text((10, 90), "Plum Market", font=f, fill="black")
    res = ocr.recognize(np.asarray(im))
    texts = [t for _, t, _ in res]
    assert fuzzy_rank("kim5821", [(None, t) for t in texts]), texts
    assert fuzzy_rank("กลุ่มเพื่อนร่วมงาน", [(None, t) for t in texts]), texts
    assert fuzzy_rank("plum market", [(None, t) for t in texts]), texts
    # boxes map back into the image
    for poly, _, _ in res:
        r = poly_to_rect(poly)
        assert 0 <= r[0] < 520 and 0 <= r[1] < 130


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

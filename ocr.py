"""Fast on-screen OCR for pc-hands: RapidOCR (PP-OCRv5 det + Thai/English rec, onnxruntime CPU, no VRAM).
Model is loaded once and kept warm (warm() is called in a background thread by server.py)."""
import threading
import time

import numpy as np

_lock = threading.Lock()
_engine = None
_err = None


def _build():
    from rapidocr import RapidOCR, LangRec, OCRVersion, ModelType
    return RapidOCR(params={
        "Rec.lang_type": LangRec.TH, "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
        "Global.log_level": "warning", "Global.use_cls": False})  # cls flips some upright lines upside down


def _get():
    global _engine, _err
    if _engine is None:
        if _err is not None:
            raise RuntimeError(_err)
        try:
            _engine = _build()
            _engine(np.zeros((64, 256, 3), np.uint8), use_cls=False)  # first call downloads/initialises everything
        except Exception as e:
            _err = f"โหลด OCR ไม่สำเร็จ: {type(e).__name__}: {e}"
            raise RuntimeError(_err)
    return _engine


def warm():
    try:
        with _lock:
            _get()
    except Exception:
        pass


def recognize(img_rgb: np.ndarray):
    """img_rgb: HxWx3 uint8 RGB. Returns [(poly(4 points), text, score)] in image pixels."""
    bgr = np.ascontiguousarray(img_rgb[:, :, ::-1])
    with _lock:
        eng = _get()
        res = eng(bgr, use_cls=False)
    if res is None or res.txts is None:
        return []
    return [(b, t, float(s)) for b, t, s in zip(res.boxes, res.txts, res.scores) if str(t).strip()]

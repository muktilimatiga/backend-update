from api.v1.endpoints.ocr import _process_image_ocr, _process_image_yolo_ocr


def ocr(image_bytes: bytes, lang: str = "en") -> str:
    """Run PaddleOCR on image bytes and return extracted text (simple, no YOLO)."""
    return _process_image_ocr(image_bytes, lang=lang)


def detect_modem(image_bytes: bytes) -> dict:
    """Run YOLO+PaddleOCR pipeline. Returns dict with modem_type and sn."""
    return _process_image_yolo_ocr(image_bytes)

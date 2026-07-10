from api.v1.endpoints.ocr import _process_image_ocr, _process_image_detect


def ocr(image_bytes: bytes, lang: str = "en") -> str:
    """Run Tesseract OCR on image bytes and return extracted text."""
    return _process_image_ocr(image_bytes, lang=lang)


def detect_modem(image_bytes: bytes) -> dict:
    """Run Cloud API pipeline. Returns dict with modem_type and sn."""
    return _process_image_detect(image_bytes)

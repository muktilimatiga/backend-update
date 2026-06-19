"""
SN Extractor — Dedicated serial number extraction from cropped modem label images.

Uses PaddleOCR with 8 preprocessing variants and a scoring system to pick the
best candidate. Optimized for CPU-only Linux containers.
"""

import re
import logging
import cv2
import numpy as np
from paddleocr import PaddleOCR

logger = logging.getLogger(__name__)

MIN_SN_LENGTH = 8
MAX_SN_LENGTH = 25
SN_PATTERN = re.compile(r"^[A-Z0-9]+$")


class SNExtractor:
    """Extract serial numbers from cropped modem label images."""

    def __init__(self):
        self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        logger.info("[SNExtractor] PaddleOCR engine initialized")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, image: np.ndarray) -> dict:
        """
        Extract serial number from a cropped image.

        Args:
            image: BGR or grayscale crop from YOLO detection.

        Returns:
            {
                "sn": str | None,          # extracted SN or None
                "confidence": float,       # 0.0 - 1.0
                "strategy": str,           # which preprocessing won
                "processed_image": np.ndarray,  # image used for final result
            }
        """
        if image is None or image.size == 0:
            return {"sn": None, "confidence": 0.0, "strategy": "none", "processed_image": image}

        variants = self._generate_variants(image)
        best = {"sn": None, "confidence": 0.0, "strategy": "none", "score": -1, "processed_image": image}

        for name, variant_img in variants:
            candidates = self._run_ocr(variant_img)
            for text, ocr_conf in candidates:
                cleaned = self._post_process(text)
                if not cleaned:
                    continue
                score = self._score_candidate(cleaned, ocr_conf)
                logger.debug(f"  [{name}] raw='{text}' cleaned='{cleaned}' ocr_conf={ocr_conf:.3f} score={score:.3f}")
                if score > best["score"]:
                    best = {
                        "sn": cleaned,
                        "confidence": ocr_conf,
                        "strategy": name,
                        "score": score,
                        "processed_image": variant_img,
                    }

        logger.info(f"[SNExtractor] Best: sn='{best['sn']}' strategy='{best['strategy']}' conf={best['confidence']:.3f}")
        return {
            "sn": best["sn"],
            "confidence": best["confidence"],
            "strategy": best["strategy"],
            "processed_image": best["processed_image"],
        }

    # ------------------------------------------------------------------
    # Preprocessing variants
    # ------------------------------------------------------------------

    def _generate_variants(self, image: np.ndarray) -> list:
        """
        Generate preprocessing variants of the input image.
        Automatically adds aggressive upscaling for small crops.
        """
        variants = []
        h, w = image.shape[:2]

        # Auto-upscale small crops first
        work = image.copy()
        if min(h, w) < 50:
            scale = max(1, 200 // min(h, w))
            work = cv2.resize(work, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            logger.debug(f"  [PREP] Upscaled {scale}x ({w}x{h} -> {work.shape[1]}x{work.shape[0]})")

        # 1. Original (passthrough)
        variants.append(("original", work.copy()))

        gray = self._to_gray(work)

        # 2. Grayscale
        variants.append(("grayscale", cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)))

        # 3. CLAHE enhanced
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        clahe_img = clahe.apply(gray)
        variants.append(("clahe", cv2.cvtColor(clahe_img, cv2.COLOR_GRAY2BGR)))

        # 4. Otsu binarization
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(("otsu", cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR)))

        # 5. Adaptive threshold
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        adaptive = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 4
        )
        variants.append(("adaptive", cv2.cvtColor(adaptive, cv2.COLOR_GRAY2BGR)))

        # 6. Sharpened
        kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
        sharpened = cv2.filter2D(gray, -1, kernel)
        variants.append(("sharpen", cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)))

        # 7. 2x further upscaled
        wh, ww = work.shape[:2]
        upscaled_2x = cv2.resize(work, (ww * 2, wh * 2), interpolation=cv2.INTER_CUBIC)
        variants.append(("upscale_2x", upscaled_2x))

        # 8. 4x further upscaled
        upscaled_4x = cv2.resize(work, (ww * 4, wh * 4), interpolation=cv2.INTER_CUBIC)
        variants.append(("upscale_4x", upscaled_4x))

        return variants

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    def _run_ocr(self, image: np.ndarray) -> list:
        """
        Run PaddleOCR on image, return list of (text, confidence) tuples.
        """
        try:
            result = self._ocr.ocr(img=image, cls=True)
            if not result or not result[0]:
                return []
            return [(line[1][0], line[1][1]) for line in result[0] if line[1][0] and line[1][0].strip()]
        except Exception as e:
            logger.debug(f"  [OCR] error: {e}")
            return []

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_candidate(self, text: str, ocr_conf: float) -> float:
        """
        Score a candidate SN string. Higher = better.

        Criteria:
        - Length in valid range (8-25) — strong preference for longer
        - Alphanumeric purity (no special chars)
        - OCR confidence
        - Bonus for matching expected SN patterns
        """
        score = 0.0
        length = len(text)

        # Length score: strongly prefer longer results (serial numbers are 10-20 chars)
        if length >= 10:
            score += 40.0 + (length * 2.0)
        elif MIN_SN_LENGTH <= length < 10:
            score += 25.0 + length
        elif 5 <= length < MIN_SN_LENGTH:
            score += 10.0
        else:
            score += 0.0

        # Alphanumeric purity
        alnum_ratio = sum(c.isalnum() for c in text) / max(length, 1)
        score += alnum_ratio * 15.0

        # No spaces bonus
        if " " not in text:
            score += 10.0

        # OCR confidence contribution
        score += ocr_conf * 20.0

        # Pattern match bonus: starts with letters, contains mix of letters+digits
        if re.match(r"^[A-Z]{2,}", text) and re.search(r"\d", text):
            score += 10.0

        return score

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def _post_process(self, text: str) -> str:
        """
        Clean extracted text for SN output.

        - Remove spaces and special characters
        - Convert to uppercase
        - Keep only [A-Z0-9]
        - Validate length
        """
        if not text:
            return ""

        cleaned = text.strip().upper()
        cleaned = re.sub(r"[^A-Z0-9]", "", cleaned)

        if len(cleaned) < MIN_SN_LENGTH:
            return ""

        return cleaned

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_gray(image: np.ndarray) -> np.ndarray:
        if len(image.shape) == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return image.copy()


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Extract serial number from modem label image")
    parser.add_argument("image", help="Path to input image (or crop)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    image = cv2.imread(args.image)
    if image is None:
        print(f"Error: cannot read image {args.image}", file=sys.stderr)
        sys.exit(1)

    extractor = SNExtractor()
    result = extractor.extract(image)

    print(f"\nSN:          {result['sn'] or '(not found)'}")
    print(f"Confidence:  {result['confidence']:.3f}")
    print(f"Strategy:    {result['strategy']}")


if __name__ == "__main__":
    main()

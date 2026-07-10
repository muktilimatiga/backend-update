"""
PaddleOCR Cloud API client.

Submits images to PaddleOCR's cloud API for OCR,
polls for results, and returns extracted text.
"""

import json
import logging
import os
import re
import time

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
POLL_INTERVAL = 5
MIN_DIMENSION = 50
UPSCALE_TARGET = 200


class PaddleOCRClient:
    """Client for PaddleOCR cloud API."""

    def __init__(
        self,
        token: str | None = None,
        model: str = DEFAULT_MODEL,
        api_url: str = DEFAULT_API_URL,
    ):
        self.token = token or os.getenv("PADDLE_OCR_API_TOKEN", "")
        self.model = model or os.getenv("PADDLE_OCR_MODEL", DEFAULT_MODEL)
        self.api_url = api_url
        self.headers = {"Authorization": f"bearer {self.token}"}

        if not self.token:
            logger.warning("[PaddleAPI] No API token configured")

    def ocr_image(self, image: np.ndarray, timeout: int = 120) -> str:
        """
        Run OCR on an image (numpy array) via the cloud API.

        Args:
            image: BGR image (numpy array).
            timeout: Maximum seconds to wait for job completion.

        Returns:
            Extracted text string, or empty string on failure.
        """
        image = self._ensure_minimum_size(image)

        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            logger.error("[PaddleAPI] Failed to encode image")
            return ""

        file_bytes = buf.tobytes()
        logger.info(f"[PaddleAPI] Sending image: {image.shape[1]}x{image.shape[0]}, {len(file_bytes):,} bytes")
        job_id = self._submit_job(file_bytes)
        if not job_id:
            return ""

        result_url = self._poll_job(job_id, timeout=timeout)
        if not result_url:
            return ""

        return self._fetch_and_parse(result_url)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_minimum_size(image: np.ndarray) -> np.ndarray:
        """Rotate portrait crops to landscape and upscale small crops."""
        h, w = image.shape[:2]
        if h > w:
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
            h, w = image.shape[:2]
            logger.info(f"[PaddleAPI] Rotated crop to landscape ({w}x{h})")
        if min(h, w) < MIN_DIMENSION:
            scale = max(1, UPSCALE_TARGET // min(h, w))
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            logger.info(f"[PaddleAPI] Upscaled crop {scale}x ({w}x{h} -> {image.shape[1]}x{image.shape[0]})")
        return image

    def _submit_job(self, file_bytes: bytes) -> str | None:
        """Submit an image to the API and return the job ID."""
        data = {
            "model": self.model,
            "optionalPayload": json.dumps({
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            }),
        }
        files = {"file": ("image.jpg", file_bytes, "image/jpeg")}

        try:
            resp = requests.post(
                self.api_url, headers=self.headers, data=data, files=files, timeout=30
            )
            if resp.status_code != 200:
                logger.error(f"[PaddleAPI] Submit failed ({resp.status_code}): {resp.text[:200]}")
                return None
            job_id = resp.json()["data"]["jobId"]
            logger.info(f"[PaddleAPI] Job submitted: {job_id}")
            return job_id
        except Exception as e:
            logger.error(f"[PaddleAPI] Submit error: {e}")
            return None

    def _poll_job(self, job_id: str, timeout: int = 120) -> str | None:
        """Poll until the job completes. Returns the result JSONL URL."""
        url = f"{self.api_url}/{job_id}"
        elapsed = 0

        while elapsed < timeout:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"[PaddleAPI] Poll failed ({resp.status_code})")
                    return None

                data = resp.json()["data"]
                state = data["state"]

                if state == "done":
                    json_url = data["resultUrl"]["jsonUrl"]
                    logger.info(f"[PaddleAPI] Job done: {job_id}")
                    return json_url
                elif state == "failed":
                    error_msg = data.get("errorMsg", "unknown")
                    logger.error(f"[PaddleAPI] Job failed: {error_msg}")
                    return None
                else:
                    progress = data.get("extractProgress", {})
                    total = progress.get("totalPages", "?")
                    done = progress.get("extractedPages", "?")
                    logger.debug(f"[PaddleAPI] {state} ({done}/{total})")

            except Exception as e:
                logger.error(f"[PaddleAPI] Poll error: {e}")
                return None

            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        logger.error(f"[PaddleAPI] Timeout after {timeout}s for job {job_id}")
        return None

    def _fetch_and_parse(self, json_url: str) -> str:
        """Download the JSONL result and extract plain text."""
        try:
            resp = requests.get(json_url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[PaddleAPI] Failed to fetch result: {e}")
            return ""

        texts = []
        for line in resp.text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line)["result"]
                for page in result.get("layoutParsingResults", []):
                    md_text = page.get("markdown", {}).get("text", "")
                    texts.append(self._markdown_to_text(md_text))
            except (json.JSONDecodeError, KeyError) as e:
                logger.debug(f"[PaddleAPI] Parse skip: {e}")
                continue

        return "\n".join(texts).strip()

    @staticmethod
    def _markdown_to_text(md: str) -> str:
        """Strip markdown formatting, return plain text."""
        text = md
        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"[*_`~]", "", text)
        text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{2,}", "\n", text)
        return text.strip()

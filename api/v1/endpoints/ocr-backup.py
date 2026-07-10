from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import PlainTextResponse
from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract
import io
import asyncio
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import numpy as np

from services.new_ocr import (
    get_headers,
    get_optional_payload,
    submit_and_poll_ocr_job,
    ocr_text_is_empty,
    get_rotated_image_bytes,
    extract_ocr_fields,
    extract_ps_from_ocr_only,
    extract_ps_barcode_fallback,
    scan_barcodes,
)
from core import settings, OLT_OPTIONS, OLT_ALIASES
from services.connection_manager import olt_manager
from schemas.config_handler import UnconfiguredOnt, OcrValidateResponse, OcrResult


# Configure Tesseract path based on OS
def _configure_tesseract():
    """Configure Tesseract path and verify installation."""
    if os.name == "nt":  # Windows
        tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(tesseract_path):
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            return True
    else:  # Linux/Unix
        # Check common Linux paths
        linux_paths = [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/opt/tesseract/bin/tesseract",
        ]
        for path in linux_paths:
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                return True

        # Try to find tesseract in PATH
        tesseract_in_path = shutil.which("tesseract")
        if tesseract_in_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_in_path
            return True

    return False


def _verify_tesseract():
    """Verify Tesseract is working and return version info."""
    try:
        version = pytesseract.get_tesseract_version()
        print(f"[OCR] Tesseract version: {version}")
        return True
    except pytesseract.TesseractNotFoundError:
        print("[OCR] ERROR: Tesseract not found!")
        print("[OCR] Install with: sudo apt-get install tesseract-ocr")
        return False
    except Exception as e:
        print(f"[OCR] ERROR verifying Tesseract: {e}")
        return False


# Configure and verify on startup
_tesseract_configured = _configure_tesseract()
_tesseract_available = _verify_tesseract()

router = APIRouter()

# Create a thread pool for OCR tasks
_ocr_executor = ThreadPoolExecutor(max_workers=2)

# Supported text file extensions for direct reading
TEXT_FILE_EXTENSIONS = {
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".css",
    ".sh",
    ".bash",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".go",
    ".rs",
    ".rb",
    ".php",
}


def _process_image_ocr(image_bytes: bytes, lang: str = "eng") -> str:
    """
    Standard OCR processing. Simple and fast.
    """
    image = Image.open(io.BytesIO(image_bytes))

    # --- PREPROCESSING ---
    # Convert RGBA to RGB (remove alpha channel)
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        image = background

    # Convert to Grayscale
    image = image.convert("L")

    # Smart invert: only invert if image is dark (dark mode screenshots)
    # This is a safe and simple check
    img_array = np.array(image)
    avg_brightness = np.mean(img_array)
    if avg_brightness < 128:
        image = ImageOps.invert(image)

    # --- OCR CONFIG ---
    # We try PSM 3 (Auto) first. If that returns nothing, we try PSM 6 (Block).
    # This covers 99% of use cases without over-engineering.

    configs = [
        r"--oem 3 --psm 3",  # Default: Fully automatic
        r"--oem 3 --psm 6",  # Fallback: Uniform block of text
    ]

    for config in configs:
        try:
            text = pytesseract.image_to_string(
                image, lang=lang, config=config, timeout=30
            )

            if text and text.strip():
                return text.strip()
        except Exception:
            continue

    return ""


@router.post("/ocr")
async def extract_text(file: UploadFile = File(...)):
    """
    Upload an image and extract text using OCR.
    Runs in a separate process to avoid blocking the event loop.
    """
    # Check if Tesseract is available
    if not _tesseract_available:
        raise HTTPException(
            status_code=503,
            detail="Tesseract OCR is not installed. Install with: sudo apt-get install tesseract-ocr",
        )

    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        # Read file bytes
        contents = await file.read()

        # Run OCR in thread pool (non-blocking)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(_ocr_executor, _process_image_ocr, contents)

        return PlainTextResponse(content=text)

    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Invalid image format")
    except pytesseract.TesseractNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Tesseract OCR not found. Install with: sudo apt-get install tesseract-ocr",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Tesseract failed: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/read-file")
def read_text_file(file: UploadFile = File(...)):
    """
    Read text content directly from a file (no OCR needed).
    """
    # Check file extension
    filename = file.filename or ""
    ext = "." + filename.split(".")[-1].lower() if "." in filename else ""

    if ext not in TEXT_FILE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Supported: {', '.join(sorted(TEXT_FILE_EXTENSIONS))}",
        )

    try:
        contents = file.file.read()
        text = contents.decode("utf-8")

        return {
            "filename": file.filename,
            "text": text,
            "lines": len(text.splitlines()),
            "status": "success",
        }
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ocr-sn")
async def scan_ocr(file: UploadFile = File(...)):
    """
    Upload an image and extract P-SN (PON Serial Number) via OCR + barcode fallback.

    Pipeline:
      1. Submit image to PaddleOCR API, poll for results.
      2. Extract P-SN from OCR text via pattern matching.
      3. If OCR yields no P-SN, run barcode scan as fallback.

    Returns JSON with: psn, device_type, confidence, method, ocr_fields, attempts.
    """
    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Supported: JPEG, PNG, WebP")

    tmp_path = None
    try:
        contents = await file.read()
        filename = file.filename or "uploaded_image.jpg"

        # Save to temp file for OpenCV/barcode processing
        suffix = os.path.splitext(filename)[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        headers = get_headers()
        optional_payload = get_optional_payload()

        # Step 1: Submit OCR job (original orientation)
        jsonl_url = submit_and_poll_ocr_job(contents, filename, headers, optional_payload)
        if jsonl_url is None:
            raise HTTPException(status_code=502, detail="OCR API job failed")

        # Step 2: Download and parse OCR results
        import requests as _requests
        jsonl_response = _requests.get(jsonl_url)
        jsonl_response.raise_for_status()
        lines = [l for l in jsonl_response.text.strip().split("\n") if l.strip()]
        if not lines:
            raise HTTPException(status_code=502, detail="Empty OCR response")

        ocr_result_obj = json.loads(lines[0])["result"]
        layout_results = ocr_result_obj["layoutParsingResults"]
        if not layout_results:
            raise HTTPException(status_code=502, detail="No layout results from OCR")

        res = layout_results[0]
        markdown_text = res["markdown"]["text"]

        # Step 2b: Rotation retry when OCR text is empty (upside-down labels)
        rotation_used = 0
        if ocr_text_is_empty(markdown_text):
            for angle in [180, 90, 270]:
                rot_bytes = get_rotated_image_bytes(tmp_path, angle)
                rot_url = submit_and_poll_ocr_job(rot_bytes, f"rotated_{angle}_{filename}", headers, optional_payload)
                if rot_url is None:
                    continue
                rot_response = _requests.get(rot_url)
                rot_response.raise_for_status()
                rot_lines = [l for l in rot_response.text.strip().split("\n") if l.strip()]
                if not rot_lines:
                    continue
                rot_result = json.loads(rot_lines[0])["result"]
                rot_layout = rot_result["layoutParsingResults"]
                if not rot_layout:
                    continue
                rot_md = rot_layout[0]["markdown"]["text"]
                if not ocr_text_is_empty(rot_md):
                    res = rot_layout[0]
                    markdown_text = rot_md
                    rotation_used = angle
                    break

        # Step 3: Extract fields from OCR text
        ocr_fields = extract_ocr_fields(markdown_text)

        # Step 4: Extract P-SN from OCR text (primary)
        psn_result = extract_ps_from_ocr_only(markdown_text, tmp_path)

        # Step 5: Barcode fallback if OCR found no P-SN
        if not psn_result["psn"]:
            barcode_psn_result = extract_ps_barcode_fallback(tmp_path)
            psn_result["attempts"].extend(barcode_psn_result["attempts"])
            if barcode_psn_result["psn"]:
                psn_result.update({
                    "psn": barcode_psn_result["psn"],
                    "device_type": barcode_psn_result["device_type"],
                    "confidence": barcode_psn_result["confidence"],
                    "method": barcode_psn_result["method"],
                })

        return {
            "psn": psn_result["psn"],
            "device_type": psn_result["device_type"],
            "confidence": psn_result["confidence"],
            "method": psn_result["method"],
            "ocr_fields": ocr_fields,
            "rotation_applied": rotation_used,
            "attempts": psn_result["attempts"],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


async def _scan_all_onts() -> list:
    """Scan all OLTs in parallel for unconfigured ONTs."""

    async def _scan_one(olt_name: str, olt_info: dict):
        try:
            handler = await asyncio.wait_for(
                olt_manager.get_connection(
                    host=olt_info["ip"],
                    username=settings.OLT_USERNAME,
                    password=settings.OLT_PASSWORD,
                    is_c600=olt_info["c600"],
                    olt_name=olt_name,
                ),
                timeout=30,
            )
            ont_list = await asyncio.wait_for(
                handler.find_unconfigured_onts(),
                timeout=60,
            )
            for ont in ont_list:
                ont.olt_name = olt_name
            return ont_list
        except Exception:
            if olt_info["ip"] in olt_manager._connections:
                del olt_manager._connections[olt_info["ip"]]
            return []

    tasks = [_scan_one(name, info) for name, info in OLT_OPTIONS.items()]
    results = await asyncio.gather(*tasks)
    return [ont for batch in results for ont in batch]


def _match_psn_to_ont(psn: str, ont_list: list) -> tuple:
    """
    Match extracted P-SN against detected ONTs.
    Returns (matched_ont or None, match_found bool).
    """
    if not psn:
        return None, False

    psn_normalized = psn.upper().strip()
    for ont in ont_list:
        ont_sn_normalized = ont.sn.upper().strip()
        if psn_normalized == ont_sn_normalized:
            return ont, True

    return None, False


@router.post("/ocr-validate-sn", response_model=OcrValidateResponse)
async def validate_ocr_sn(file: UploadFile = File(...)):
    """
    Upload image → extract P-SN via OCR → match against detected ONTs from all OLTs.

    Flow:
      1. Extract P-SN from image using OCR + barcode fallback.
      2. Scan all OLTs in parallel for unconfigured ONTs.
      3. Match P-SN against detected ONTs' sn field.
      4. If matched → return matched ONT.
      5. If no match → return all detected ONTs for manual selection.
    """
    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Supported: JPEG, PNG, WebP")

    tmp_path = None
    try:
        contents = await file.read()
        filename = file.filename or "uploaded_image.jpg"

        # Save to temp file for OpenCV/barcode processing
        suffix = os.path.splitext(filename)[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        headers = get_headers()
        optional_payload = get_optional_payload()

        # --- OCR Pipeline (same as /ocr-sn) ---
        jsonl_url = submit_and_poll_ocr_job(contents, filename, headers, optional_payload)
        if jsonl_url is None:
            raise HTTPException(status_code=502, detail="OCR API job failed")

        import requests as _requests
        jsonl_response = _requests.get(jsonl_url)
        jsonl_response.raise_for_status()
        lines = [l for l in jsonl_response.text.strip().split("\n") if l.strip()]
        if not lines:
            raise HTTPException(status_code=502, detail="Empty OCR response")

        ocr_result_obj = json.loads(lines[0])["result"]
        layout_results = ocr_result_obj["layoutParsingResults"]
        if not layout_results:
            raise HTTPException(status_code=502, detail="No layout results from OCR")

        res = layout_results[0]
        markdown_text = res["markdown"]["text"]

        # Rotation retry
        rotation_used = 0
        if ocr_text_is_empty(markdown_text):
            for angle in [180, 90, 270]:
                rot_bytes = get_rotated_image_bytes(tmp_path, angle)
                rot_url = submit_and_poll_ocr_job(rot_bytes, f"rotated_{angle}_{filename}", headers, optional_payload)
                if rot_url is None:
                    continue
                rot_response = _requests.get(rot_url)
                rot_response.raise_for_status()
                rot_lines = [l for l in rot_response.text.strip().split("\n") if l.strip()]
                if not rot_lines:
                    continue
                rot_result = json.loads(rot_lines[0])["result"]
                rot_layout = rot_result["layoutParsingResults"]
                if not rot_layout:
                    continue
                rot_md = rot_layout[0]["markdown"]["text"]
                if not ocr_text_is_empty(rot_md):
                    res = rot_layout[0]
                    markdown_text = rot_md
                    rotation_used = angle
                    break

        ocr_fields = extract_ocr_fields(markdown_text)
        psn_result = extract_ps_from_ocr_only(markdown_text, tmp_path)

        if not psn_result["psn"]:
            barcode_psn_result = extract_ps_barcode_fallback(tmp_path)
            psn_result["attempts"].extend(barcode_psn_result["attempts"])
            if barcode_psn_result["psn"]:
                psn_result.update({
                    "psn": barcode_psn_result["psn"],
                    "device_type": barcode_psn_result["device_type"],
                    "confidence": barcode_psn_result["confidence"],
                    "method": barcode_psn_result["method"],
                })

        ocr_result = OcrResult(
            psn=psn_result["psn"],
            device_type=psn_result["device_type"],
            confidence=psn_result["confidence"],
            method=psn_result["method"],
            ocr_fields=ocr_fields,
            rotation_applied=rotation_used,
            attempts=psn_result["attempts"],
        )

        # --- Scan all OLTs for unconfigured ONTs ---
        all_onts = await _scan_all_onts()

        # --- Match P-SN against detected ONTs ---
        matched_ont, match_found = _match_psn_to_ont(psn_result["psn"], all_onts)

        return OcrValidateResponse(
            ocr_result=ocr_result,
            matched_ont=matched_ont,
            all_onts=all_onts,
            match_found=match_found,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR validate failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
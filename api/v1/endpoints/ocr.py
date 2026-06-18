from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
import io
import asyncio
import os
import shutil
import numpy as np
from concurrent.futures import ThreadPoolExecutor

# --- Engine detection ---
_paddle_available = False
_tesseract_available = False
_paddle_ocr = None

try:
    from paddleocr import PaddleOCR

    _paddle_ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    _paddle_available = True
    print("[OCR] PaddleOCR engine loaded")
except ImportError:
    print("[OCR] PaddleOCR not available (install paddleocr + paddlepaddle on Linux)")
except Exception as e:
    print(f"[OCR] PaddleOCR init error: {e}")

if not _paddle_available:
    try:
        import pytesseract

        # Configure Tesseract path
        if os.name == "nt":
            tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.exists(tesseract_path):
                pytesseract.pytesseract.tesseract_cmd = tesseract_path
        else:
            for path in ["/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/tesseract/bin/tesseract"]:
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    break
            else:
                tesseract_in_path = shutil.which("tesseract")
                if tesseract_in_path:
                    pytesseract.pytesseract.tesseract_cmd = tesseract_in_path

        version = pytesseract.get_tesseract_version()
        _tesseract_available = True
        print(f"[OCR] Tesseract fallback loaded (v{version})")
    except Exception as e:
        print(f"[OCR] Tesseract not available: {e}")

if not _paddle_available and not _tesseract_available:
    print("[OCR] WARNING: No OCR engine available!")

# Lazy-load YOLO+PaddleOCR pipeline
_new_ocr_module = None


def _init_new_ocr():
    global _new_ocr_module
    if not _paddle_available:
        return
    try:
        from services.new_ocr import run_pipeline_bytes
        _new_ocr_module = run_pipeline_bytes
        print("[OCR] YOLO+PaddleOCR pipeline loaded")
    except ImportError as e:
        print(f"[OCR] Could not load new_ocr pipeline: {e}")
    except Exception as e:
        print(f"[OCR] Pipeline init error: {e}")


router = APIRouter()

_ocr_executor = ThreadPoolExecutor(max_workers=2)

TEXT_FILE_EXTENSIONS = {
    ".txt", ".py", ".js", ".ts", ".jsx", ".tsx", ".md",
    ".json", ".yaml", ".yml", ".xml", ".html", ".css",
    ".sh", ".bash", ".java", ".c", ".cpp", ".h", ".go",
    ".rs", ".rb", ".php",
}


def _preprocess_image(image_bytes: bytes) -> Image.Image:
    """Common preprocessing: RGBA->RGB, grayscale, smart invert."""
    image = Image.open(io.BytesIO(image_bytes))

    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        image = background

    image = image.convert("L")

    img_array = np.array(image)
    avg_brightness = np.mean(img_array)
    if avg_brightness < 128:
        image = ImageOps.invert(image)

    return image


def _ocr_with_paddleocr(image_bytes: bytes) -> str:
    """Simple OCR using PaddleOCR."""
    image = _preprocess_image(image_bytes)
    image = image.convert("RGB")
    img_array = np.array(image)

    try:
        result = _paddle_ocr.ocr(img_array, cls=True)
        if not result or not result[0]:
            return ""
        texts = []
        for line in result[0]:
            if line and len(line) >= 2:
                text = line[1][0]
                if text and text.strip():
                    texts.append(text.strip())
        return " ".join(texts)
    except Exception as e:
        print(f"[OCR] PaddleOCR error: {e}")
        return ""


def _ocr_with_tesseract(image_bytes: bytes) -> str:
    """Simple OCR using Tesseract (fallback)."""
    image = _preprocess_image(image_bytes)

    import pytesseract

    configs = [
        r"--oem 3 --psm 3",
        r"--oem 3 --psm 6",
    ]

    for config in configs:
        try:
            text = pytesseract.image_to_string(image, lang="eng", config=config, timeout=30)
            if text and text.strip():
                return text.strip()
        except Exception:
            continue

    return ""


def _process_image_ocr(image_bytes: bytes, lang: str = "en") -> str:
    """OCR text extraction using best available engine."""
    if _paddle_available:
        return _ocr_with_paddleocr(image_bytes)
    elif _tesseract_available:
        return _ocr_with_tesseract(image_bytes)
    return ""


def _process_image_yolo_ocr(image_bytes: bytes) -> dict:
    """Full YOLO+PaddleOCR pipeline. Requires PaddleOCR."""
    global _new_ocr_module

    if not _paddle_available:
        return {"error": "PaddleOCR not available", "modem_type": None, "sn": None}

    if _new_ocr_module is None:
        _init_new_ocr()

    if _new_ocr_module is None:
        return {"error": "YOLO pipeline not loaded", "modem_type": None, "sn": None}

    try:
        return _new_ocr_module(image_bytes)
    except Exception as e:
        print(f"[OCR] Pipeline error: {e}")
        return {"error": str(e), "modem_type": None, "sn": None}


@router.post("/ocr")
async def extract_text(file: UploadFile = File(...)):
    """
    Upload an image and extract text using OCR.
    Uses PaddleOCR on Linux, Tesseract fallback on macOS Intel.
    """
    if not _paddle_available and not _tesseract_available:
        raise HTTPException(
            status_code=503,
            detail="No OCR engine available. Install paddleocr+paddlepaddle (Linux) or pytesseract+Tesseract.",
        )

    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        contents = await file.read()
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(_ocr_executor, _process_image_ocr, contents)
        return PlainTextResponse(content=text)
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Invalid image format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ocr/detect")
async def detect_modem(file: UploadFile = File(...)):
    """
    Upload an image and detect modem type + serial number using YOLO+PaddleOCR.
    Requires PaddleOCR (Linux or macOS arm64).
    """
    if not _paddle_available:
        raise HTTPException(
            status_code=503,
            detail="YOLO detection requires PaddleOCR. Install paddleocr+paddlepaddle (Linux only).",
        )

    if file.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        contents = await file.read()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_ocr_executor, _process_image_yolo_ocr, contents)

        if "error" in result and result["error"]:
            raise HTTPException(status_code=500, detail=result["error"])

        return JSONResponse(content=result)
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Invalid image format")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/read-file")
def read_text_file(file: UploadFile = File(...)):
    """
    Read text content directly from a file (no OCR needed).
    """
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

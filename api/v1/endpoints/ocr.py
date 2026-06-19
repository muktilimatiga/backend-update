from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
import io
import asyncio
import os
import numpy as np
from concurrent.futures import ThreadPoolExecutor

# --- Engine detection ---
_rapidocr_engine = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _rapidocr_engine = RapidOCR()
    print("[OCR] RapidOCR engine loaded")
except Exception as e:
    print(f"[OCR] RapidOCR not available: {e}")

if not _rapidocr_engine:
    print("[OCR] WARNING: No OCR engine available!")

# Lazy-load YOLO+RapidOCR pipeline
_new_ocr_module = None


def _init_new_ocr():
    global _new_ocr_module
    try:
        from services.new_ocr import run_pipeline_bytes
        _new_ocr_module = run_pipeline_bytes
        print("[OCR] YOLO+RapidOCR pipeline loaded")
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


def _ocr_with_rapidocr(image_bytes: bytes) -> str:
    """Simple OCR using RapidOCR."""
    image = _preprocess_image(image_bytes)
    image = image.convert("RGB")
    img_array = np.array(image)

    try:
        result, _elapse = _rapidocr_engine(img_array)
        if not result:
            return ""
        texts = []
        for item in result:
            text = item[1]
            confidence = item[2]
            if text and text.strip() and confidence > 0.2:
                texts.append(text.strip())
        return " ".join(texts)
    except Exception as e:
        print(f"[OCR] RapidOCR error: {e}")
        return ""


def _process_image_ocr(image_bytes: bytes, lang: str = "en") -> str:
    """OCR text extraction using RapidOCR."""
    if _rapidocr_engine:
        return _ocr_with_rapidocr(image_bytes)
    return ""


def _process_image_yolo_ocr(image_bytes: bytes) -> dict:
    """Full YOLO+RapidOCR pipeline."""
    global _new_ocr_module

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
    Uses RapidOCR engine.
    """
    if not _rapidocr_engine:
        raise HTTPException(
            status_code=503,
            detail="No OCR engine available. Install rapidocr-onnxruntime.",
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
    Upload an image and detect modem type + serial number using YOLO+RapidOCR.
    Requires RapidOCR.
    """
    if not _rapidocr_engine:
        raise HTTPException(
            status_code=503,
            detail="YOLO detection requires RapidOCR. Install rapidocr-onnxruntime.",
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
        raise HTTPException(status_code=400, detail="File is not valid UTF-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

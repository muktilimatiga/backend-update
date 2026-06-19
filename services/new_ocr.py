"""
YOLO + PaddleOCR pipeline for modem type and serial number detection.

Uses YOLO to detect regions on modem labels, then:
- Logo regions (C-DATA, F670L): trust YOLO class name directly
- SN regions: use SNExtractor for multi-variant OCR + scoring
"""

import sys
import os
import argparse
import logging
import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "best.pt")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debug_crops")

DISPLAY_NAMES = {
    "modem_type": "C-DATA",
    "modem_type_1": "F670L",
    "sn": "SN",
}

LOGO_CLASSES = {"modem_type", "modem_type_1"}


# ------------------------------------------------------------------
# YOLO
# ------------------------------------------------------------------

def init_yolo(model_path: str = MODEL_PATH) -> YOLO:
    model = YOLO(model_path)
    logger.info(f"[YOLO] Model loaded: {model_path}")
    logger.info(f"[YOLO] Classes: {model.names}")
    return model


def detect_text_regions(yolo: YOLO, image: np.ndarray, conf_threshold: float = 0.25):
    scales = [1, 2, 4]
    all_detections = []

    for scale in scales:
        if scale > 1:
            scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        else:
            scaled = image

        results = yolo.predict(source=scaled, conf=conf_threshold, verbose=False)

        for r in results:
            boxes = r.boxes
            if boxes is None:
                continue
            for i in range(len(boxes)):
                x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy().astype(int)
                conf = float(boxes.conf[i].cpu().numpy())
                cls_id = int(boxes.cls[i].cpu().numpy())
                cls_name = yolo.names.get(cls_id, str(cls_id))

                if scale > 1:
                    x1, y1, x2, y2 = int(x1 / scale), int(y1 / scale), int(x2 / scale), int(y2 / scale)

                crop = image[max(0, y1):y2, max(0, x1):x2]
                if crop.size == 0:
                    continue

                all_detections.append({
                    "bbox": (x1, y1, x2, y2),
                    "confidence": conf,
                    "class_id": cls_id,
                    "class_name": cls_name,
                    "display_name": DISPLAY_NAMES.get(cls_name, cls_name),
                    "crop": crop,
                })

        if all_detections:
            logger.info(f"[YOLO] Detected {len(all_detections)} region(s) at scale {scale}x")
            break

    if not all_detections:
        logger.info("[YOLO] No regions detected at any scale")

    return all_detections


# ------------------------------------------------------------------
# Image utils
# ------------------------------------------------------------------

def auto_rotate(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    if h > w:
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        logger.info(f"[ROTATE] Rotated 90° CW ({w}x{h} -> {image.shape[1]}x{image.shape[0]})")
    return image


def save_debug_crops(detections: list):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    for i, det in enumerate(detections):
        crop_path = os.path.join(DEBUG_DIR, f"crop_{i+1}_{det['display_name']}.jpg")
        cv2.imwrite(crop_path, det["crop"])
        logger.debug(f"  [DEBUG] Saved crop: {crop_path}")


# ------------------------------------------------------------------
# Annotation
# ------------------------------------------------------------------

def annotate_image(image: np.ndarray, detections: list, all_texts: dict) -> np.ndarray:
    annotated = image.copy()

    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        display_name = det["display_name"]
        det_conf = det["confidence"]

        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)

        ocr_results = all_texts.get(display_name, [])
        if ocr_results:
            combined_text = " ".join([t["text"] for t in ocr_results])
            label = f"{display_name}: {combined_text}"
        else:
            label = f"{display_name} (conf: {det_conf:.2f})"

        font_scale = 0.6
        thickness = 2
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        cv2.rectangle(annotated, (x1, y1 - th - 10), (x1 + tw, y1), (0, 255, 0), -1)
        cv2.putText(annotated, label, (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness)

    return annotated


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------

def _run_pipeline_on_image(image: np.ndarray, model_path: str, conf: float, save_output: bool, image_path: str = "input"):
    from services.sn_extractor import SNExtractor

    image = auto_rotate(image)
    yolo = init_yolo(model_path)
    extractor = SNExtractor()

    detections = detect_text_regions(yolo, image, conf)
    save_debug_crops(detections)

    if not detections:
        logger.info("[INFO] No YOLO regions detected, running OCR on full image...")
        scale = max(1, 640 // max(image.shape[:2]))
        if scale > 1:
            upscaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            logger.info(f"[INFO] Upscaled image {scale}x ({upscaled.shape[1]}x{upscaled.shape[0]})")
        else:
            upscaled = image
        result = extractor.extract(upscaled)
        if result["sn"]:
            logger.info(f"  SN: {result['sn']}")
            return {"modem_type": None, "sn": result["sn"], "raw_results": {"full_image": [{"text": result["sn"], "confidence": result["confidence"], "strategy": result["strategy"]}]}}
        else:
            logger.info("[RESULT] No text detected.")
            return {"modem_type": None, "sn": None, "raw_results": {}}

    all_texts = {}

    for i, det in enumerate(detections):
        x1, y1, x2, y2 = det["bbox"]
        display_name = det["display_name"]
        class_name = det["class_name"]
        det_conf = det["confidence"]

        logger.info(f"\n--- Region {i+1}: {display_name} (YOLO conf: {det_conf:.3f}) ---")

        if class_name in LOGO_CLASSES:
            logger.info(f"    [OCR] Logo region, using YOLO class: {display_name}")
            all_texts[display_name] = [{"text": display_name, "confidence": det_conf, "strategy": "yolo_class"}]
        else:
            result = extractor.extract(det["crop"])
            if result["sn"]:
                all_texts[display_name] = [{"text": result["sn"], "confidence": result["confidence"], "strategy": result["strategy"]}]
                logger.info(f"    OCR: \"{result['sn']}\" (conf: {result['confidence']:.3f}, strategy: {result['strategy']})")
            else:
                all_texts[display_name] = []
                logger.info("    OCR: (no text detected)")

    modem_type = None
    modem_type_text = None
    sn = None
    for display_name, texts in all_texts.items():
        if display_name in ("C-DATA", "F670L"):
            modem_type = display_name
            modem_type_text = " ".join([t["text"] for t in texts]) if texts else None
        elif display_name == "SN":
            sn = " ".join([t["text"] for t in texts]) if texts else None

    logger.info(f"  MODEM_TYPE: {modem_type or 'F670L'}")
    logger.info(f"  SN: {sn or modem_type_text or '(none)'}")

    if save_output:
        annotated = annotate_image(image, detections, all_texts)
        output_path = os.path.splitext(image_path)[0] + "_ocr_result.jpg"
        cv2.imwrite(output_path, annotated)
        logger.info(f"\n[SAVED] Annotated image: {output_path}")

    return {
        "modem_type": modem_type or "F670L",
        "sn": sn or modem_type_text,
        "raw_results": all_texts,
    }


def run_pipeline(image_path: str, model_path: str = MODEL_PATH, conf: float = 0.25, save_output: bool = True):
    if not os.path.exists(image_path):
        logger.error(f"Image not found: {image_path}")
        sys.exit(1)

    image = cv2.imread(image_path)
    if image is None:
        logger.error(f"Failed to read image: {image_path}")
        sys.exit(1)

    logger.info(f"[INFO] Image: {image_path} ({image.shape[1]}x{image.shape[0]})")
    return _run_pipeline_on_image(image, model_path, conf, save_output, image_path)


def run_pipeline_bytes(image_bytes: bytes, model_path: str = MODEL_PATH, conf: float = 0.25) -> dict:
    """Run the YOLO+SNExtractor pipeline on raw image bytes."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image is None:
        return {"error": "Failed to decode image", "modem_type": None, "sn": None}
    return _run_pipeline_on_image(image, model_path, conf, save_output=False)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="YOLO + PaddleOCR modem detection pipeline")
    parser.add_argument("image", help="Path to input image")
    parser.add_argument("--model", default=MODEL_PATH, help="Path to YOLO .pt model")
    parser.add_argument("--conf", type=float, default=0.25, help="YOLO confidence threshold (default: 0.25)")
    parser.add_argument("--no-save", action="store_true", help="Don't save annotated output image")
    args = parser.parse_args()

    run_pipeline(args.image, model_path=args.model, conf=args.conf, save_output=not args.no_save)


if __name__ == "__main__":
    main()

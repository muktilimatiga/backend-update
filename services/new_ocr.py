import sys
import os
import argparse
import cv2
import numpy as np
from ultralytics import YOLO
from paddleocr import PaddleOCR


MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "best.pt")

DISPLAY_NAMES = {
    "modem_type": "C-DATA",
    "modem_type_1": "F670L",
    "sn": "SN",
}


def init_yolo(model_path: str = MODEL_PATH) -> YOLO:
    model = YOLO(model_path)
    print(f"[YOLO] Model loaded: {model_path}")
    print(f"[YOLO] Classes: {model.names}")
    return model


def init_paddleocr() -> PaddleOCR:
    ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    print("[PaddleOCR] Engine initialized")
    return ocr


def auto_rotate(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    if h > w:
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        print(f"[ROTATE] Rotated 90° CW ({w}x{h} -> {image.shape[1]}x{image.shape[0]})")
    return image


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
            print(f"[YOLO] Detected {len(all_detections)} region(s) at scale {scale}x")
            break

    if not all_detections:
        print("[YOLO] No regions detected at any scale")

    return all_detections


def read_text_paddleocr(ocr_engine: PaddleOCR, image: np.ndarray):
    h, w = image.shape[:2]
    if h < 50 and w < 50:
        scale = max(1, 100 // min(h, w))
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        print(f"    [OCR] Upscaled crop {scale}x for better detection")
    result = ocr_engine.ocr(img=image, cls=True)
    texts = []
    if result and result[0]:
        for line in result[0]:
            box, (text, score) = line
            texts.append({"text": text, "confidence": score})
    return texts


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


def run_pipeline(image_path: str, model_path: str = MODEL_PATH, conf: float = 0.25, save_output: bool = True):
    if not os.path.exists(image_path):
        print(f"[ERROR] Image not found: {image_path}")
        sys.exit(1)

    image = cv2.imread(image_path)
    if image is None:
        print(f"[ERROR] Failed to read image: {image_path}")
        sys.exit(1)

    print(f"[INFO] Image: {image_path} ({image.shape[1]}x{image.shape[0]})")

    return _run_pipeline_on_image(image, model_path, conf, save_output, image_path)


def run_pipeline_bytes(image_bytes: bytes, model_path: str = MODEL_PATH, conf: float = 0.25) -> dict:
    """Run the YOLO+PaddleOCR pipeline on raw image bytes. Returns structured dict."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image is None:
        return {"error": "Failed to decode image", "modem_type": None, "sn": None}

    return _run_pipeline_on_image(image, model_path, conf, save_output=False)


def _run_pipeline_on_image(image: np.ndarray, model_path: str, conf: float, save_output: bool, image_path: str = "input"):
    image = auto_rotate(image)

    yolo = init_yolo(model_path)
    ocr_engine = init_paddleocr()

    detections = detect_text_regions(yolo, image, conf)

    if not detections:
        print("[INFO] No YOLO regions detected, running OCR on full image...")
        scale = max(1, 640 // max(image.shape[:2]))
        if scale > 1:
            upscaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            print(f"[INFO] Upscaled image {scale}x for better OCR ({upscaled.shape[1]}x{upscaled.shape[0]})")
        else:
            upscaled = image
        ocr_results = read_text_paddleocr(ocr_engine, upscaled)
        if ocr_results:
            sn = ocr_results[0]["text"]
            print(f"  SN: {sn}")
            return {"modem_type": None, "sn": sn, "raw_results": ocr_results}
        else:
            print("[RESULT] No text detected.")
            return {"modem_type": None, "sn": None, "raw_results": []}

    all_texts = {}

    for i, det in enumerate(detections):
        x1, y1, x2, y2 = det["bbox"]
        display_name = det["display_name"]
        det_conf = det["confidence"]

        print(f"\n--- Region {i+1}: {display_name} (YOLO conf: {det_conf:.3f}) ---")

        ocr_results = read_text_paddleocr(ocr_engine, det["crop"])
        all_texts[display_name] = ocr_results

        if ocr_results:
            for j, t in enumerate(ocr_results):
                print(f"    OCR [{j+1}]: \"{t['text']}\" (conf: {t['confidence']:.3f})")
        else:
            print("    OCR: (no text detected)")

    modem_type = None
    modem_type_text = None
    sn = None
    for display_name, texts in all_texts.items():
        if display_name in ("C-DATA", "F670L"):
            modem_type = display_name
            modem_type_text = " ".join([t["text"] for t in texts]) if texts else None
        elif display_name == "SN":
            sn = " ".join([t["text"] for t in texts]) if texts else None

    print(f"  MODE_TYPE: {modem_type or 'F670L'}")
    print(f"  SN: {sn or modem_type_text or '(none)'}")

    if save_output:
        annotated = annotate_image(image, detections, all_texts)
        output_path = os.path.splitext(image_path)[0] + "_ocr_result.jpg"
        cv2.imwrite(output_path, annotated)
        print(f"\n[SAVED] Annotated image: {output_path}")

    return {
        "modem_type": modem_type or "F670L",
        "sn": sn or modem_type_text,
        "raw_results": all_texts,
    }


def main():
    parser = argparse.ArgumentParser(description="YOLO + PaddleOCR text detection pipeline")
    parser.add_argument("image", help="Path to input image")
    parser.add_argument("--model", default=MODEL_PATH, help="Path to YOLO .pt model")
    parser.add_argument("--conf", type=float, default=0.25, help="YOLO confidence threshold (default: 0.25)")
    parser.add_argument("--no-save", action="store_true", help="Don't save annotated output image")
    args = parser.parse_args()

    run_pipeline(args.image, model_path=args.model, conf=args.conf, save_output=not args.no_save)


if __name__ == "__main__":
    main()

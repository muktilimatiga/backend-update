# Please make sure the requests library is installed
# pip install requests
import json
import os
import re
import requests
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import cv2
import numpy as np
import zxingcpp  # type: ignore[import-untyped]

from core.config import settings

JOB_URL = settings.JOB_URL
TOKEN_API_OCR = settings.TOKEN_API_OCR
MODEL = settings.MODEL

# ============================================================================
# IMAGE PREPROCESSING MODULE
# ============================================================================

def enhance_contrast(image):
    """Enhance contrast using CLAHE (Contrast Limited Adaptive Histogram Equalization)"""
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR) if len(image.shape) == 3 else enhanced


def denoise_image(image):
    """Remove noise from image"""
    if len(image.shape) == 3:
        return cv2.fastNlMeansDenoisingColored(image, None, 10, 10, 7, 21)
    else:
        return cv2.fastNlMeansDenoising(image, None, 10, 7, 21)


def sharpen_image(image):
    """Sharpen image using unsharp mask"""
    blur = cv2.GaussianBlur(image, (0, 0), 3)
    return cv2.addWeighted(image, 1.5, blur, -0.5, 0)


def adaptive_threshold(image):
    """Apply adaptive thresholding for uneven lighting"""
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY, 11, 2)
    return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR) if len(image.shape) == 3 else thresh


def upscale_image(image, scale=2):
    """Upscale image for better text recognition"""
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def preprocess_image(image):
    """
    Create multiple enhanced versions of the image for better OCR.
    
    Returns list of (name, enhanced_image) tuples
    """
    versions = []
    
    # Version 0: Original
    versions.append(("original", image))
    
    # Version 1: Contrast enhanced
    contrast_img = enhance_contrast(image)
    versions.append(("contrast", contrast_img))
    
    # Version 2: Denoised
    denoised_img = denoise_image(image)
    versions.append(("denoised", denoised_img))
    
    # Version 3: Sharpened
    sharpened_img = sharpen_image(image)
    versions.append(("sharpened", sharpened_img))
    
    # Version 4: Adaptive threshold
    thresh_img = adaptive_threshold(image)
    versions.append(("threshold", thresh_img))
    
    # Version 5: Upscaled 2x
    upscaled_img = upscale_image(image, 2)
    versions.append(("upscaled_2x", upscaled_img))
    
    # Version 6: Contrast + Sharpened
    contrast_sharp = sharpen_image(contrast_img)
    versions.append(("contrast_sharp", contrast_sharp))
    
    return versions


# ============================================================================
# P-SN PATTERN MATCHING
# ============================================================================

def extract_psPattern(text):
    """
    Extract P-SN (PON Serial Number) using pattern matching.
    
    Returns: (psn_value, device_type) or (None, None)
    """
    # Remove HTML tags and clean up text
    clean_text = re.sub(r'<[^>]+>', '', text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    # C-DATA pattern: CDTC + alphanumeric (6+ chars)
    # Examples: CDTC507560C8, CDTC1D4208B8, CDTC507560F5
    cdata_patterns = [
        r'(CDTC[A-Za-z0-9]{6,})',
        r'(COTC[A-Za-z0-9]{6,})',  # Handle OCR errors (O instead of D)
        r'(CDT[A-Za-z0-9]{7,})',   # Partial match
        # P.SN or P-SN followed by value on same line
        r'P[\.\-]SN[:\s]+(CDT[A-Za-z0-9]+)',
        r'PON\s*SN[:\s]+(CDT[A-Za-z0-9]+)',
        # Handle multi-line: P.SN on one line, value on next
        r'P[\.\-]SN[:\s]+\n\s*(CDT[A-Za-z0-9]+)',
    ]
    
    for pattern in cdata_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE | re.MULTILINE)
        if match:
            psn = match.group(1).upper()
            # Fix common OCR errors
            psn = psn.replace('COTC', 'CDTC')
            return psn, 'C-DATA'
    
    # ZTE pattern: ZTEG + alphanumeric (6+ chars)
    # Examples: ZTEGD07A8EEE, ZTEGD08EA229
    zte_patterns = [
        r'(ZTEG[A-Za-z0-9]{6,})',
        r'(ZTE[A-Za-z0-9]{7,})',   # Partial match
        # P.SN or P-SN followed by value on same line
        r'P[\.\-]SN[:\s]+(ZTEG[A-Za-z0-9]+)',
        r'PON\s*SN[:\s]+(ZTEG[A-Za-z0-9]+)',
        # Handle multi-line: P.SN on one line, value on next
        r'P[\.\-]SN[:\s]+\n\s*(ZTEG[A-Za-z0-9]+)',
    ]
    
    for pattern in zte_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper(), 'F670L'
    
    return None, None


def extract_mac_address(text):
    """Extract MAC address from text"""
    clean_text = re.sub(r'<[^>]+>', '', text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    mac_patterns = [
        r'MAC[:\s]+([0-9A-Fa-f]{12})',
        r'MAC[:\s]+([0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2})',
        r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})',
    ]
    
    for pattern in mac_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE)
        if match:
            mac = match.group(1).upper()
            mac = re.sub(r'[:-]', '', mac)
            if len(mac) == 12 and all(c in '0123456789ABCDEF' for c in mac):
                return mac
    
    return None


# ============================================================================
# MULTI-ATTEMPT EXTRACTION
# ============================================================================

def extract_ps_from_barcode(image_path):
    """
    Attempt 1: Extract P-SN from barcode (fastest, most accurate).
    
    Returns: (psn, device_type, confidence) or (None, None, 0)
    """
    barcodes = scan_barcodes(image_path)
    
    for barcode in barcodes:
        data = barcode['data'].strip()
        
        # Check for C-DATA P-SN
        if re.match(r'^CDT', data, re.IGNORECASE):
            psn = data.upper()
            psn = psn.replace('COTC', 'CDTC')  # Fix OCR errors
            return psn, 'C-DATA', 0.95
        
        # Check for ZTE P-SN
        if re.match(r'^ZTEG', data, re.IGNORECASE):
            return data.upper(), 'F670L', 0.95
    
    return None, None, 0


def extract_ps_from_ocr(image_path, ocr_text):
    """
    Attempt 2: Extract P-SN from OCR text with pattern matching.
    
    Returns: (psn, device_type, confidence) or (None, None, 0)
    """
    psn, device_type = extract_psPattern(ocr_text)
    
    if psn:
        # Higher confidence if pattern is exact (CDTC/ZTEG prefix)
        if psn.startswith(('CDTC', 'ZTEG')):
            return psn, device_type, 0.85
        else:
            return psn, device_type, 0.70
    
    return None, None, 0


def extract_ps_from_preprocessed(image_path):
    """
    Attempt 3: Extract P-SN using preprocessed images (for low quality).
    
    Returns: (psn, device_type, confidence, method) or (None, None, 0, None)
    """
    image = cv2.imread(image_path)
    if image is None:
        return None, None, 0, None
    
    # Try barcode on preprocessed versions
    preprocessed = preprocess_image(image)
    
    for name, enhanced_img in preprocessed:
        # Save temp image for barcode scanning
        temp_path = f"/tmp/temp_{name}.jpg"
        cv2.imwrite(temp_path, enhanced_img)
        
        barcodes = scan_barcodes(temp_path)
        for barcode in barcodes:
            data = barcode['data'].strip()
            if re.match(r'^CDT', data, re.IGNORECASE):
                return data.upper().replace('COTC', 'CDTC'), 'C-DATA', 0.80, f'barcode_{name}'
            if re.match(r'^ZTEG', data, re.IGNORECASE):
                return data.upper(), 'F670L', 0.80, f'barcode_{name}'
        
        # Try OCR on preprocessed version
        # Note: This would require running OCR locally or via API
        # For now, we'll rely on the main OCR call
        
        # Cleanup temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    return None, None, 0, None


def extract_ps_from_ocr_only(ocr_text, image_path=None):
    """
    Extract P-SN from OCR API text only (no barcode scanning).

    Returns: {
        "psn": str,
        "device_type": str,
        "confidence": float,
        "method": str,
        "attempts": list
    }
    """
    result = {
        "psn": None,
        "device_type": None,
        "confidence": 0,
        "method": None,
        "attempts": []
    }

    if not ocr_text:
        return result

    psn, device_type, confidence = extract_ps_from_ocr(image_path, ocr_text)
    result["attempts"].append({
        "method": "ocr_pattern",
        "psn": psn,
        "device_type": device_type,
        "confidence": confidence
    })

    if psn:
        result.update({
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence,
            "method": "ocr_pattern",
        })

    return result


def extract_ps_barcode_fallback(image_path):
    """
    Fallback P-SN extraction using barcode scanning when OCR API fails.
    Tries raw barcode first, then preprocessed image variants.

    Returns: {
        "psn": str,
        "device_type": str,
        "confidence": float,
        "method": str,
        "attempts": list
    }
    """
    result = {
        "psn": None,
        "device_type": None,
        "confidence": 0,
        "method": None,
        "attempts": []
    }

    # Attempt A: Direct barcode scan
    psn, device_type, confidence = extract_ps_from_barcode(image_path)
    result["attempts"].append({
        "method": "barcode",
        "psn": psn,
        "device_type": device_type,
        "confidence": confidence
    })

    if psn and confidence >= 0.95:
        result.update({
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence,
            "method": "barcode",
        })
        return result

    # Attempt B: Preprocessed image barcode scan
    psn, device_type, confidence, method = extract_ps_from_preprocessed(image_path)
    result["attempts"].append({
        "method": f"preprocessed_{method}" if method else "preprocessed",
        "psn": psn,
        "device_type": device_type,
        "confidence": confidence
    })

    if psn and confidence >= 0.75:
        result.update({
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence,
            "method": f"preprocessed_{method}" if method else "preprocessed",
        })
        return result

    # Return best attempt (if any)
    best_attempt = max(result["attempts"], key=lambda x: x["confidence"] or 0)
    if best_attempt["psn"]:
        result.update({
            "psn": best_attempt["psn"],
            "device_type": best_attempt["device_type"],
            "confidence": best_attempt["confidence"],
            "method": best_attempt["method"],
        })

    return result


def extract_ps_comprehensive(image_path, ocr_text=None):
    """
    Comprehensive P-SN extraction with multiple attempts and confidence scoring.

    Order of attempts:
      1. OCR API text pattern matching (primary)
      2. Barcode scan on original image (fallback, only if OCR yields nothing)
      3. Barcode scan on preprocessed image variants (last resort)

    Returns: {
        "psn": str,
        "device_type": str,
        "confidence": float,
        "method": str,
        "attempts": list
    }
    """
    result = {
        "psn": None,
        "device_type": None,
        "confidence": 0,
        "method": None,
        "attempts": []
    }

    # Attempt 1: OCR text pattern matching (API result is primary source)
    if ocr_text:
        psn, device_type, confidence = extract_ps_from_ocr(image_path, ocr_text)
        result["attempts"].append({
            "method": "ocr_pattern",
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence
        })

        if psn and confidence >= 0.85:
            return {
                "psn": psn,
                "device_type": device_type,
                "confidence": confidence,
                "method": "ocr_pattern",
                "attempts": result["attempts"]
            }

    # Attempt 2: Barcode scan (fallback when OCR found nothing)
    psn, device_type, confidence = extract_ps_from_barcode(image_path)
    result["attempts"].append({
        "method": "barcode",
        "psn": psn,
        "device_type": device_type,
        "confidence": confidence
    })

    if psn and confidence >= 0.95:
        return {
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence,
            "method": "barcode",
            "attempts": result["attempts"]
        }

    # Attempt 3: Preprocessed image barcode scan (last resort)
    psn, device_type, confidence, method = extract_ps_from_preprocessed(image_path)
    result["attempts"].append({
        "method": f"preprocessed_{method}" if method else "preprocessed",
        "psn": psn,
        "device_type": device_type,
        "confidence": confidence
    })

    if psn and confidence >= 0.75:
        return {
            "psn": psn,
            "device_type": device_type,
            "confidence": confidence,
            "method": f"preprocessed_{method}" if method else "preprocessed",
            "attempts": result["attempts"]
        }

    # Return best attempt (if any)
    best_attempt = max(result["attempts"], key=lambda x: x["confidence"] or 0)
    if best_attempt["psn"]:
        return {
            "psn": best_attempt["psn"],
            "device_type": best_attempt["device_type"],
            "confidence": best_attempt["confidence"],
            "method": best_attempt["method"],
            "attempts": result["attempts"]
        }

    return result


def get_headers() -> dict:
    """Get authorization headers for API requests."""
    return {"Authorization": f"bearer {TOKEN_API_OCR}"}


def get_optional_payload() -> dict:
    """Get optional payload configuration for OCR processing."""
    return {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }


def scan_barcodes(image_path: str) -> List[Dict[str, Any]]:
    """
    Scan all barcodes from an image file using zxing-cpp.
    
    Args:
        image_path: Path to the image file
        
    Returns:
        List of dictionaries containing barcode data:
        [{"type": str, "data": str, "rect": tuple}, ...]
    """
    try:
        # Load image with OpenCV
        image = cv2.imread(image_path)
        if image is None:
            print(f"  Warning: Could not load image for barcode scanning: {image_path}")
            return []
        
        # Try multiple preprocessing methods for better detection
        results = []
        
        # Method 1: Original image
        barcodes = zxingcpp.read_barcodes(image)
        for barcode in barcodes:
            results.append({
                "type": str(barcode.format),
                "data": barcode.text,
                "rect": {
                    "x": barcode.position.top_left.x,
                    "y": barcode.position.top_left.y,
                    "width": barcode.position.top_right.x - barcode.position.top_left.x,
                    "height": barcode.position.bottom_left.y - barcode.position.top_left.y
                },
                "quality": 0
            })
        
        # Method 2: Upscaled image (2x) for better detection
        if not results:
            upscaled = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            barcodes = zxingcpp.read_barcodes(upscaled)
            for barcode in barcodes:
                # Scale coordinates back to original size
                results.append({
                    "type": str(barcode.format),
                    "data": barcode.text,
                    "rect": {
                        "x": barcode.position.top_left.x // 2,
                        "y": barcode.position.top_left.y // 2,
                        "width": (barcode.position.top_right.x - barcode.position.top_left.x) // 2,
                        "height": (barcode.position.bottom_left.y - barcode.position.top_left.y) // 2
                    },
                    "quality": 0
                })
        
        # Method 3: Grayscale with threshold
        if not results:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            barcodes = zxingcpp.read_barcodes(thresh)
            for barcode in barcodes:
                results.append({
                    "type": str(barcode.format),
                    "data": barcode.text,
                    "rect": {
                        "x": barcode.position.top_left.x,
                        "y": barcode.position.top_left.y,
                        "width": barcode.position.top_right.x - barcode.position.top_left.x,
                        "height": barcode.position.bottom_left.y - barcode.position.top_left.y
                    },
                    "quality": 0
                })
        
        return results
        
    except Exception as e:
        print(f"  Warning: Barcode scanning failed: {e}")
        return []


def extract_ocr_fields(markdown_text: str) -> Dict[str, str]:
    """
    Extract key fields (MAC, SN, P-SN) from OCR markdown text.
    
    Args:
        markdown_text: OCR result in markdown format
        
    Returns:
        Dictionary of extracted fields
    """
    fields = {}
    
    # Remove HTML tags and clean up text
    clean_text = re.sub(r'<[^>]+>', '', markdown_text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    # MAC address pattern (12 hex characters, with or without separators)
    mac_patterns = [
        r'MAC[:\s]+([0-9A-Fa-f]{12})',
        r'MAC[:\s]+([0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2})',
        r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})',
    ]
    for pattern in mac_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE)
        if match:
            mac = match.group(1).upper()
            # Normalize: remove separators
            mac = re.sub(r'[:-]', '', mac)
            # Validate MAC format (12 hex chars)
            if len(mac) == 12 and all(c in '0123456789ABCDEF' for c in mac):
                fields['MAC'] = mac
                break
    
    # Serial Number patterns
    sn_patterns = [
        r'SN[:\s]+([A-Za-z0-9\-]+)',
        r'Serial[:\s]+([A-Za-z0-9\-]+)',
        r'(D\d{3}-\d{8,})',
    ]
    for pattern in sn_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE)
        if match:
            fields['SN'] = match.group(1)
            break
    
    # P-SN (PON Serial Number) patterns
    psn_patterns = [
        r'P[\.\-]?SN[:\s]+([A-Za-z0-9\-]+)',
        r'PON\s*SN[:\s]+([A-Za-z0-9\-]+)',
        r'(CDTC[A-Za-z0-9]+)',
        r'(ZTEG[A-Za-z0-9]+)',
    ]
    for pattern in psn_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE)
        if match:
            fields['P-SN'] = match.group(1)
            break
    
    # LOID (Logical Object ID)
    loid_patterns = [
        r'LOID[:\s]+([A-Za-z0-9\-]+)',
    ]
    for pattern in loid_patterns:
        match = re.search(pattern, clean_text, re.IGNORECASE)
        if match:
            fields['LOID'] = match.group(1)
            break
    
    return fields


def identify_barcode_field(barcode_data: str, barcode_type: str) -> str:
    """
    Identify what field a barcode represents based on its content.
    
    Args:
        barcode_data: The decoded barcode data
        barcode_type: The type of barcode (CODE128, etc.)
        
    Returns:
        Field name (MAC, SN, P-SN, SSID, PASSWORD, or UNKNOWN)
    """
    data = barcode_data.strip()
    
    # MAC address: 12 hex characters
    if re.match(r'^[0-9A-Fa-f]{12}$', data) or re.match(r'^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$', data):
        return 'MAC'
    
    # SN: starts with D017 or similar patterns
    if re.match(r'^D\d{3}-\d{8,}$', data):
        return 'SN'
    
    # P-SN: starts with CDTC or similar
    if re.match(r'^CDTC', data, re.IGNORECASE) or re.match(r'^ZTEG', data, re.IGNORECASE):
        return 'P-SN'
    
    # SSID: starts with HGW-
    if re.match(r'^HGW-', data, re.IGNORECASE):
        return 'SSID'
    
    # Password: numeric only, 8 digits
    if re.match(r'^\d{8}$', data):
        return 'PASSWORD'
    
    # Default IP
    if re.match(r'^192\.168\.\d+\.\d+$', data):
        return 'IP'
    
    return 'UNKNOWN'


def verify_ocr_with_barcodes(
    ocr_fields: Dict[str, str], 
    barcode_data: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Compare OCR extracted fields with barcode data for verification.
    
    Args:
        ocr_fields: Fields extracted from OCR markdown
        barcode_data: List of barcode scan results
        
    Returns:
        Verification report dictionary
    """
    verification = {
        "ocr_vs_barcode": {},
        "barcode_fields": {},
        "overall_match": True,
        "confidence": "high",
        "mismatches": []
    }
    
    # Identify barcode fields
    for barcode in barcode_data:
        field = identify_barcode_field(barcode["data"], barcode["type"])
        if field != 'UNKNOWN':
            verification["barcode_fields"][field] = {
                "value": barcode["data"],
                "type": barcode["type"]
            }
    
    # Compare OCR fields with barcode fields
    for field in ['MAC', 'SN', 'P-SN']:
        ocr_value = ocr_fields.get(field, None)
        barcode_value = verification["barcode_fields"].get(field, {}).get("value", None)
        
        if ocr_value and barcode_value:
            # Normalize for comparison
            ocr_normalized = ocr_value.upper().replace('-', '').replace(':', '')
            barcode_normalized = barcode_value.upper().replace('-', '').replace(':', '')
            
            match = ocr_normalized == barcode_normalized
            verification["ocr_vs_barcode"][field] = {
                "ocr": ocr_value,
                "barcode": barcode_value,
                "match": match
            }
            
            if not match:
                verification["overall_match"] = False
                verification["mismatches"].append(field)
        elif ocr_value:
            verification["ocr_vs_barcode"][field] = {
                "ocr": ocr_value,
                "barcode": None,
                "match": None,
                "note": "No barcode found for comparison"
            }
        elif barcode_value:
            verification["ocr_vs_barcode"][field] = {
                "ocr": None,
                "barcode": barcode_value,
                "match": None,
                "note": "No OCR value found for comparison"
            }
    
    # Set confidence level
    if verification["mismatches"]:
        verification["confidence"] = "low"
    elif not verification["barcode_fields"]:
        verification["confidence"] = "medium"
    
    return verification


# ============================================================================
# OCR JOB SUBMISSION HELPERS
# ============================================================================

def ocr_text_is_empty(markdown_text: str) -> bool:
    """
    Return True when the OCR markdown contains no useful plain text
    (i.e. only image tags were produced, which happens for upside-down labels).
    """
    # Strip HTML tags and whitespace
    clean = re.sub(r'<[^>]+>', '', markdown_text)
    clean = clean.strip()
    return len(clean) < 10  # fewer than 10 chars means effectively empty


def submit_and_poll_ocr_job(
    image_bytes: bytes,
    filename: str,
    headers: dict,
    optional_payload: dict,
) -> Optional[str]:
    """
    Submit an OCR job with raw image bytes and poll until done.

    Returns the JSONL result URL on success, or None on failure.
    """
    data = {
        "model": MODEL,
        "optionalPayload": json.dumps(optional_payload)
    }
    files = {"file": (filename, image_bytes, "image/jpeg")}
    job_response = requests.post(JOB_URL, headers=headers, data=data, files=files)

    if job_response.status_code != 200:
        print(f"  Error: API returned status {job_response.status_code}")
        print(f"  Response: {job_response.text[:200]}")
        return None

    job_id = job_response.json()["data"]["jobId"]
    print(f"  OCR Job submitted: {job_id}")

    while True:
        job_result_response = requests.get(f"{JOB_URL}/{job_id}", headers=headers)
        if job_result_response.status_code != 200:
            print(f"  Error polling job status: {job_result_response.status_code}")
            return None

        state = job_result_response.json()["data"]["state"]

        if state == 'pending':
            pass
        elif state == 'running':
            try:
                progress = job_result_response.json()['data']['extractProgress']
                print(f"  Running: {progress.get('extractedPages', 0)}/{progress.get('totalPages', '?')} pages", end='\r')
            except KeyError:
                pass
        elif state == 'done':
            print(f"  OCR Job completed successfully")
            return job_result_response.json()['data']['resultUrl']['jsonUrl']
        elif state == 'failed':
            error_msg = job_result_response.json()['data']['errorMsg']
            print(f"  Job failed: {error_msg}")
            return None

        time.sleep(3)


def get_rotated_image_bytes(image_path: str, rotation: int) -> bytes:
    """
    Load an image, apply a rotation, and return the result as JPEG bytes.

    Args:
        image_path: Source image path
        rotation: One of 90, 180, 270 (clockwise degrees)

    Returns:
        JPEG bytes of the rotated image
    """
    image = cv2.imread(image_path)
    if rotation == 90:
        rotated = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        rotated = cv2.rotate(image, cv2.ROTATE_180)
    elif rotation == 270:
        rotated = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    else:
        rotated = image
    _, buf = cv2.imencode('.jpg', rotated, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes()


def process_single_image(file_path: str, output_base_dir: str) -> bool:
    """
    Process a single image file using PaddleOCR API with comprehensive P-SN extraction.

    Pipeline order:
      1. Submit OCR job to API and wait for results.
      2. Extract P-SN from the OCR text (primary method).
      3. If OCR yields no P-SN, run barcode scan as fallback.

    Args:
        file_path: Path to the image file
        output_base_dir: Base directory for output

    Returns:
        True if successful, False otherwise
    """
    file_path = Path(file_path)
    if not file_path.exists():
        print(f"  Error: File not found at {file_path}")
        return False

    headers = get_headers()
    optional_payload = get_optional_payload()

    # Create output directory for this image
    image_name = file_path.stem
    output_dir = Path(output_base_dir) / image_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialise P-SN result placeholder (populated after OCR)
    psn_result = {
        "psn": None,
        "device_type": None,
        "confidence": 0,
        "method": None,
        "attempts": []
    }
    psn_filename = output_dir / "psn_extraction.json"

    try:
        barcode_data = []   # only populated when barcode fallback is triggered
        ocr_fields = {}

        # Read original image bytes once
        with open(file_path, "rb") as f:
            original_bytes = f.read()

        # -----------------------------------------------------------------------
        # Step 1: Submit OCR job to the API (original orientation)
        # -----------------------------------------------------------------------
        jsonl_url = submit_and_poll_ocr_job(
            original_bytes, file_path.name, headers, optional_payload
        )
        if jsonl_url is None:
            return False

        # -----------------------------------------------------------------------
        # Step 2: Download OCR results
        # -----------------------------------------------------------------------
        jsonl_response = requests.get(jsonl_url)
        jsonl_response.raise_for_status()
        lines = jsonl_response.text.strip().split('\n')
        lines = [l for l in lines if l.strip()]

        if not lines:
            print(f"  Error: empty JSONL response")
            return False

        # Parse the first valid result
        ocr_result_obj = json.loads(lines[0])["result"]
        layout_results = ocr_result_obj["layoutParsingResults"]

        if not layout_results:
            print(f"  Error: no layout results")
            return False

        res = layout_results[0]
        markdown_text = res["markdown"]["text"]

        # -----------------------------------------------------------------------
        # Step 2b: Rotation retry — when OCR text is empty the label is likely
        # upside-down; re-submit with 180° rotation (most common case) and
        # then 90°/270° if still empty.
        # -----------------------------------------------------------------------
        rotation_used = 0
        if ocr_text_is_empty(markdown_text):
            print(f"  OCR returned no text — retrying with rotated images...")
            for angle in [180, 90, 270]:
                print(f"  Retrying with {angle}° rotation...")
                rot_bytes = get_rotated_image_bytes(str(file_path), angle)
                rot_url = submit_and_poll_ocr_job(
                    rot_bytes,
                    f"rotated_{angle}_{file_path.name}",
                    headers,
                    optional_payload,
                )
                if rot_url is None:
                    continue

                rot_response = requests.get(rot_url)
                rot_response.raise_for_status()
                rot_lines = [l for l in rot_response.text.strip().split('\n') if l.strip()]
                if not rot_lines:
                    continue

                rot_result = json.loads(rot_lines[0])["result"]
                rot_layout = rot_result["layoutParsingResults"]
                if not rot_layout:
                    continue

                rot_md = rot_layout[0]["markdown"]["text"]
                if not ocr_text_is_empty(rot_md):
                    print(f"  ✓ Got text at {angle}° rotation")
                    res = rot_layout[0]
                    markdown_text = rot_md
                    rotation_used = angle
                    break
            else:
                print(f"  All rotations returned empty text")

        # Save markdown
        md_filename = output_dir / f"{image_name}.md"
        with open(md_filename, "w", encoding="utf-8") as md_file:
            md_file.write(markdown_text)
        print(f"  Saved: {md_filename}")
        if rotation_used:
            print(f"  (rotation applied: {rotation_used}°)")

        # -----------------------------------------------------------------------
        # Step 3: Extract P-SN from OCR text (primary)
        # -----------------------------------------------------------------------
        ocr_fields = extract_ocr_fields(markdown_text)
        if ocr_fields:
            print(f"  OCR extracted fields: {list(ocr_fields.keys())}")

        print(f"  Extracting P-SN from OCR text...")
        psn_result = extract_ps_from_ocr_only(markdown_text, str(file_path))

        if psn_result["psn"]:
            print(f"  ✓ P-SN (OCR): {psn_result['psn']} ({psn_result['device_type']})")
            print(f"    Confidence: {psn_result['confidence']:.0%} | Method: {psn_result['method']}")
        else:
            # ----------------------------------------------------------
            # Step 4: OCR gave no P-SN → fall back to barcode scanning
            # ----------------------------------------------------------
            print(f"  ✗ P-SN not found in OCR text — trying barcode fallback...")
            barcode_psn_result = extract_ps_barcode_fallback(str(file_path))

            psn_result["attempts"].extend(barcode_psn_result["attempts"])

            if barcode_psn_result["psn"]:
                psn_result.update({
                    "psn": barcode_psn_result["psn"],
                    "device_type": barcode_psn_result["device_type"],
                    "confidence": barcode_psn_result["confidence"],
                    "method": barcode_psn_result["method"],
                })
                print(f"  ✓ P-SN (barcode fallback): {psn_result['psn']} ({psn_result['device_type']})")
                print(f"    Confidence: {psn_result['confidence']:.0%} | Method: {psn_result['method']}")

                barcode_data = scan_barcodes(str(file_path))
                print(f"  Found {len(barcode_data)} barcodes (for field verification)")
            else:
                print(f"  ✗ P-SN: Not found via OCR or barcode")

        # Save P-SN extraction result
        with open(psn_filename, "w") as f:
            json.dump(psn_result, f, indent=2)

        # Save markdown images
        for img_path_key, img_url in res["markdown"]["images"].items():
            full_img_path = output_dir / img_path_key
            full_img_path.parent.mkdir(parents=True, exist_ok=True)
            img_bytes_dl = requests.get(img_url).content
            with open(full_img_path, "wb") as img_file:
                img_file.write(img_bytes_dl)

        # Save output images
        for img_name_key, img_url in res["outputImages"].items():
            img_response = requests.get(img_url)
            if img_response.status_code == 200:
                img_filename = output_dir / f"{img_name_key}_0.jpg"
                with open(img_filename, "wb") as f:
                    f.write(img_response.content)

        # -----------------------------------------------------------------------
        # Step 5: Save barcode results (if barcode fallback was triggered)
        # -----------------------------------------------------------------------
        if barcode_data:
            barcode_output = {
                "image": file_path.name,
                "barcodes": barcode_data,
                "total_barcodes": len(barcode_data),
                "scan_time": datetime.now().isoformat()
            }
            barcode_filename = output_dir / "barcodes.json"
            with open(barcode_filename, "w") as f:
                json.dump(barcode_output, f, indent=2)
            print(f"  Saved: {barcode_filename}")

            # Verify OCR fields against barcode data
            if ocr_fields:
                verification = verify_ocr_with_barcodes(ocr_fields, barcode_data)
                verification["ocr_fields"] = ocr_fields
                verification["psn_extraction"] = psn_result
                verification["timestamp"] = datetime.now().isoformat()

                verification_filename = output_dir / "verification.json"
                with open(verification_filename, "w") as f:
                    json.dump(verification, f, indent=2)
                print(f"  Saved: {verification_filename}")

                if verification["overall_match"]:
                    print(f"  ✓ Verification: All fields match")
                else:
                    print(f"  ✗ Verification: Mismatches found in {verification['mismatches']}")

        return True

    except requests.exceptions.RequestException as e:
        print(f"  Network error: {e}")
        return False
    except Exception as e:
        import traceback
        print(f"  Unexpected error: {e}")
        traceback.print_exc()
        return False


def batch_process_images(input_dir: str, output_dir: str = "output", delay: int = 5) -> dict:
    """
    Process all images in a directory sequentially with delays.
    
    Args:
        input_dir: Directory containing images to process
        output_dir: Base directory for output
        delay: Delay in seconds between API calls
        
    Returns:
        Dictionary with processing statistics
    """
    input_path = Path(input_dir)
    if not input_path.exists():
        print(f"Error: Input directory not found at {input_dir}")
        sys.exit(1)
    
    # Find all image files
    image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    image_files = sorted(
        [f for f in input_path.iterdir() 
         if f.is_file() and f.suffix.lower() in image_extensions],
        key=lambda x: int(''.join(filter(str.isdigit, x.stem)) or '0')
    )
    
    if not image_files:
        print(f"No image files found in {input_dir}")
        sys.exit(1)
    
    total_images = len(image_files)
    print(f"Found {total_images} images to process")
    print(f"Output directory: {output_dir}")
    print(f"Delay between images: {delay} seconds")
    print("=" * 60)
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Process statistics
    stats = {
        "total": total_images,
        "successful": 0,
        "failed": 0,
        "failed_files": [],
        "start_time": time.time()
    }
    
    for idx, image_file in enumerate(image_files, 1):
        print(f"\n[{idx}/{total_images}] Processing: {image_file.name}")
        
        success = process_single_image(str(image_file), output_dir)
        
        if success:
            stats["successful"] += 1
        else:
            stats["failed"] += 1
            stats["failed_files"].append(image_file.name)
        
        # Delay between images (except after the last one)
        if idx < total_images:
            print(f"  Waiting {delay} seconds before next image...")
            time.sleep(delay)
    
    # Calculate total time
    stats["end_time"] = time.time()
    stats["total_time"] = stats["end_time"] - stats["start_time"]
    
    # Print summary
    print("\n" + "=" * 60)
    print("BATCH PROCESSING COMPLETE")
    print("=" * 60)
    print(f"Total images: {stats['total']}")
    print(f"Successful: {stats['successful']}")
    print(f"Failed: {stats['failed']}")
    print(f"Total time: {stats['total_time']:.1f} seconds")
    
    if stats["failed_files"]:
        print(f"\nFailed files:")
        for failed_file in stats["failed_files"]:
            print(f"  - {failed_file}")
    
    # Save summary to JSON
    summary_path = Path(output_dir) / "batch_summary.json"
    with open(summary_path, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"\nSummary saved to: {summary_path}")
    
    return stats


def main():
    """Main entry point for the script."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Batch OCR processing using PaddleOCR API")
    parser.add_argument("input", nargs="?", default="test", 
                        help="Input directory or file (default: test)")
    parser.add_argument("-o", "--output", default="output", 
                        help="Output directory (default: output)")
    parser.add_argument("-d", "--delay", type=int, default=5, 
                        help="Delay in seconds between API calls (default: 5)")
    
    args = parser.parse_args()
    
    print("PaddleOCR Batch Processing Tool")
    print("=" * 60)
    print(f"API Endpoint: {JOB_URL}")
    print(f"Model: {MODEL}")
    print("=" * 60)
    
    input_path = Path(args.input)
    if input_path.is_file():
        print(f"Processing single file: {args.input}")
        success = process_single_image(args.input, args.output)
        print(f"\nResult: {'Success' if success else 'Failed'}")
    elif input_path.is_dir():
        batch_process_images(args.input, args.output, args.delay)
    else:
        print(f"Error: Input not found: {args.input}")
        sys.exit(1)


if __name__ == "__main__":
    main()

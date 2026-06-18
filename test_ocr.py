import sys
import os
import io
from PIL import Image, ImageDraw, ImageFont

# Add current directory to path so we can import the api module
sys.path.append(os.getcwd())

try:
    from api.v1.endpoints.ocr import _process_image_ocr
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)


def create_test_image(text="Hello World OCR Test"):
    # Create white image
    img = Image.new("RGB", (400, 100), color=(255, 255, 255))
    d = ImageDraw.Draw(img)

    # Use default font since we might not have specific ttf
    # Draw text in black
    d.text((20, 40), text, fill=(0, 0, 0))

    # Convert to bytes
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()


def test_ocr():
    print("Generating test image...")
    image_bytes = create_test_image("Validation Successful")

    print("Running OCR processing...")
    try:
        result = _process_image_ocr(image_bytes)
        print("\n--- OCR Result ---")
        print(f"Detected Text: '{result}'")

        if "Validation Successful" in result:
            print("\n✅ Test PASSED: Text detected correctly.")
        elif result:
            print("\n⚠️ Test PARTIAL: Text detected but might be inaccurate.")
        else:
            print("\n❌ Test FAILED: No text detected.")

    except Exception as e:
        print(f"\n❌ Test CRASHED: {e}")


if __name__ == "__main__":
    test_ocr()

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


def create_test_image(text="Normal Text"):
    img = Image.new("RGB", (400, 100), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((20, 40), text, fill=(0, 0, 0))
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()


def create_rotated_image(text="Rotated Text"):
    img = Image.new("RGB", (400, 100), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((20, 40), text, fill=(0, 0, 0))

    # Rotate 180 degrees (upside down)
    rotated = img.transpose(Image.Transpose.ROTATE_180)

    img_byte_arr = io.BytesIO()
    rotated.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()


def test_ocr():
    print("--- Test 1: Normal Image ---")
    image_bytes = create_test_image("Normal Success")
    result = _process_image_ocr(image_bytes)
    print(f"Result: '{result}'")

    print("\n--- Test 2: Upside Down Image ---")
    image_bytes = create_rotated_image("Rotated Success")
    result = _process_image_ocr(image_bytes)
    print(f"Result: '{result}'")


if __name__ == "__main__":
    test_ocr()

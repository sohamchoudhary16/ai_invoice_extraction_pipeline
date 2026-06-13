"""
src/preprocessing/image_cleanup.py
Image preprocessing before Tesseract OCR — Pillow only, no opencv.
Compatible with Pillow 10.x (ImageMath.eval removed in Pillow 10.0).

Colour overlay suppression uses only:
  - Image.split()
  - ImageChops operations
  - Image.point() for thresholding
  - Image.paste() for masking
No ImageMath.eval anywhere.
"""

from PIL import Image, ImageFilter, ImageOps, ImageChops
from src.observability.logger import get_logger

log = get_logger(__name__)


def preprocess_image(img: Image.Image, dpi: int = 300) -> Image.Image:
    img = _ensure_rgb(img)
    img = _suppress_colour_overlays(img)
    img = _denoise(img)
    img = _binarize(img)
    img = _remove_border(img)
    return img


def _ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _suppress_colour_overlays(img: Image.Image) -> Image.Image:
    """
    Whiten coloured (high-saturation) pixels while preserving black text.

    Uses only Pillow 10-compatible operations:
      split, ImageChops.lighter/darker, point(), paste()

    Algorithm:
      chroma = max(R,G,B) - min(R,G,B)
        high chroma = coloured pixel (stamp, annotation)
        low chroma  = grey/black/white pixel (text, background)

      We also need brightness to avoid darkening text:
        dark pixels (text)       → max(R,G,B) < 60  → keep
        bright pixels (bg)       → max(R,G,B) > 240 → keep
        mid-brightness + coloured → stamp/annotation → whiten
    """
    r, g, b = img.split()

    # max channel (brightness proxy)
    max_ch = ImageChops.lighter(ImageChops.lighter(r, g), b)
    # min channel
    min_ch = ImageChops.darker(ImageChops.darker(r, g), b)
    # chroma = max - min  (values 0-255)
    chroma = ImageChops.subtract(max_ch, min_ch)

    # Threshold: chroma > 60 means "coloured enough to be a stamp"
    # point(lambda) applies per-pixel: returns 255 if condition, else 0
    chroma_mask  = chroma.point(lambda p: 255 if p > 60  else 0)

    # Not-dark mask: max channel > 60 (exclude black text)
    not_dark     = max_ch.point(lambda p: 255 if p > 60  else 0)

    # Not-white mask: max channel < 240 (exclude white background)
    not_white    = max_ch.point(lambda p: 255 if p < 240 else 0)

    # Final mask: all three conditions must be true
    # Use ImageChops.multiply to AND masks (255*255/255=255, anything*0/255=0)
    mask = ImageChops.multiply(chroma_mask, not_dark)
    mask = ImageChops.multiply(mask, not_white)

    # Paste white where mask is active
    white = Image.new("RGB", img.size, (255, 255, 255))
    img.paste(white, mask=mask)

    log.debug("overlay_suppression_applied")
    return img


def _denoise(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.MedianFilter(size=3))


def _binarize(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    enhanced = ImageOps.autocontrast(gray)
    return enhanced.convert("RGB")


def _remove_border(img: Image.Image, border_px: int = 8) -> Image.Image:
    w, h = img.size
    if w > border_px * 4 and h > border_px * 4:
        return img.crop((border_px, border_px, w - border_px, h - border_px))
    return img

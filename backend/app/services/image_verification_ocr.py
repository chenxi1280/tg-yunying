from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from statistics import mean

from PIL import Image, ImageEnhance, ImageOps

TESSERACT_TIMEOUT_SECONDS = 10
OCR_SCALE_FACTOR = 3
TESSERACT_CONFIG = (
    "--oem 3 --psm 7 "
    "-c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz+-*/xX=?"
)


def recognize_with_tesseract(image_bytes: bytes) -> tuple[str, float]:
    import pytesseract

    image = _prepared_image(image_bytes)
    data = pytesseract.image_to_data(
        image,
        config=TESSERACT_CONFIG,
        output_type=pytesseract.Output.DICT,
        timeout=TESSERACT_TIMEOUT_SECONDS,
    )
    words = [
        str(text).strip()
        for text in data.get("text", [])
        if str(text).strip()
    ]
    scores = [
        float(score) / 100
        for score in data.get("conf", [])
        if _valid_tesseract_score(score)
    ]
    return "".join(words), mean(scores) if scores else 0.0


def recognize_with_rapidocr(image_bytes: bytes) -> tuple[str, float]:
    result = _rapidocr_engine()(image_bytes)
    texts = tuple(str(value).strip() for value in (result.txts or ()))
    scores = tuple(float(value) for value in (result.scores or ()))
    return "".join(texts), mean(scores) if scores else 0.0


def _prepared_image(image_bytes: bytes) -> Image.Image:
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.grayscale(source)
        scaled = image.resize(
            (
                image.width * OCR_SCALE_FACTOR,
                image.height * OCR_SCALE_FACTOR,
            ),
            Image.Resampling.LANCZOS,
        )
        return ImageEnhance.Contrast(scaled).enhance(2.0)


def _valid_tesseract_score(score: object) -> bool:
    try:
        return float(score) >= 0
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=1)
def _rapidocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR()

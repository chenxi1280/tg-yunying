from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from statistics import mean
from threading import Lock

from PIL import Image, ImageChops, ImageEnhance, ImageOps

TESSERACT_TIMEOUT_SECONDS = 10
OCR_SCALE_FACTOR = 3
CAPTCHA_SCALE_FACTOR = 4
BLUE_MASK_THRESHOLDS = (20, 30, 40)
DDDDOCR_CROP_RATIOS = (1.0, 0.75, 0.65)
DDDDOCR_CONFIDENCE = 0.80
RAPIDOCR_INFERENCE_LOCK = Lock()
DDDDOCR_INFERENCE_LOCK = Lock()
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
    with RAPIDOCR_INFERENCE_LOCK:
        return _recognize_with_rapidocr(image_bytes)


def _recognize_with_rapidocr(image_bytes: bytes) -> tuple[str, float]:
    result = _rapidocr_engine()(image_bytes)
    texts = tuple(str(value).strip() for value in (result.txts or ()))
    scores = tuple(float(value) for value in (result.scores or ()))
    return "".join(texts), mean(scores) if scores else 0.0


def recognize_rapidocr_variants(
    image_bytes: bytes,
) -> tuple[tuple[str, float], ...]:
    variants = (image_bytes,) + tuple(
        _blue_mask(image_bytes, threshold)
        for threshold in BLUE_MASK_THRESHOLDS
    )
    with RAPIDOCR_INFERENCE_LOCK:
        return tuple(
            _recognize_with_rapidocr(image) for image in variants
        )


def recognize_ddddocr_variants(
    image_bytes: bytes,
) -> tuple[tuple[str, float], ...]:
    with DDDDOCR_INFERENCE_LOCK:
        engine = _ddddocr_engine()
        return tuple(
            (
                str(engine.classification(image) or ""),
                DDDDOCR_CONFIDENCE,
            )
            for image in _cropped_variants(image_bytes)
        )


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


def _blue_mask(image_bytes: bytes, threshold: int) -> bytes:
    with Image.open(BytesIO(image_bytes)) as source:
        red, green, blue = source.convert("RGB").split()
        competing = ImageChops.lighter(red, green)
        strength = ImageChops.subtract(blue, competing)
        mask = strength.point(
            lambda value: 255 if value >= threshold else 0
        )
        prepared = ImageOps.invert(mask).resize(
            (
                source.width * CAPTCHA_SCALE_FACTOR,
                source.height * CAPTCHA_SCALE_FACTOR,
            ),
            Image.Resampling.LANCZOS,
        )
    return _image_bytes(prepared)


def _cropped_variants(image_bytes: bytes) -> tuple[bytes, ...]:
    with Image.open(BytesIO(image_bytes)) as source:
        source.load()
        images = tuple(
            source.crop(
                (0, 0, max(1, int(source.width * ratio)), source.height)
            )
            for ratio in DDDDOCR_CROP_RATIOS
        )
    return tuple(_image_bytes(image) for image in images)


def _image_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@lru_cache(maxsize=1)
def _rapidocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR()


@lru_cache(maxsize=1)
def _ddddocr_engine():
    import ddddocr

    return ddddocr.DdddOcr(show_ad=False)

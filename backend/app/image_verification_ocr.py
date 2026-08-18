from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from statistics import mean
from threading import Lock
from typing import Any

from PIL import Image, ImageChops, ImageOps

CAPTCHA_SCALE_FACTOR = 4
BLUE_MASK_THRESHOLDS = (20, 30, 40)
DDDDOCR_CROP_RATIOS = (1.0, 0.75, 0.65)
DDDDOCR_CONFIDENCE = 0.80
RAPIDOCR_INFERENCE_LOCK = Lock()
DDDDOCR_INFERENCE_LOCK = Lock()


class _RapidOcrRecognizer:
    def __init__(self) -> None:
        rapidocr, text_recognizer, text_rec_input, load_image = (
            _rapidocr_components()
        )
        loader = rapidocr.__new__(rapidocr)
        config = loader._load_config(None, None)
        config.Rec.engine_cfg = config.EngineConfig[
            config.Rec.engine_type.value
        ]
        config.Rec.font_path = config.Global.font_path
        config.Rec.model_root_dir = config.Global.model_root_dir
        self._recognizer = text_recognizer(config.Rec)
        self._input_type = text_rec_input
        self._load_image = load_image()

    def __call__(self, image_bytes: bytes) -> Any:
        image = self._load_image(image_bytes)
        return self._recognizer(self._input_type(img=image))


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
    return _RapidOcrRecognizer()


def _rapidocr_components():
    from rapidocr import RapidOCR
    from rapidocr.ch_ppocr_rec.main import TextRecognizer
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    from rapidocr.utils.load_image import LoadImage

    return RapidOCR, TextRecognizer, TextRecInput, LoadImage


@lru_cache(maxsize=1)
def _ddddocr_engine():
    import ddddocr

    return ddddocr.DdddOcr(show_ad=False)


def verify_engines_ready() -> tuple[str, str]:
    _rapidocr_engine()
    _ddddocr_engine()
    return ("rapidocr", "ddddocr")

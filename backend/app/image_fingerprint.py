from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError


PERCEPTUAL_HASH_SIZE = 8


def image_perceptual_hash(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            grayscale = image.convert("L").resize((PERCEPTUAL_HASH_SIZE, PERCEPTUAL_HASH_SIZE))
            pixels = list(grayscale.get_flattened_data())
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("image bytes are not a supported image") from exc
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):016x}"


def image_avatar_perceptual_hash(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            edge = min(image.size)
            left = (image.width - edge) // 2
            top = (image.height - edge) // 2
            square = image.crop((left, top, left + edge, top + edge))
            grayscale = square.convert("L").resize(
                (PERCEPTUAL_HASH_SIZE, PERCEPTUAL_HASH_SIZE),
                Image.Resampling.LANCZOS,
            )
            pixels = list(grayscale.get_flattened_data())
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("image bytes are not a supported image") from exc
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):016x}"


def perceptual_hash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


__all__ = ["image_avatar_perceptual_hash", "image_perceptual_hash", "perceptual_hash_distance"]

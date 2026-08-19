from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

from app.image_fingerprint import image_avatar_perceptual_hash, perceptual_hash_distance


def _source_image() -> Image.Image:
    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((300, 0, 899, 799), fill="navy")
    draw.ellipse((430, 180, 770, 520), fill="orange")
    return image


def _bytes(image: Image.Image, image_format: str, **save_options) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def test_avatar_hash_matches_telegram_center_crop_and_resize() -> None:
    source = _source_image()
    telegram_render = source.crop((200, 0, 1000, 800)).resize((640, 640), Image.Resampling.LANCZOS)

    local_hash = image_avatar_perceptual_hash(_bytes(source, "PNG"))
    remote_hash = image_avatar_perceptual_hash(_bytes(telegram_render, "JPEG", quality=88))

    assert perceptual_hash_distance(local_hash, remote_hash) <= 5


def test_avatar_hash_rejects_visually_different_square() -> None:
    source_hash = image_avatar_perceptual_hash(_bytes(_source_image(), "PNG"))
    different = Image.new("RGB", (640, 640), "black")
    draw = ImageDraw.Draw(different)
    draw.polygon(((0, 0), (640, 0), (0, 640)), fill="white")

    assert perceptual_hash_distance(source_hash, image_avatar_perceptual_hash(_bytes(different, "PNG"))) > 5

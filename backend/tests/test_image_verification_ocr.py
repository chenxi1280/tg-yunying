from types import SimpleNamespace

import pytest

from app import image_verification_ocr


pytestmark = pytest.mark.no_postgres


def test_rapidocr_engine_loads_only_recognition_model(monkeypatch) -> None:
    events: list[str] = []
    config = SimpleNamespace(
        Global=SimpleNamespace(font_path="font", model_root_dir="models"),
        EngineConfig={"onnxruntime": "engine-config"},
        Rec=SimpleNamespace(
            engine_type=SimpleNamespace(value="onnxruntime"),
            engine_cfg=None,
            font_path=None,
            model_root_dir=None,
        ),
    )

    class FakeRapidOcr:
        def __init__(self) -> None:
            raise AssertionError("full RapidOCR must not be initialized")

        def _load_config(self, _path, _params):
            events.append("config")
            return config

    class FakeRecognizer:
        def __init__(self, rec_config) -> None:
            events.append(f"recognizer:{rec_config.engine_cfg}")

        def __call__(self, request):
            events.append(f"infer:{request.img}")
            return SimpleNamespace(txts=("12+7",), scores=(0.91,))

    class FakeInput:
        def __init__(self, *, img) -> None:
            self.img = img

    class FakeLoader:
        def __call__(self, payload):
            return f"loaded:{payload.decode()}"

    monkeypatch.setattr(
        image_verification_ocr,
        "_rapidocr_components",
        lambda: (FakeRapidOcr, FakeRecognizer, FakeInput, FakeLoader),
    )

    engine = image_verification_ocr._RapidOcrRecognizer()
    result = engine(b"captcha")

    assert result.txts == ("12+7",)
    assert events == [
        "config",
        "recognizer:engine-config",
        "infer:loaded:captcha",
    ]
    assert config.Rec.font_path == "font"
    assert config.Rec.model_root_dir == "models"

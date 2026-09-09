"""Public content rendering masks advertisements without changing audit rows."""

from pydantic import field_serializer

from app.content_safety import screened_public_text
from .api import ApiModel


class ScreenedContentOut(ApiModel):
    @field_serializer("content", "content_preview", "text", check_fields=False)
    def screen_public_content(self, value: str) -> str:
        return screened_public_text(value)

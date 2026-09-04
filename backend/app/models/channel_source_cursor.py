from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelSourcePageCursor(Base):
    __tablename__ = "channel_source_page_cursors"

    listener_source_state_id: Mapped[str] = mapped_column(String(36),
        ForeignKey("listener_source_state.id", ondelete="CASCADE"), primary_key=True)
    page_state: Mapped[dict] = mapped_column(JSON, default=dict)

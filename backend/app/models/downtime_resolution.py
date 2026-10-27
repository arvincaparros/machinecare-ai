
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class DowntimeResolution(Base):
    __tablename__ = "downtime_resolutions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("downtime_incidents.id"),
        unique=True,
        nullable=False
    )

    root_cause: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)
    preventive_action: Mapped[str | None] = mapped_column(Text)

    incident: Mapped["DowntimeIncident"] = relationship(
        "DowntimeIncident",
        back_populates="resolution"
    )

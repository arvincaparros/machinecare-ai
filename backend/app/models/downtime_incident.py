
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class DowntimeIncident(Base):
    __tablename__ = "downtime_incidents"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    incident_id: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False
    )

    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id"), nullable=False
    )

    production_line: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)

    product: Mapped[str | None] = mapped_column(String(150))
    issue_category: Mapped[str | None] = mapped_column(String(100))
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    downtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    responsible_team: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), nullable=False)

    machine: Mapped["Machine"] = relationship(
        "Machine", back_populates="incidents"
    )

    resolution: Mapped["DowntimeResolution | None"] = relationship(
        "DowntimeResolution",
        back_populates="incident",
        uselist=False
    )

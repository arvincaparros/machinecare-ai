
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True
    )

    machine_code: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )

    name: Mapped[str] = mapped_column(
        String(150), nullable=False
    )

    production_line: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    incidents: Mapped[list["DowntimeIncident"]] = relationship(
        "DowntimeIncident",
        back_populates="machine"
    )

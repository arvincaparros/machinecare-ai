
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import DowntimeIncident


def get_resolved_incidents(db: Session) -> list[DowntimeIncident]:
    """Retrieve closed incidents with machine and resolution."""

    statement = (
        select(DowntimeIncident)
        .options(
            joinedload(DowntimeIncident.machine),
            joinedload(DowntimeIncident.resolution)
        )
        .where(DowntimeIncident.status == "Closed")
        .order_by(DowntimeIncident.id)
    )

    return list(db.scalars(statement).all())


def get_incident_by_number(
    db: Session,
    incident_number: str
) -> DowntimeIncident | None:
    """Retrieve an incident using its business incident ID."""

    statement = (
        select(DowntimeIncident)
        .options(
            joinedload(DowntimeIncident.machine),
            joinedload(DowntimeIncident.resolution)
        )
        .where(DowntimeIncident.incident_id == incident_number)
    )

    return db.scalar(statement)

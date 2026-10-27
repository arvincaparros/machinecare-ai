
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import DowntimeIncident, Machine


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


def get_machines(db: Session) -> list[Machine]:
    """Retrieve every machine by code, for the import picklist."""

    statement = select(Machine).order_by(Machine.machine_code)

    return list(db.scalars(statement).all())


def find_known_machine_codes(
    db: Session,
    machine_codes: Collection[str]
) -> set[str]:
    """Return which of these machine codes exist, matched exactly."""

    codes = list(machine_codes)

    if not codes:
        return set()

    statement = select(Machine.machine_code).where(
        Machine.machine_code.in_(codes)
    )

    return set(db.scalars(statement).all())


def find_machines_by_code(
    db: Session,
    machine_codes: Collection[str]
) -> dict[str, Machine]:
    """Map each requested machine code to its stored machine."""

    codes = list(machine_codes)

    if not codes:
        return {}

    statement = select(Machine).where(Machine.machine_code.in_(codes))

    return {
        machine.machine_code: machine
        for machine in db.scalars(statement).all()
    }


def find_existing_incident_ids(
    db: Session,
    incident_ids: Collection[str]
) -> set[str]:
    """Return which of these business incident IDs already exist."""

    ids = list(incident_ids)

    if not ids:
        return set()

    statement = select(DowntimeIncident.incident_id).where(
        DowntimeIncident.incident_id.in_(ids)
    )

    return set(db.scalars(statement).all())

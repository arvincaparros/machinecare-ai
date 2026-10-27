"""The write side of the incident import.

Step 1 parses an upload and Step 2 classifies it against PostgreSQL
without writing. This module holds the only code that inserts import
rows:

* a second check of incident IDs and machine codes, inside the same
  transaction as the inserts, because a preview is a reading of the
  database and not a reservation of it;
* one ``commit()`` after every incident and its resolution has been
  added, so a failure on any row leaves the whole file out;
* two ways the database can refuse the rows -- a unique-constraint
  conflict from a simultaneous import, and an insert that fails for any
  other reason -- both answered by rolling back every row. Nothing here
  updates, deletes or replaces an existing record.

Indexing is deliberately absent. It runs after this module's ``commit``
and must never decide whether the rows are kept. The upload digest that
ties a commit to the previewed file lives in
``app.services.incident_import_digest``, which needs no database.

``row.status`` is always ``Closed``: Step 1's parser rejects any other
value (``ACCEPTED_STATUS``), so it cannot reach an insert. The
incident's own ``production_line`` is stored as supplied; it is never
derived from the machine it references.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import DowntimeIncident, DowntimeResolution, Machine
from app.repositories import incident_repository
from app.services.incident_import_preview import (
    EXISTING_INCIDENT_ID,
    UNKNOWN_MACHINE,
)
from app.services.incident_import_service import (
    ImportRowError,
    IncidentImportRow,
)


class IncidentImportStaleData(Exception):
    """Rows the preview accepted can no longer be inserted.

    Either an incident ID was inserted by somebody else after the
    preview, or a machine the file references has disappeared. Both are
    answered by asking the client to preview again; neither is repaired
    by overwriting what is stored now.
    """

    def __init__(
        self,
        *,
        incident_ids: Collection[str] = (),
        machine_codes: Collection[str] = (),
    ) -> None:
        self.incident_ids = tuple(incident_ids)
        self.machine_codes = tuple(machine_codes)
        super().__init__("The database changed after the preview.")


class IncidentImportInsertFailed(Exception):
    """An insert failed for a reason the uploaded file cannot fix."""


@dataclass(frozen=True)
class ImportCommitOutcome:
    """What one commit wrote, and what it deliberately left alone."""

    imported_rows: tuple[IncidentImportRow, ...]
    skipped_existing_rows: int

    @property
    def imported_row_count(self) -> int:
        return len(self.imported_rows)

    @property
    def imported_incident_ids(self) -> tuple[str, ...]:
        return tuple(row.incident_id for row in self.imported_rows)


def commit_incident_import(
    session: Session,
    rows: Sequence[IncidentImportRow],
    *,
    skip_existing: bool,
) -> ImportCommitOutcome:
    """Insert these rows and their resolutions, or insert nothing.

    ``rows`` are the eligible rows a preview produced for this request.
    They are checked again here, in the transaction that inserts them,
    so an ID or a machine that changed since the preview is caught
    before any row is written. The unique constraint on
    ``downtime_incidents.incident_id`` stays the final authority: an
    insert it refuses rolls the whole file back.

    ``skip_existing`` may drop rows that appeared after the preview, but
    it never updates them. Any other error raises, and the caller
    reports a failure; the database keeps whatever it had.
    """

    wanted = list(rows)
    skipped = 0

    if wanted:
        existing = incident_repository.find_existing_incident_ids(
            session,
            [row.incident_id for row in wanted],
        )

        if existing:
            if not skip_existing:
                raise IncidentImportStaleData(
                    incident_ids=sorted(existing)
                )

            wanted = [
                row
                for row in wanted
                if row.incident_id not in existing
            ]
            skipped = len(existing)

    if not wanted:
        return ImportCommitOutcome((), skipped)

    machines = incident_repository.find_machines_by_code(
        session,
        {row.machine for row in wanted},
    )
    missing = sorted({
        row.machine
        for row in wanted
        if row.machine not in machines
    })

    if missing:
        raise IncidentImportStaleData(machine_codes=missing)

    incidents = [
        _build_incident(row, machines[row.machine])
        for row in wanted
    ]

    try:
        session.add_all(incidents)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        conflicting = _ids_that_exist_now(session, wanted)

        if conflicting:
            raise IncidentImportStaleData(
                incident_ids=conflicting
            ) from None

        raise IncidentImportInsertFailed() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise IncidentImportInsertFailed() from error

    return ImportCommitOutcome(tuple(wanted), skipped)


def stale_data_errors(
    stale: IncidentImportStaleData,
    rows: Sequence[IncidentImportRow],
) -> tuple[ImportRowError, ...]:
    """Turn a stale-data rejection into the errors a client can act on."""

    errors: list[ImportRowError] = []
    incident_ids = set(stale.incident_ids)
    machine_codes = set(stale.machine_codes)

    for row in rows:
        if row.incident_id in incident_ids:
            errors.append(
                ImportRowError(
                    row.row_number,
                    "Incident ID",
                    EXISTING_INCIDENT_ID,
                    f"Incident ID {row.incident_id} was inserted after "
                    "the preview was taken. Nothing was imported; "
                    "preview the file again to see the current state.",
                )
            )

        if row.machine in machine_codes:
            errors.append(
                ImportRowError(
                    row.row_number,
                    "Machine",
                    UNKNOWN_MACHINE,
                    f"Machine code {row.machine} no longer exists. "
                    "Correct the code or create the machine first; an "
                    "import never creates machines.",
                )
            )

    return tuple(sorted(errors, key=lambda error: (error.row, error.field)))


def _build_incident(
    row: IncidentImportRow,
    machine: Machine,
) -> DowntimeIncident:
    """Build one incident with its one-to-one resolution attached."""

    incident = DowntimeIncident(
        incident_id=row.incident_id,
        machine_id=machine.id,
        production_line=row.production_line,
        date=row.date,
        product=row.product,
        issue_category=row.issue_category,
        cause=row.cause,
        downtime_minutes=row.downtime_minutes,
        responsible_team=row.responsible_team,
        status=row.status,
    )
    incident.resolution = DowntimeResolution(
        root_cause=row.root_cause,
        action_taken=row.action_taken,
        preventive_action=row.preventive_action,
    )

    return incident


def _ids_that_exist_now(
    session: Session,
    rows: Sequence[IncidentImportRow],
) -> tuple[str, ...]:
    """Read which IDs exist once a failed transaction has rolled back.

    This tells a lost race on ``incident_id`` apart from any other
    insert failure, and it names the rows the client has to change.
    """

    try:
        existing = incident_repository.find_existing_incident_ids(
            session,
            [row.incident_id for row in rows],
        )
    except SQLAlchemyError:
        return ()

    return tuple(sorted(existing))

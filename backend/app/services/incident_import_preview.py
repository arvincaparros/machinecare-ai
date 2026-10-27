"""Database-aware classification of a validated import file.

Step 1's parser validates one upload against the schema and knows
nothing about PostgreSQL. This module adds the two checks that need the
database -- machine codes and existing incident IDs -- and decides, per
row, whether it is new, skipped or invalid.

Everything here is a pure function over the parser result and two sets
of strings that a read-only query produced, so the counts can be tested
without a database. The caller owns the query; no ORM object is loaded
or mutated here.

Count semantics, which the response schema also documents::

    total_rows == invalid_rows + skipped_existing_rows + eligible_rows

Blank rows and the header are never counted. A row that the parser
already rejected is never checked against the database, so an existing
incident ID on a row with a field error is simply part of
``invalid_rows``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from app.services.incident_import_service import (
    ImportRowError,
    IncidentImportResult,
    IncidentImportRow,
)


# Row-level error codes added by the database checks.
UNKNOWN_MACHINE = "unknown_machine"
EXISTING_INCIDENT_ID = "existing_incident_id"

# Import outcome for a row that passed every check.
STATE_NEW = "new"
STATE_SKIPPED = "skipped_existing"


@dataclass(frozen=True)
class ImportPreviewRow:
    """One row that passed every check, with its import outcome."""

    row: IncidentImportRow
    state: str


@dataclass(frozen=True)
class IncidentImportPreview:
    """Rows, row errors and counts for one previewed file."""

    file_name: str
    skip_existing: bool
    total_rows: int
    rows: tuple[ImportPreviewRow, ...]
    errors: tuple[ImportRowError, ...]

    @property
    def valid_row_count(self) -> int:
        return len(self.rows)

    @property
    def skipped_rows(self) -> tuple[ImportPreviewRow, ...]:
        return tuple(
            item
            for item in self.rows
            if item.state == STATE_SKIPPED
        )

    @property
    def skipped_row_count(self) -> int:
        return len(self.skipped_rows)

    @property
    def eligible_rows(self) -> tuple[ImportPreviewRow, ...]:
        return tuple(
            item
            for item in self.rows
            if item.state == STATE_NEW
        )

    @property
    def eligible_row_count(self) -> int:
        return len(self.eligible_rows)

    @property
    def invalid_row_count(self) -> int:
        return len({error.row for error in self.errors})

    @property
    def is_valid(self) -> bool:
        return not self.errors


def collect_lookup_values(
    parsed: IncidentImportResult,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the machine codes and IDs the preview has to look up.

    Only rows the parser accepted are checked, and the values are sorted
    so the generated query is deterministic.
    """

    machine_codes = sorted({
        row.machine
        for row in parsed.valid_rows
    })
    incident_ids = sorted({
        row.incident_id
        for row in parsed.valid_rows
    })

    return tuple(machine_codes), tuple(incident_ids)


def build_import_preview(
    parsed: IncidentImportResult,
    *,
    known_machine_codes: Collection[str],
    existing_incident_ids: Collection[str],
    skip_existing: bool = False,
) -> IncidentImportPreview:
    """Classify every parser-approved row against the database facts."""

    known_machines = set(known_machine_codes)
    existing_ids = set(existing_incident_ids)

    rows: list[ImportPreviewRow] = []
    errors: list[ImportRowError] = []

    for row in parsed.valid_rows:
        if row.machine not in known_machines:
            errors.append(
                ImportRowError(
                    row.row_number,
                    "Machine",
                    UNKNOWN_MACHINE,
                    f"Machine code {row.machine} does not exist. Correct "
                    "the code or create the machine first; an import "
                    "never creates machines.",
                )
            )
            continue

        if row.incident_id in existing_ids:
            if skip_existing:
                rows.append(ImportPreviewRow(row, STATE_SKIPPED))
            else:
                errors.append(
                    ImportRowError(
                        row.row_number,
                        "Incident ID",
                        EXISTING_INCIDENT_ID,
                        f"Incident ID {row.incident_id} already exists. "
                        "Choose a new incident ID, or preview again with "
                        "skip_existing to leave the existing incident "
                        "untouched.",
                    )
                )
            continue

        rows.append(ImportPreviewRow(row, STATE_NEW))

    combined = sorted(
        (*parsed.errors, *errors),
        key=lambda error: (error.row, error.field),
    )

    return IncidentImportPreview(
        file_name=parsed.file_name,
        skip_existing=skip_existing,
        total_rows=parsed.total_rows,
        rows=tuple(rows),
        errors=tuple(combined),
    )

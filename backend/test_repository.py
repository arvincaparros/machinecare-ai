
from app.db.database import SessionLocal
from app.repositories.incident_repository import (
    get_resolved_incidents,
    get_incident_by_number,
)


def main():
    with SessionLocal() as db:
        incidents = get_resolved_incidents(db)

        print(f"Resolved incidents: {len(incidents)}")

        incident = get_incident_by_number(
            db,
            "DT-2026-001"
        )

        if incident:
            print(f"Incident: {incident.incident_id}")
            print(f"Machine: {incident.machine.machine_code}")
            print(f"Cause: {incident.cause}")

            if incident.resolution:
                print(
                    f"Root Cause: "
                    f"{incident.resolution.root_cause}"
                )


if __name__ == "__main__":
    main()


from datetime import date

from sqlalchemy import select

from app.db.database import SessionLocal
from app.models.machine import Machine
from app.models.downtime_incident import DowntimeIncident
from app.models.downtime_resolution import DowntimeResolution
from app.services.incident_retrieval_service import load_incidents


def seed_database():
    incidents = load_incidents()

    created_machines = 0
    created_incidents = 0

    with SessionLocal.begin() as db:

        for record in incidents:
            machine_code = record["Machine"]

            # Find existing machine
            machine = db.scalar(
                select(Machine).where(
                    Machine.machine_code == machine_code
                )
            )

            # Create machine if it doesn't exist
            if machine is None:
                machine = Machine(
                    machine_code=machine_code,
                    name=machine_code,
                    production_line=record["Production Line"]
                )

                db.add(machine)
                db.flush()

                created_machines += 1

            # Avoid duplicate incidents
            existing = db.scalar(
                select(DowntimeIncident).where(
                    DowntimeIncident.incident_id
                    == record["Incident ID"]
                )
            )

            if existing:
                continue

            # Create downtime incident
            incident = DowntimeIncident(
                incident_id=record["Incident ID"],
                machine_id=machine.id,
                production_line=record["Production Line"],
                date=date.fromisoformat(record["Date"]),
                product=record["Product"],
                issue_category=record["Issue Category"],
                cause=record["Cause"],
                downtime_minutes=record["Downtime (min)"],
                responsible_team=record["Responsible Team"],
                status=record["Status"]
            )

            # Create related resolution
            resolution = DowntimeResolution(
                incident=incident,
                root_cause=record["Root Cause"],
                action_taken=record["Action Taken"],
                preventive_action=record["Preventive Action"]
            )

            db.add(incident)
            db.add(resolution)

            created_incidents += 1

    print("Database seeding completed!")
    print(f"Created machines: {created_machines}")
    print(f"Created incidents: {created_incidents}")


if __name__ == "__main__":
    seed_database()

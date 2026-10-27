
from app.db.database import Base

# Import models so SQLAlchemy registers their tables
from app.models.machine import Machine
from app.models.downtime_incident import DowntimeIncident
from app.models.downtime_resolution import DowntimeResolution


def main():
    print("Registered tables:")

    for table_name in Base.metadata.tables:
        print(f"- {table_name}")

    print("\nModels loaded successfully!")


if __name__ == "__main__":
    main()

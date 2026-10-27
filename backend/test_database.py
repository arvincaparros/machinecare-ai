
from sqlalchemy import text

from app.db.database import engine


def main():
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database(), current_user")
        )

        row = result.one()

        print("Database:", row[0])
        print("User:", row[1])
        print("SQLAlchemy connection successful!")


if __name__ == "__main__":
    main()

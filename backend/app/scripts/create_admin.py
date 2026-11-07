"""One-time CLI that creates an administrator account.

Usage (from ``backend/``):

    python -m app.scripts.create_admin --username alice --display-name "Alice"

The password is read from the ``ADMIN_PASSWORD`` environment variable
or, failing that, prompted on the terminal; it is never an argv value
(where it would land in shell history and process listings).

Per the plan, the first administrator is created by this script —
never by a seeded row in a migration.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Create an administrator account."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("ADMIN_USERNAME", ""),
        help="Username (default: ADMIN_USERNAME env var).",
    )
    parser.add_argument(
        "--display-name",
        dest="display_name",
        default=os.environ.get("ADMIN_DISPLAY_NAME", ""),
        help="Human-readable name (default: ADMIN_DISPLAY_NAME env var).",
    )
    args = parser.parse_args(argv)

    username = args.username.strip()
    if not username:
        parser.error("A username is required.")

    password = os.environ.get("ADMIN_PASSWORD", "")

    if not password:
        password = getpass.getpass(f"Password for {username}: ")
        confirm = getpass.getpass("Confirm password: ")

        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 2

    # Imports are deferred: the database module reads its settings at
    # import time, so this script only pays that cost when it runs.
    from sqlalchemy import select

    from app.core.password_policy import (
        describe_violations,
        validate_password,
    )
    from app.core.permissions import ROLE_ADMIN
    from app.core.security import hash_password
    from app.core.settings import get_settings
    from app.db.database import SessionLocal
    from app.models import Role, User

    settings = get_settings()

    violations = validate_password(
        password,
        username=username,
        min_length=settings.password_min_length,
    )

    if violations:
        print(
            "Password rejected: "
            + describe_violations(
                violations, min_length=settings.password_min_length
            ),
            file=sys.stderr,
        )
        return 2

    with SessionLocal() as db:
        normalized = username.lower()

        if db.scalar(
            select(User).where(User.username == normalized)
        ):
            print("That username already exists.", file=sys.stderr)
            return 1

        role = db.scalar(select(Role).where(Role.name == ROLE_ADMIN))

        if role is None:
            print(
                "The admin role row is missing; run the a1 migration "
                "first (alembic upgrade head).",
                file=sys.stderr,
            )
            return 1

        display_name = args.display_name.strip() or username

        db.add(
            User(
                username=normalized,
                display_name=display_name,
                password_hash=hash_password(password),
                roles=[role],
            )
        )
        db.commit()

    print(f"Administrator '{normalized}' created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

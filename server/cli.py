import argparse
import asyncio
import getpass
import sys

from server.app.db import AsyncSessionLocal, init_db
from server.app.user_service import create_user, UsernameTakenError


async def create_admin(username: str, display_name: str, password: str) -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        try:
            await create_user(db, username=username, display_name=display_name, password=password, is_admin=True)
        except UsernameTakenError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    print(f"Admin user '{username}' created.")


def _prompt_create_admin() -> None:
    username = input("Username: ").strip()
    display_name = input("Display name: ").strip()
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match", file=sys.stderr)
        sys.exit(1)
    asyncio.run(create_admin(username, display_name, password))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m server.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-admin", help="Interactively create an admin user")

    args = parser.parse_args()
    if args.command == "create-admin":
        _prompt_create_admin()


if __name__ == "__main__":
    main()

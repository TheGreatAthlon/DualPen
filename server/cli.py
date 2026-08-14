import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from server import backup_service
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


def _run_backup(dest: str | None, keep: int | None) -> None:
    dest_dir = Path(dest) if dest else None
    if dest_dir is None:
        env_dest = os.environ.get("COLLAB_EDITOR_BACKUP_PATH")
        dest_dir = Path(env_dest) if env_dest else None
    archive_path = backup_service.run_backup(dest_dir, keep)
    print(f"Backup written to {archive_path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m server.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-admin", help="Interactively create an admin user")

    backup_parser = subparsers.add_parser(
        "backup", help="Back up the database, document store, and encryption key into one archive"
    )
    backup_parser.add_argument(
        "--dest",
        help="Directory to write the backup archive into "
        "(default: $COLLAB_EDITOR_BACKUP_PATH, or <repo>/backups)",
    )
    backup_parser.add_argument(
        "--keep",
        type=int,
        help="Delete older archives in the destination directory beyond this count",
    )

    args = parser.parse_args()
    if args.command == "create-admin":
        _prompt_create_admin()
    elif args.command == "backup":
        _run_backup(args.dest, args.keep)


if __name__ == "__main__":
    main()

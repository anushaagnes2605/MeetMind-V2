"""Inspect or provision a MeetMind account in the configured database.

The same code path as the API is used, so what this reports is exactly what
POST /auth/login will do.

Examples
--------
Check the production account (run from backend/ with the Turso variables set):

    python account_tool.py --email you@example.com --require-turso

Provision the same account in an empty production database:

    python account_tool.py --email you@example.com --create --require-turso

The password is read from the MEETMIND_ACCOUNT_PASSWORD environment variable
or prompted for, so it never appears in shell history or in this script.
Passwords are never printed, and password hashes are never printed.
"""

import argparse
import getpass
import os
import sys

os.environ.setdefault("MEETMIND_SKIP_SEED", "1")

import main  # noqa: E402  (env must be prepared before importing the app)


def read_password(confirm: bool, supplied: str | None) -> str:
    if supplied:
        return supplied
    env_password = os.getenv("MEETMIND_ACCOUNT_PASSWORD", "")
    if env_password:
        return env_password
    password = getpass.getpass("Password: ")
    if confirm:
        if getpass.getpass("Repeat password: ") != password:
            raise SystemExit("Passwords do not match.")
    if not password:
        raise SystemExit("A password is required.")
    return password


def describe_database() -> str:
    if main.TURSO_DATABASE_URL:
        url = main.TURSO_DATABASE_URL.split("://", 1)[-1]
        host = url.split("/", 1)[0]
        return f"Turso ({host})"
    return f"SQLite file ({main.DATABASE_PATH})"


def find_user(email: str):
    with main.get_db() as db:
        return db.execute(
            """
            SELECT id, email, password_hash, display_name, role, is_active, created_at
            FROM users WHERE email = ?
            """,
            (email,),
        ).fetchone()


def print_account(user, email: str, password: str) -> bool:
    print(f"Database: {describe_database()}")
    print(f"Account:  {email}")
    if user is None:
        print("  exists:        no")
        return False
    matches = main.verify_password(password, user["password_hash"])
    print("  exists:        yes")
    print(f"  active:        {'yes' if user['is_active'] else 'no'}")
    print(f"  role:          {user['role']}")
    print(f"  display name:  {user['display_name']}")
    print(f"  created:       {user['created_at']}")
    print(f"  password match:{' yes' if matches else ' NO'}")
    return matches


def create_account(email: str, password: str) -> None:
    if find_user(email) is not None:
        raise SystemExit(
            "An account with this email already exists. Nothing was changed. "
            "Use --reset-password only if you intend to change its password."
        )
    if len(password) < 8:
        raise SystemExit("MeetMind requires passwords of at least 8 characters.")
    display_name = email.split("@", 1)[0]
    with main.get_db() as db:
        db.execute(
            """
            INSERT INTO users (email, password_hash, display_name, role)
            VALUES (?, ?, ?, 'admin')
            """,
            (email, main.hash_password(password), display_name),
        )
    print(f"Created {email} in {describe_database()}. Existing users were not touched.")


def reset_password(email: str, password: str) -> None:
    if find_user(email) is None:
        raise SystemExit("No account with this email exists; use --create instead.")
    if len(password) < 8:
        raise SystemExit("MeetMind requires passwords of at least 8 characters.")
    with main.get_db() as db:
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE email = ?",
            (main.hash_password(password), email),
        )
    print(f"Password hash replaced for {email} in {describe_database()}.")


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True, help="account email address")
    parser.add_argument(
        "--require-turso",
        action="store_true",
        help="fail instead of inspecting the local SQLite file when Turso is not configured",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--create", action="store_true", help="create the account if it is missing")
    group.add_argument(
        "--reset-password",
        action="store_true",
        help="change the password of an existing account (destructive, opt-in only)",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    if not main.EMAIL_PATTERN.match(email):
        raise SystemExit("Enter a valid email address.")

    if args.require_turso and not main.TURSO_DATABASE_URL:
        raise SystemExit(
            "TURSO_DATABASE_URL is not set, so this would inspect the local SQLite "
            "file instead of the production database."
        )

    password = read_password(confirm=args.create or args.reset_password, supplied=None)

    if args.create:
        create_account(email, password)
        print_account(find_user(email), email, password)
        return 0
    if args.reset_password:
        reset_password(email, password)
        print_account(find_user(email), email, password)
        return 0

    matches = print_account(find_user(email), email, password)
    if matches:
        print("Result: this account can sign in with the supplied password.")
        return 0
    print("Result: POST /auth/login would reject this password.")
    return 1


if __name__ == "__main__":
    sys.exit(main_cli())

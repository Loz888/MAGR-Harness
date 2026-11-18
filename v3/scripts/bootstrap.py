"""Create only missing, local development secrets; never print their values."""

import os
import secrets
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values, set_key


def local_compose_password(database_url: str) -> str | None:
    """Recover only the password of this project's default local database."""
    if "${" in database_url:
        return None
    try:
        url = urlsplit(database_url)
        if (
            url.scheme in {"postgres", "postgresql"}
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
            and url.port == 55433
            and unquote(url.username or "") == "mgar_v3"
            and unquote(url.path) == "/mgar_v3"
            and url.password
        ):
            password = unquote(url.password)
            # dotenv and Compose differ on single-quoted backslash escapes.
            # Require explicit configuration instead of changing such a secret.
            return password if "\\" not in password else None
    except ValueError:
        pass
    return None


def bootstrap(root: Path) -> None:
    target = root / ".env"
    if target.exists():
        existing = dotenv_values(target, encoding="utf-8-sig", interpolate=False)
        if not existing.get("MGAR_V3_DB_PASSWORD"):
            password = local_compose_password(existing.get("MGAR_V3_DATABASE_URL") or "")
            if password is not None:
                set_key(target, "MGAR_V3_DB_PASSWORD", password, quote_mode="always")
                target.chmod(0o600)
                print("Existing .env preserved; restored MGAR_V3_DB_PASSWORD from local DSN")
                return
            print("Existing .env preserved; local Compose needs explicit MGAR_V3_DB_PASSWORD")
            return
        print("Existing .env preserved")
        return
    password = secrets.token_hex(24)
    contents = (
        f"MGAR_V3_DB_PASSWORD={password}\n"
        f"MGAR_V3_DATABASE_URL=postgresql://mgar_v3:{password}@127.0.0.1:55433/mgar_v3\n"
        "MGAR_V3_REDIS_URL=redis://127.0.0.1:56379/0\n"
        f"MGAR_V3_LOCAL_TOKEN={secrets.token_hex(32)}\n"
        "MGAR_V3_ALLOW_PAID=0\nMGAR_V3_ALLOW_EXTERNAL_WRITE=0\n"
    )
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    print("Created local .env (0600); no external credentials configured")


if __name__ == "__main__":
    bootstrap(Path(__file__).resolve().parents[1])

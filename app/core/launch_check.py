"""Read-only production preflight: python -m app.core.launch_check."""

import asyncio
import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.clients.database import create_database_engine
from app.env import Settings


def production_config(settings: Settings) -> Settings:
    return Settings(
        _env_file=None, **{**settings.model_dump(), "app_environment": "production"}
    )


async def check_database(settings: Settings) -> bool:
    engine = create_database_engine(settings)
    try:
        async with asyncio.timeout(5), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            actual = set(
                (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).scalars()
            )
        path = Path(__file__).resolve().parents[2]
        config = Config(str(path / "alembic.ini"))
        config.set_main_option("script_location", str(path / "app/db/migrations"))
        expected = set(ScriptDirectory.from_config(config).get_heads())
        return actual == expected
    finally:
        await engine.dispose()


async def main() -> int:
    try:
        settings = production_config(Settings())
    except ValidationError:
        print(
            json.dumps(
                {
                    "ready": False,
                    "check": "production configuration",
                    "detail": "Fix production environment variables; see docs/launch-readiness.md.",
                }
            )
        )
        return 1
    try:
        database_ready = await check_database(settings)
    except (SQLAlchemyError, TimeoutError, OSError):
        print(
            json.dumps(
                {
                    "ready": False,
                    "check": "database",
                    "detail": "Database connection or migration table is unavailable.",
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ready": database_ready,
                "check": "database migrations",
                "detail": "Current migration head confirmed."
                if database_ready
                else "Run alembic upgrade head before launch.",
                "providers": "Not contacted; verify configured integrations separately.",
            }
        )
    )
    return 0 if database_ready else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

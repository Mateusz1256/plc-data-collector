"""Programmatic Alembic migration runner that ships inside the package."""

from __future__ import annotations

from importlib import resources

from alembic import command
from alembic.config import Config


def run_migrations(database_url: str) -> None:
    """Upgrade the database at ``database_url`` to the latest schema revision.

    The migration scripts are located through ``importlib.resources``, so this
    works from any working directory and does not need ``alembic.ini``.
    """
    script_location = resources.files("plc_data_collector.persistence").joinpath(
        "migrations"
    )
    config = Config()
    config.set_main_option("script_location", str(script_location))
    # Alembic stores options in a ConfigParser, where "%" starts interpolation.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")

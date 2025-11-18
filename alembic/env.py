import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# alembic config
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Thêm project root vào sys.path để import package 'app'
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env nếu có
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

# Import settings và Base metadata từ project
from app.configs.settings import settings  # noqa: E402
from app.database.database import Base  # noqa: E402

# Import tất cả models để đảm bảo metadata đầy đủ
try:
    import app.models  # noqa: F401,E402
except Exception:
    # Nếu package models có module con không tự import được, Alembic vẫn dùng Base.metadata
    pass

target_metadata = Base.metadata

# Nếu DATABASE_URL dùng async driver, chuyển sang sync driver cho alembic
database_url = os.getenv("DATABASE_URL") or getattr(settings, "DATABASE_URL", None)
if database_url and "+asyncpg" in database_url:
    database_url = database_url.replace("+asyncpg", "")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

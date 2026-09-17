from psycopg_pool import ConnectionPool

from relay.config import settings

pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=1,
    max_size=5,
)

import psycopg
from psycopg.rows import dict_row
from config import settings

def get_db_conn():
    """
    Returns a PostgreSQL connection using psycopg with dict_row factory.
    Use it as a context manager:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)

def get_db():
    """
    Dependency to be used in FastAPI endpoints.
    Ensures connection is closed after the request.
    """
    with get_db_conn() as conn:
        yield conn

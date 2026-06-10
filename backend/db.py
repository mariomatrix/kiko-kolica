import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

# Load local environment variables
load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://kolica_user:SigurnaLozinka123!@192.168.100.181:5432/kolica_dev"
)

def get_db():
    """
    Returns a PostgreSQL connection using psycopg with dict_row factory.
    Use it as a context manager:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

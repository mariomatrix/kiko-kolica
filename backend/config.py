from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import os

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Auth & Sessions
    KOLICA_SESSION_SECRET: str = "change-me-in-production"

    # CORS
    KOLICA_CORS_ORIGINS: str = "*"

    # GRM Paths
    REQUEST_INBOX: str = "/mnt/nas/004_Konstrukcija/010_BI_File_Drop/REQUEST"
    RESPONSE_ROOT: str = "/mnt/nas/004_Konstrukcija/010_BI_File_Drop/ALDO_POC/responses"
    ERROR_ROOT: str = "/mnt/nas/004_Konstrukcija/010_BI_File_Drop/ALDO_POC/errors"

    # Migration
    SQLITE_DB_PATH: str = "kolica.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.KOLICA_CORS_ORIGINS.split(",")]

settings = Settings()

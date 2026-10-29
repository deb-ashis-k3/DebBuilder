from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:dev%40123@localhost:5432/debbuilder"
    encryption_key: str
    # Shared secret used by the LiveKit voice worker to call back into the API.
    internal_token: str
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173"
    max_node_executions: int = 500
    # Executions running at once; more wait their turn (backpressure for webhook bursts).
    max_concurrent_executions: int = 20
    db_pool_size: int = 20
    db_max_overflow: int = 30
    # Sign-in attempts per IP per minute (password and MFA steps).
    login_rate_limit: int = 10
    # First-run owner setup is only accepted from this machine unless explicitly allowed,
    # so nobody on the network can claim a fresh install before you do.
    allow_remote_setup: bool = False
    # Email for sign-in codes. Without SMTP_HOST, codes are written to the server console
    # (EMAIL_CODE_FALLBACK=log, for local development) or sign-in is refused (=off).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: str = "starttls"  # starttls | ssl | none
    mail_from: str = ""
    email_code_fallback: str = "log"

    @field_validator("encryption_key", "internal_token")
    @classmethod
    def _required(cls, v: str, info) -> str:
        if not v or len(v) < 16:
            raise ValueError(f"{info.field_name.upper()} is missing or too short in backend/.env (see .env.example)")
        return v

    @property
    def psycopg_url(self) -> str:
        """Plain libpq URL (for the LangGraph Postgres checkpointer)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()

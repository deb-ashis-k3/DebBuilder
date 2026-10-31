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
    # Voice workflows: "auto" = the backend runs a LiveKit voice worker for every active workflow's
    # agent name (voice/worker.py, restarted if it crashes); "off" = you run the workers yourself.
    voice_workers: str = "auto"
    # "auto" = replicas elect one leader (Postgres lock) that runs schedules, listeners and voice workers;
    # "off" = this replica only serves requests (e.g. web pods next to a dedicated worker pod).
    background_tasks: str = "auto"
    # Warm call processes each worker keeps ready (each holds the audio models in memory).
    voice_worker_idle_processes: int = 1
    # How workers reach this API; default http://127.0.0.1:$PORT (8000).
    internal_api_url: str = ""
    cors_origins: str = "http://localhost:5173"
    max_node_executions: int = 500
    # Files carried by items (binary data) and the Read/Write Files node's sandbox live under here.
    data_dir: str = "data"
    max_binary_mb: int = 100
    # Read/Write Files may only touch paths inside this folder (relative paths resolve against it).
    files_root: str = "data/files"
    # Execute Command runs shell commands on the server: off unless you enable it (like n8n 2.x).
    enable_execute_command: bool = False
    # Executions running at once per process; more wait their turn (backpressure for webhook bursts).
    # They mostly wait on network calls and hold no database connection while running, so this can be high.
    max_concurrent_executions: int = 200
    db_pool_size: int = 20
    db_max_overflow: int = 30
    # ---- execution history at volume (engine/store.py) ----
    # What to keep: "all" | "none" (per workflow overridable in its settings). Paused runs are always kept.
    executions_save_success: str = "all"
    executions_save_error: str = "all"
    executions_save_manual: bool = True
    # "full" keeps every node's items; "summary" keeps status, timings and counts only (much smaller rows).
    executions_save_data: str = "full"
    # The writer batches rows: every N ms, or sooner when this many are waiting; producers wait past max_pending.
    executions_flush_ms: int = 200
    executions_batch_size: int = 500
    executions_max_pending: int = 20000
    # Retention on the leader: delete history older than this (0 = keep forever); runs left "running" by a
    # crashed server are marked failed after this many hours.
    executions_max_age_days: int = 14
    executions_stale_after_hours: int = 12
    executions_prune_every_minutes: int = 15
    # Sign-in attempts per IP per minute (password and MFA steps).
    login_rate_limit: int = 10
    # First-run owner setup is only accepted from this machine unless explicitly allowed,
    # so nobody on the network can claim a fresh install before you do.
    allow_remote_setup: bool = False
    # Once the owner exists, the sign-in page offers "Sign up": new accounts are editors with their
    # own empty workspace, created only after the emailed code is entered. False = admins add users.
    allow_signup: bool = True
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

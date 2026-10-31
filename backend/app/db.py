import json
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, event, func, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True,
                             pool_size=get_settings().db_pool_size, max_overflow=get_settings().db_max_overflow)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lower-case
    username: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)  # lower-case, optional
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="editor")  # owner | admin | editor | viewer
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet-encrypted base32
    mfa_last_step: Mapped[int] = mapped_column(Integer, default=0)  # TOTP replay protection
    recovery_codes: Mapped[list] = mapped_column(JSONB, default=list)  # sha256 hashes, one-time
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """Browser sessions. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKey(Base):
    """`dbk_<prefix>_<secret>`; the prefix is the lookup key, only a SHA-256 of the full key is stored."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(20))  # never above the creator's role
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginChallenge(Base):
    """Second sign-in step: a one-time code emailed to the user (hash only, short-lived)."""

    __tablename__ = "login_challenges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # None for a pending owner setup: the account doesn't exist until the code is verified.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    purpose: Mapped[str] = mapped_column(String(20), default="login", server_default="login")  # login | reset | setup
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # setup only: encrypted pending account
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sends: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str] = mapped_column(String(64), default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class Folder(Base):
    """n8n-style folders; nested via parent_id (deleting a parent deletes its subfolders)."""

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    nodes: Mapped[list] = mapped_column(JSONB, default=list)
    edges: Mapped[list] = mapped_column(JSONB, default=list)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )
    # Draft / publish: production (triggers, webhooks, schedules) runs the published version; the editor edits
    # `nodes` / `edges` (the draft). The published graph is copied here so production lookups need no join.
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    published_nodes: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    published_edges: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # graph_hash of the draft, kept current on every save (see _draft_hash_on_update), so lists can tell "unpublished
    # changes" without loading and hashing every graph.
    draft_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Bumped on every save: the editor sends the revision it loaded and gets 409 if another tab saved since.
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def live_nodes(self) -> list:
        """What production runs: the published graph, or the draft if it was never published."""
        return (self.published_nodes or []) if self.published_version_id else (self.nodes or [])

    @property
    def live_edges(self) -> list:
        return (self.published_edges or []) if self.published_version_id else (self.edges or [])


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(100), index=True)
    data: Mapped[str] = mapped_column(Text)  # Fernet-encrypted JSON
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow
    )


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    # running | success | error | waiting | canceled (only running / waiting are indexed: see init_db)
    status: Mapped[str] = mapped_column(String(20), default="running")
    # manual | webhook | schedule | chat | whatsapp | voice | subworkflow | error
    mode: Mapped[str] = mapped_column(String(20), default="manual")
    trigger_node: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input: Mapped[list] = mapped_column(JSONB, default=list)
    output: Mapped[list] = mapped_column(JSONB, default=list)
    node_runs: Mapped[list] = mapped_column(JSONB, default=list)
    # The graph that ran, so history stays accurate after edits: by hash in workflow_snapshots (one row per
    # version, not per execution); older executions carry it inline.
    workflow_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    waiting_for: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


@event.listens_for(Workflow, "before_insert")
def _draft_hash_on_insert(mapper, connection, target: Workflow) -> None:
    target.draft_hash = graph_hash(target.nodes or [], target.edges or [])


@event.listens_for(Workflow, "before_update")
def _draft_hash_on_update(mapper, connection, target: Workflow) -> None:
    state = sa_inspect(target)
    if "nodes" in state.unloaded or "edges" in state.unloaded:
        return  # loaded without its graph (load_only), so the graph didn't change here
    if state.attrs.nodes.history.has_changes() or state.attrs.edges.history.has_changes():
        target.draft_hash = graph_hash(target.nodes or [], target.edges or [])


class WorkflowVersion(Base):
    """A published (or restorable) snapshot of a workflow's graph: n8n's workflow history. The workflow keeps
    editing a draft; production runs the version in workflows.published_version_id."""

    __tablename__ = "workflow_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    nodes: Mapped[list] = mapped_column(JSONB, default=list)
    edges: Mapped[list] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowSnapshot(Base):
    """One row per distinct workflow version that ran (content hash), shared by all its executions."""

    __tablename__ = "workflow_snapshots"

    hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExecutionStat(Base):
    """Finished executions per workflow, day and status: incremented by the execution writer, read by dashboards."""

    __tablename__ = "execution_stats"

    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"),
                                                   primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)


class ChatMessage(Base):
    """Conversation memory for AI nodes, keyed by session."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(300), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StaticData(Base):
    """Per-workflow key/value store (n8n's $getWorkflowStaticData)."""

    __tablename__ = "static_data"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)


def graph_hash(nodes: list, edges: list) -> str:
    """Content of a graph for "unpublished changes": editor-only pinned test data doesn't count."""
    import hashlib

    clean = [{k: v for k, v in n.items() if k != "pin_data"} for n in nodes or []]
    raw = json.dumps([clean, edges or []], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


async def init_db() -> None:
    async with engine.begin() as conn:
        # Replicas starting together would race to create the same tables: one at a time.
        await conn.execute(text("SELECT pg_advisory_xact_lock(233811)"))
        await conn.run_sync(Base.metadata.create_all)
        # create_all doesn't add columns to existing tables; upgrade databases created before folders.
        await conn.execute(text(
            "ALTER TABLE workflows ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES folders(id) ON DELETE SET NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflows_folder_id ON workflows (folder_id)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(32)"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)"))
        await conn.execute(text("ALTER TABLE login_challenges ADD COLUMN IF NOT EXISTS purpose VARCHAR(20) NOT NULL DEFAULT 'login'"))
        await conn.execute(text("ALTER TABLE login_challenges ADD COLUMN IF NOT EXISTS payload TEXT"))
        await conn.execute(text("ALTER TABLE login_challenges ALTER COLUMN user_id DROP NOT NULL"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_executions_workflow_started ON executions (workflow_id, started_at DESC)"))
        # Draft / publish (workflow_versions).
        for col, typ in (("published_version_id", "UUID"), ("published_version_number", "INTEGER"),
                         ("published_hash", "VARCHAR(40)"), ("published_nodes", "JSONB"), ("published_edges", "JSONB")):
            await conn.execute(text(f"ALTER TABLE workflows ADD COLUMN IF NOT EXISTS {col} {typ}"))
        # Workflows active before versions existed keep running exactly what they run now: their draft becomes v1.
        rows = (await conn.execute(text(
            "SELECT id, nodes, edges FROM workflows WHERE active AND published_version_id IS NULL"))).all()
        for wid, nodes, edges in rows:
            vid = uuid.uuid4()
            h = graph_hash(nodes or [], edges or [])
            await conn.execute(text(
                "INSERT INTO workflow_versions (id, workflow_id, number, nodes, edges, content_hash, note) "
                "VALUES (:id, :wid, 1, CAST(:n AS JSONB), CAST(:e AS JSONB), :h, 'Published automatically (before version history)')"),
                {"id": vid, "wid": wid, "n": json.dumps(nodes or []), "e": json.dumps(edges or []), "h": h})
            await conn.execute(text(
                "UPDATE workflows SET published_version_id = :vid, published_version_number = 1, published_hash = :h, "
                "published_nodes = CAST(:n AS JSONB), published_edges = CAST(:e AS JSONB) WHERE id = :wid"),
                {"vid": vid, "h": h, "n": json.dumps(nodes or []), "e": json.dumps(edges or []), "wid": wid})
        # Draft hash (fast workflow lists) and revision (conflict detection between tabs).
        await conn.execute(text("ALTER TABLE workflows ADD COLUMN IF NOT EXISTS draft_hash VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE workflows ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0"))
        while True:  # backfill in batches (rows written before the column existed)
            rows = (await conn.execute(text(
                "SELECT id, nodes, edges FROM workflows WHERE draft_hash IS NULL LIMIT 500"))).all()
            if not rows:
                break
            await conn.execute(text("UPDATE workflows SET draft_hash = :h WHERE id = :id"),
                               [{"id": wid, "h": graph_hash(nodes or [], edges or [])} for wid, nodes, edges in rows])
        # One number per version of a workflow. Concurrent publishes could once create duplicates: renumber them
        # (in creation order) before the unique index goes on.
        dupes = (await conn.execute(text(
            "SELECT workflow_id FROM workflow_versions GROUP BY workflow_id, number HAVING count(*) > 1"))).scalars().all()
        if dupes:
            await conn.execute(text(
                "UPDATE workflow_versions v SET number = r.rn FROM (SELECT id, row_number() OVER "
                "(PARTITION BY workflow_id ORDER BY number, created_at, id) AS rn FROM workflow_versions "
                "WHERE workflow_id = ANY(:ids)) r WHERE v.id = r.id"), {"ids": list(set(dupes))})
            await conn.execute(text(
                "UPDATE workflows w SET published_version_number = v.number FROM workflow_versions v "
                "WHERE w.published_version_id = v.id AND w.published_version_number IS DISTINCT FROM v.number"))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_versions_number ON workflow_versions (workflow_id, number)"))
        # High-volume execution history (see engine/store.py).
        await conn.execute(text("ALTER TABLE executions ADD COLUMN IF NOT EXISTS snapshot_hash VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE executions ALTER COLUMN workflow_snapshot DROP NOT NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_executions_snapshot_hash ON executions (snapshot_hash)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_executions_started_at ON executions (started_at)"))
        # Only live rows need a status index; finished ones (almost all) then never touch it.
        await conn.execute(text("DROP INDEX IF EXISTS ix_executions_status"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_executions_live ON executions (status) WHERE status IN ('running', 'waiting')"))
        # Room in each page for in-place (HOT) updates; vacuum early on a table with constant churn.
        await conn.execute(text(
            "ALTER TABLE executions SET (fillfactor = 85, autovacuum_vacuum_scale_factor = 0.02, "
            "autovacuum_analyze_scale_factor = 0.02)"))
        # Fill the stats rollup once from existing history.
        await conn.execute(text(
            "INSERT INTO execution_stats (workflow_id, day, status, count) "
            "SELECT workflow_id, (started_at AT TIME ZONE 'UTC')::date, status, count(*) FROM executions "
            "WHERE status NOT IN ('running', 'waiting') AND NOT EXISTS (SELECT 1 FROM execution_stats) "
            "GROUP BY 1, 2, 3 ON CONFLICT DO NOTHING"))
        # Per-user workspaces: rows created before ownership existed belong to the first owner.
        for table in ("workflows", "folders", "credentials"):
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id) ON DELETE SET NULL"))
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_owner_id ON {table} (owner_id)"))
            await conn.execute(text(
                f"UPDATE {table} SET owner_id = (SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1) "
                "WHERE owner_id IS NULL"))


async def get_session():
    async with SessionLocal() as session:
        yield session


__all__ = [
    "ApiKey",
    "AsyncSession",
    "AuditLog",
    "AuthSession",
    "Base",
    "ChatMessage",
    "Credential",
    "Execution",
    "Folder",
    "LoginChallenge",
    "SessionLocal",
    "StaticData",
    "User",
    "Workflow",
    "get_session",
    "init_db",
]

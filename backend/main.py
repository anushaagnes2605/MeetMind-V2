from pathlib import Path
import re
import sqlite3
import os
import logging
import json
import hashlib
import secrets
from datetime import date, datetime, timezone
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic import ValidationError


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LOGGER = logging.getLogger("meetmind.ai")

# Vercel deployments are production unless MEETMIND_ENV explicitly says
# otherwise. Relying on MEETMIND_ENV alone meant that forgetting to set it on
# Vercel silently produced non-production session cookies, so a login from the
# frontend project was never sent back on the next request.
IS_VERCEL = bool(os.getenv("VERCEL"))
MEETMIND_ENV = os.getenv("MEETMIND_ENV", "").strip().lower() or (
    "production" if IS_VERCEL else "development"
)
IS_PRODUCTION = MEETMIND_ENV == "production"

# Vercel functions have a read-only filesystem except for /tmp, and /tmp is
# ephemeral. A SQLite file therefore cannot store users in production. When a
# Turso/libSQL database is configured, all queries go there instead.
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

_configured_database_path = os.getenv("DATABASE_PATH", "").strip()
if _configured_database_path:
    _database_path = Path(_configured_database_path)
    DATABASE_PATH = _database_path if _database_path.is_absolute() else BASE_DIR / _database_path
else:
    DATABASE_PATH = Path("/tmp/meetmind.db") if IS_VERCEL else BASE_DIR / "meetmind.db"

if IS_VERCEL and not TURSO_DATABASE_URL and not _configured_database_path:
    raise RuntimeError(
        "Persistent storage is not configured. Vercel's filesystem is read-only "
        "and ephemeral, so SQLite would lose every user and session. Set "
        "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in the Vercel project, or set "
        "DATABASE_PATH to a path on a persistent volume."
    )

COOKIE_SAMESITE = os.getenv(
    "COOKIE_SAMESITE", "none" if IS_PRODUCTION else "lax"
).strip().lower()
if COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    raise RuntimeError("COOKIE_SAMESITE must be one of: lax, strict, none")

COOKIE_SECURE = os.getenv(
    "COOKIE_SECURE", "true" if IS_PRODUCTION else "false"
).strip().lower() in {"1", "true", "yes", "on"}
# Browsers reject SameSite=None cookies that are not marked Secure.
if COOKIE_SAMESITE == "none":
    COOKIE_SECURE = True

app = FastAPI(
    title="MeetMind API",
    description="AI-powered meeting intelligence and execution assistant",
    version="1.0.0",
)

default_cors_origins = (
    "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174"
)
if IS_PRODUCTION:
    default_cors_origins += ",https://meet-mind-v2.vercel.app"

cors_origins = os.getenv("CORS_ORIGINS", default_cors_origins).split(",")
cors_origin_regex = os.getenv(
    "CORS_ORIGIN_REGEX",
    "https://.*\\.vercel\\.app" if IS_PRODUCTION else "",
).strip() or None
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip().rstrip("/") for origin in cors_origins if origin.strip()],
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=100)


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def require_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        session = db.execute(
            """
            SELECT s.id, s.expires_at, u.id AS user_id, u.email, u.display_name, u.role
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL
              AND u.is_active = 1 AND s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return dict(session)


@app.get("/")
def home():
    return {
        "message": "MeetMind backend is running!",
        "status": "success",
        "groq_configured": bool(os.getenv("GROQ_API_KEY")),
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


def issue_session(user: dict, response: Response) -> dict:
    token = secrets.token_urlsafe(32)
    # Store expiry in UTC. Sessions are compared as strings against
    # datetime('now'), which is UTC, so local server time would skew expiry.
    expires = datetime.now(timezone.utc).timestamp() + SESSION_TTL_HOURS * 3600
    expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute(
            """
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            """,
            (user["id"], session_token_hash(token), expires_at),
        )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
    )
    return {
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "role": user["role"],
        },
    }


def is_integrity_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "IntegrityError"


def mask_email(email: str) -> str:
    """Return a log-safe form of an email address."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


@app.post("/auth/login")
def login(payload: LoginRequest, response: Response):
    email = payload.email.strip().lower()
    with get_db() as db:
        user = db.execute(
            """
            SELECT id, email, password_hash, display_name, role, is_active
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()
    if user is None:
        # Never log the password or the raw address. The reason code makes a
        # production login failure diagnosable from the function logs.
        LOGGER.warning("Login rejected: no account for %s", mask_email(email))
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user["is_active"]:
        LOGGER.warning("Login rejected: account is deactivated for %s", mask_email(email))
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(payload.password, user["password_hash"]):
        LOGGER.warning("Login rejected: password does not match for %s", mask_email(email))
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return issue_session(user, response)


@app.post("/auth/register", status_code=201)
def register(payload: RegisterRequest, response: Response):
    email = payload.email.strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    display_name = payload.display_name.strip() or email.split("@", 1)[0]
    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT id FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if existing is not None:
                raise HTTPException(status_code=409, detail="An account with this email already exists")
            cursor = db.execute(
                """
                INSERT INTO users (email, password_hash, display_name, role)
                VALUES (?, ?, ?, 'member')
                """,
                (email, hash_password(payload.password), display_name),
            )
            user_id = cursor.lastrowid
    except HTTPException:
        raise
    except Exception as exc:
        if is_integrity_error(exc):
            raise HTTPException(status_code=409, detail="An account with this email already exists") from exc
        raise

    with get_db() as db:
        user = db.execute(
            """
            SELECT id, email, display_name, role
            FROM users
            WHERE id = ? AND is_active = 1
            """,
            (user_id,),
        ).fetchone()
    if user is None:
        raise HTTPException(status_code=500, detail="Account could not be created")
    return issue_session(user, response)


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with get_db() as db:
            db.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = datetime('now')
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (session_token_hash(token),),
            )
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )
    return {"authenticated": False}


@app.get("/auth/me")
def current_user(user: dict = Depends(require_user)):
    return {
        "authenticated": True,
        "user": {
            "id": user["user_id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "role": user["role"],
        },
    }


class TranscriptSubmission(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    transcript: str = Field(min_length=1)


class SpeakerStatement(BaseModel):
    speaker: str
    text: str


class Decision(BaseModel):
    text: str
    source_passage: str
    owner: Optional[str] = None


class ActionItem(BaseModel):
    text: str
    owner: Optional[str] = None
    deadline: Optional[str] = None
    priority: str = "Medium"
    source_passage: str


class Conflict(BaseModel):
    new_decision: str
    previous_decision: str
    previous_meeting: str
    reason: str


class ImportantDiscussionPoint(BaseModel):
    text: str
    speaker: Optional[str] = None
    source_passage: str


class MeetingAnalysis(BaseModel):
    summary: str
    speaker_statements: list[SpeakerStatement] = []
    decisions: list[Decision] = []
    action_items: list[ActionItem] = []
    conflicts: list[Conflict] = []
    important_points: list[ImportantDiscussionPoint] = []
    analysis_mode: str
    fallback_reason: Optional[str] = None


class GeminiMeetingOutput(BaseModel):
    summary: str
    speaker_statements: list[SpeakerStatement] = []
    decisions: list[Decision] = []
    action_items: list[ActionItem] = []
    important_points: list[ImportantDiscussionPoint] = []


class AgentRunCreate(BaseModel):
    meeting_id: Optional[int] = None
    provider: Optional[str] = None


class AgentStepRecord(BaseModel):
    id: int
    step_order: int
    agent_role: str
    operation: str
    description: str
    status: str
    created_at: str


class ToolExecutionRecord(BaseModel):
    id: int
    run_id: int
    tool_name: str
    validated_arguments: str
    status: str
    result_summary: Optional[str] = None
    created_at: str


class AgentRunRecord(BaseModel):
    id: int
    meeting_id: Optional[int] = None
    status: str
    provider: str
    created_at: str
    updated_at: str
    steps: list[AgentStepRecord] = []
    tool_executions: list[ToolExecutionRecord] = []


class MemorySearchRequest(BaseModel):
    keywords: list[str] = []
    meeting: Optional[str] = None
    meeting_id: Optional[int] = None
    owner: Optional[str] = None
    exclude_meeting_id: Optional[int] = None
    limit: int = Field(default=20, ge=1, le=100)
    run_id: Optional[int] = None


class MemoryDecisionEvidence(BaseModel):
    meeting_id: int
    meeting_title: str
    decision_id: int
    decision: str
    source_passage: str
    owner: Optional[str] = None
    created_at: str


class MemoryActionEvidence(BaseModel):
    meeting_id: int
    meeting_title: str
    action_id: int
    action: str
    owner: Optional[str] = None
    deadline: Optional[str] = None
    status: str
    source_passage: str


class MemorySearchResult(BaseModel):
    keywords: list[str] = []
    decisions: list[MemoryDecisionEvidence] = []
    open_actions: list[MemoryActionEvidence] = []


class DecisionComparisonRequest(BaseModel):
    new_decision: str
    meeting_id: Optional[int] = None
    source_passage: Optional[str] = None
    owner: Optional[str] = None
    run_id: Optional[int] = None


class DecisionEvidence(BaseModel):
    meeting_id: int
    meeting_title: str
    decision_id: int
    decision: str
    source_passage: str
    created_at: str


class DecisionComparisonResult(BaseModel):
    classification: Literal[
        "contradiction",
        "approved_change",
        "clarification",
        "repetition",
        "unrelated",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    provider: Literal["groq", "gemini", "deterministic"]
    evidence: list[DecisionEvidence] = []


class CommitmentRecord(BaseModel):
    action_id: int
    meeting_id: int
    meeting_title: str
    task: str
    owner: Optional[str] = None
    deadline: Optional[str] = None
    priority: str
    status: Literal["open", "completed", "overdue", "approaching", "unassigned"]
    source_passage: str
    duplicate_of: Optional[int] = None


class CommitmentAnalysisResult(BaseModel):
    commitments: list[CommitmentRecord] = []
    open_count: int = 0
    overdue_count: int = 0
    approaching_count: int = 0
    unassigned_count: int = 0
    completed_count: int = 0
    duplicate_count: int = 0


class CommitmentRiskRecord(BaseModel):
    action_id: int
    meeting_id: int
    meeting_title: str
    task: str
    owner: Optional[str] = None
    deadline: Optional[str] = None
    status: str
    risk_score: int = Field(ge=0, le=100)
    risk_level: Literal["low", "medium", "high", "critical"]
    factors: list[str] = []
    source_passage: str


class RiskRankingResult(BaseModel):
    ranked_commitments: list[CommitmentRiskRecord] = []
    methodology: str = "Deterministic rule-based scoring"


class InterventionRecommendation(BaseModel):
    action_id: Optional[int] = None
    meeting_id: Optional[int] = None
    task: str
    intervention: Literal[
        "request_progress_update",
        "confirm_owner",
        "clarify_deadline",
        "follow_up_overdue",
        "review_high_risk",
    ]
    urgency: Literal["low", "medium", "high", "critical"]
    reason: str


class DecisionComparisonProviderOutput(BaseModel):
    classification: Literal[
        "contradiction",
        "approved_change",
        "clarification",
        "repetition",
        "unrelated",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str


class ApprovalCreateRequest(BaseModel):
    run_id: int
    tool_name: Literal["create_local_task", "mark_action_complete", "update_task_status"]
    arguments: dict
    reason: str
    evidence: dict = {}


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]


class CommitmentToolRequest(BaseModel):
    meeting_id: Optional[int] = None
    run_id: Optional[int] = None


class ManagedConnection:
    """Context manager that commits/rolls back and always closes the database
    connection. Both sqlite3 and turso_serverless expose the DB-API surface the
    rest of this module relies on (execute/fetchone/lastrowid/executescript)."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False


def get_db():
    if TURSO_DATABASE_URL:
        if not TURSO_AUTH_TOKEN:
            raise RuntimeError("TURSO_AUTH_TOKEN is required when TURSO_DATABASE_URL is set")
        import turso_serverless

        connection = turso_serverless.connect(
            TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
        # turso_serverless returns plain tuples by default, while this module
        # reads every column by name (row["password_hash"]). Without a row
        # factory, login raises "tuple indices must be integers" and every
        # authenticated endpoint fails. Install the sqlite3.Row-compatible
        # factory so the Turso and SQLite paths behave identically.
        connection.row_factory = turso_serverless.Row
        return ManagedConnection(connection)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return ManagedConnection(connection)


class CreateLocalTaskArgs(BaseModel):
    meeting_id: int
    text: str = Field(min_length=1, max_length=500)
    owner: Optional[str] = Field(default=None, max_length=100)
    deadline: Optional[str] = Field(default=None, max_length=100)
    priority: Literal["Low", "Medium", "High"] = "Medium"
    source_passage: str = Field(default="Approved local follow-up task.", min_length=1, max_length=1000)


class MarkActionCompleteArgs(BaseModel):
    action_id: int = Field(gt=0)


class UpdateTaskStatusArgs(BaseModel):
    action_id: int = Field(gt=0)
    status: Literal["Open", "In Progress", "Done"]


LOCAL_TOOL_SCHEMAS = {
    "create_local_task": CreateLocalTaskArgs,
    "mark_action_complete": MarkActionCompleteArgs,
    "update_task_status": UpdateTaskStatusArgs,
}


def validate_local_tool_arguments(tool_name: str, arguments: dict) -> dict:
    schema = LOCAL_TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        raise ValueError("Tool is not registered")
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    return schema.model_validate(arguments).model_dump()


def execute_registered_local_tool(run_id: int, tool_name: str, arguments: dict) -> dict:
    try:
        validated = validate_local_tool_arguments(tool_name, arguments)
    except ValidationError as exc:
        record_tool_execution(
            run_id,
            tool_name,
            arguments,
            "failed",
            "Invalid tool arguments.",
        )
        return {
            "success": False,
            "tool": tool_name,
            "message": "Invalid tool arguments.",
        }
    except ValueError as exc:
        record_tool_execution(
            run_id,
            tool_name,
            arguments,
            "failed",
            str(exc),
        )
        return {
            "success": False,
            "tool": tool_name,
            "message": str(exc),
        }

    try:
        with get_db() as db:
            if tool_name == "create_local_task":
                meeting = db.execute(
                    "SELECT id FROM meetings WHERE id = ?",
                    (validated["meeting_id"],),
                ).fetchone()
                if meeting is None:
                    raise ValueError("Meeting reference does not exist")

                existing = db.execute(
                    """
                    SELECT id FROM action_items
                    WHERE meeting_id = ? AND text = ? AND status != 'Done'
                    LIMIT 1
                    """,
                    (validated["meeting_id"], validated["text"]),
                ).fetchone()
                if existing is not None:
                    result = {
                        "success": True,
                        "tool": tool_name,
                        "action_id": existing["id"],
                        "created": False,
                        "message": "Local follow-up task already exists.",
                    }
                else:
                    cursor = db.execute(
                        """
                        INSERT INTO action_items
                            (meeting_id, text, source_passage, owner, deadline, priority, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'Open')
                        """,
                        (
                            validated["meeting_id"],
                            validated["text"],
                            validated["source_passage"],
                            validated["owner"],
                            validated["deadline"],
                            validated["priority"],
                        ),
                    )
                    action_id = cursor.lastrowid
                    result = {
                        "success": True,
                        "tool": tool_name,
                        "action_id": action_id,
                        "created": True,
                        "message": "Local follow-up task created.",
                    }

            elif tool_name == "mark_action_complete":
                action = db.execute(
                    "SELECT id, status FROM action_items WHERE id = ?",
                    (validated["action_id"],),
                ).fetchone()
                if action is None:
                    raise ValueError("Action item reference does not exist")
                if action["status"] != "Done":
                    db.execute(
                        "UPDATE action_items SET status = 'Done' WHERE id = ?",
                        (validated["action_id"],),
                    )
                    changed = True
                else:
                    changed = False
                result = {
                    "success": True,
                    "tool": tool_name,
                    "action_id": validated["action_id"],
                    "changed": changed,
                    "message": (
                        "Action item marked complete."
                        if changed
                        else "Action item was already complete."
                    ),
                }

            elif tool_name == "update_task_status":
                action = db.execute(
                    "SELECT id, status FROM action_items WHERE id = ?",
                    (validated["action_id"],),
                ).fetchone()
                if action is None:
                    raise ValueError("Action item reference does not exist")
                if action["status"] != validated["status"]:
                    db.execute(
                        "UPDATE action_items SET status = ? WHERE id = ?",
                        (validated["status"], validated["action_id"]),
                    )
                    changed = True
                else:
                    changed = False
                result = {
                    "success": True,
                    "tool": tool_name,
                    "action_id": validated["action_id"],
                    "status": validated["status"],
                    "changed": changed,
                    "message": (
                        "Action item status updated."
                        if changed
                        else "Action item already has that status."
                    ),
                }
            else:
                raise ValueError("Tool is not registered")
    except ValueError as exc:
        record_tool_execution(
            run_id,
            tool_name,
            validated,
            "failed",
            str(exc),
        )
        return {
            "success": False,
            "tool": tool_name,
            "message": str(exc),
        }
    except Exception as exc:
        record_tool_execution(
            run_id,
            tool_name,
            validated,
            "failed",
            f"Local tool execution failed: {type(exc).__name__}",
        )
        return {
            "success": False,
            "tool": tool_name,
            "message": "Local tool execution failed.",
        }

    record_tool_execution(
        run_id,
        tool_name,
        validated,
        "success",
        json.dumps(result, ensure_ascii=False),
    )
    return result


def get_approval_request(approval_id: int) -> dict:
    with get_db() as db:
        approval = db.execute(
            """
            SELECT ar.id, ar.run_id, ar.tool_name, ar.action_arguments,
                   ar.reason, ar.evidence, ar.status, ar.created_at,
                   ar.updated_at, ar.decision_at, ar.execution_status,
                   ar.execution_result, ar.executed_at,
                   run.meeting_id
            FROM approval_requests ar
            JOIN agent_runs run ON run.id = ar.run_id
            WHERE ar.id = ?
            """,
            (approval_id,),
        ).fetchone()
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    result = dict(approval)
    result["action_arguments"] = json.loads(result["action_arguments"])
    result["evidence"] = json.loads(result["evidence"])
    result["execution_result"] = (
        json.loads(result["execution_result"])
        if result["execution_result"]
        else None
    )
    return result


def create_approval_request(
    run_id: int,
    tool_name: str,
    arguments: dict,
    reason: str,
    evidence: dict,
) -> dict:
    try:
        validated = validate_local_tool_arguments(tool_name, arguments)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with get_db() as db:
        run = db.execute(
            "SELECT id, meeting_id FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Agent run not found")

        if tool_name == "create_local_task":
            meeting_id = validated["meeting_id"]
            meeting = db.execute(
                "SELECT id FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
            if meeting is None:
                raise HTTPException(status_code=400, detail="Meeting reference does not exist")

            duplicate = db.execute(
                """
                SELECT id FROM approval_requests
                WHERE tool_name = 'create_local_task'
                  AND action_arguments = ?
                  AND status IN ('pending', 'approved')
                LIMIT 1
                """,
                (json.dumps(validated, ensure_ascii=False),),
            ).fetchone()
            if duplicate is not None:
                return get_approval_request(duplicate["id"])

            existing_action = db.execute(
                """
                SELECT id FROM action_items
                WHERE meeting_id = ? AND text = ? AND status != 'Done'
                LIMIT 1
                """,
                (meeting_id, validated["text"]),
            ).fetchone()
            if existing_action is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A matching local task already exists",
                )

        cursor = db.execute(
            """
            INSERT INTO approval_requests
                (run_id, tool_name, action_arguments, reason, evidence, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (
                run_id,
                tool_name,
                json.dumps(validated, ensure_ascii=False),
                reason,
                json.dumps(evidence, ensure_ascii=False),
            ),
        )
        approval_id = cursor.lastrowid
    record_tool_execution(
        run_id,
        "create_approval_request",
        {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "arguments": validated,
        },
        "success",
        f"Pending approval {approval_id} created for {tool_name}.",
    )
    return get_approval_request(approval_id)


COMMITMENT_STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "will", "should",
    "needs", "need", "please", "this", "that", "with", "by", "before",
}


def normalize_commitment_text(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(
        word for word in words
        if word not in COMMITMENT_STOP_WORDS
    )


def parse_commitment_deadline(deadline: Optional[str]) -> Optional[date]:
    if not deadline:
        return None
    value = deadline.strip()
    today = date.today()
    lowered = value.lower()
    if lowered == "today":
        return today
    if lowered == "tomorrow":
        return date.fromordinal(today.toordinal() + 1)
    if lowered == "this week":
        return today
    if lowered == "next week":
        return date.fromordinal(today.toordinal() + 7)

    formats = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year)
            return parsed
        except ValueError:
            continue
    return None


def analyze_commitments(meeting_id: Optional[int] = None) -> CommitmentAnalysisResult:
    with get_db() as db:
        if meeting_id is None:
            rows = db.execute(
                """
                SELECT a.id AS action_id, a.meeting_id, a.text AS task,
                       a.owner, a.deadline, a.priority, a.status,
                       a.source_passage, m.title AS meeting_title
                FROM action_items a
                JOIN meetings m ON m.id = a.meeting_id
                ORDER BY a.id ASC
                """
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT a.id AS action_id, a.meeting_id, a.text AS task,
                       a.owner, a.deadline, a.priority, a.status,
                       a.source_passage, m.title AS meeting_title
                FROM action_items a
                JOIN meetings m ON m.id = a.meeting_id
                WHERE a.meeting_id = ?
                ORDER BY a.id ASC
                """,
                (meeting_id,),
            ).fetchall()

    today = date.today()
    commitments = []
    normalized_seen = {}
    duplicate_count = 0
    counts = {"open": 0, "overdue": 0, "approaching": 0, "unassigned": 0, "completed": 0}

    for row in rows:
        duplicate_of = None
        normalized = normalize_commitment_text(row["task"])
        if normalized:
            for previous_id, previous_normalized in normalized_seen.items():
                shared_words = set(normalized.split()) & set(previous_normalized.split())
                union_words = set(normalized.split()) | set(previous_normalized.split())
                similarity = len(shared_words) / len(union_words) if union_words else 0.0
                if similarity >= 0.72:
                    duplicate_of = previous_id
                    duplicate_count += 1
                    break
            normalized_seen[row["action_id"]] = normalized

        status = "open"
        if row["status"] == "Done":
            status = "completed"
        elif not row["owner"]:
            status = "unassigned"
        else:
            deadline = parse_commitment_deadline(row["deadline"])
            if deadline and deadline < today:
                status = "overdue"
            elif deadline and 0 <= (deadline - today).days <= 7:
                status = "approaching"

        counts[status] += 1
        commitments.append(
            CommitmentRecord(
                action_id=row["action_id"],
                meeting_id=row["meeting_id"],
                meeting_title=row["meeting_title"],
                task=row["task"],
                owner=row["owner"],
                deadline=row["deadline"],
                priority=row["priority"],
                status=status,
                source_passage=row["source_passage"],
                duplicate_of=duplicate_of,
            )
        )

    return CommitmentAnalysisResult(
        commitments=commitments,
        open_count=counts["open"],
        overdue_count=counts["overdue"],
        approaching_count=counts["approaching"],
        unassigned_count=counts["unassigned"],
        completed_count=counts["completed"],
        duplicate_count=duplicate_count,
    )


def calculate_commitment_risk(meeting_id: Optional[int] = None) -> RiskRankingResult:
    analysis = analyze_commitments(meeting_id)
    ranked = []

    with get_db() as db:
        all_rows = db.execute(
            """
            SELECT a.id AS action_id, a.meeting_id, a.text AS task,
                   a.owner, a.deadline, a.status, a.source_passage,
                   m.title AS meeting_title
            FROM action_items a
            JOIN meetings m ON m.id = a.meeting_id
            ORDER BY a.id ASC
            """
        ).fetchall()

    repeated_actions = {}
    for row in all_rows:
        normalized = normalize_commitment_text(row["task"])
        if normalized:
            repeated_actions.setdefault(normalized, []).append(row)

    for commitment in analysis.commitments:
        if commitment.status == "completed":
            continue

        score = 0
        factors = []
        if commitment.status == "overdue":
            score += 45
            factors.append("Deadline is overdue.")
        elif commitment.status == "approaching":
            score += 30
            factors.append("Deadline is within 7 days.")
        if not commitment.owner:
            score += 25
            factors.append("Owner is unassigned.")
        if not commitment.deadline:
            score += 20
            factors.append("Deadline is missing.")
        if commitment.priority.lower() == "high":
            score += 15
            factors.append("Commitment is marked high priority.")
        elif commitment.priority.lower() == "medium":
            score += 5
        if commitment.duplicate_of is not None:
            score += 10
            factors.append("Commitment appears duplicated or overlapping.")

        normalized = normalize_commitment_text(commitment.task)
        historical_matches = repeated_actions.get(normalized, [])
        if len(historical_matches) > 1:
            score += 10
            factors.append("Repeated commitment appears across multiple records.")

        score = min(score, 100)
        if score >= 80:
            level = "critical"
        elif score >= 55:
            level = "high"
        elif score >= 30:
            level = "medium"
        else:
            level = "low"

        ranked.append(
            CommitmentRiskRecord(
                action_id=commitment.action_id,
                meeting_id=commitment.meeting_id,
                meeting_title=commitment.meeting_title,
                task=commitment.task,
                owner=commitment.owner,
                deadline=commitment.deadline,
                status=commitment.status,
                risk_score=score,
                risk_level=level,
                factors=factors or ["No specific risk factor detected."],
                source_passage=commitment.source_passage,
            )
        )

    ranked.sort(key=lambda item: (-item.risk_score, item.meeting_id, item.action_id))
    return RiskRankingResult(ranked_commitments=ranked)


def recommend_interventions(meeting_id: Optional[int] = None) -> list[InterventionRecommendation]:
    recommendations = []
    for risk in calculate_commitment_risk(meeting_id).ranked_commitments:
        if risk.status == "overdue":
            intervention = "follow_up_overdue"
            urgency = "critical"
            reason = "The commitment is overdue and needs immediate follow-up."
        elif not risk.owner:
            intervention = "confirm_owner"
            urgency = "high" if risk.risk_level in ("high", "critical") else "medium"
            reason = "No owner is assigned, so accountability is unclear."
        elif not risk.deadline:
            intervention = "clarify_deadline"
            urgency = "high" if risk.risk_level in ("high", "critical") else "medium"
            reason = "The deadline is missing or ambiguous."
        elif risk.status == "approaching":
            intervention = "request_progress_update"
            urgency = "high"
            reason = "The deadline is approaching and progress should be confirmed."
        elif risk.risk_level in ("high", "critical"):
            intervention = "review_high_risk"
            urgency = risk.risk_level
            reason = "The commitment combines several risk factors."
        else:
            continue

        recommendations.append(
            InterventionRecommendation(
                action_id=risk.action_id,
                meeting_id=risk.meeting_id,
                task=risk.task,
                intervention=intervention,
                urgency=urgency,
                reason=reason,
            )
        )

    urgency_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    recommendations.sort(
        key=lambda item: (urgency_order[item.urgency], item.action_id or 0)
    )
    return recommendations
    value = deadline.strip()
    today = date.today()
    lowered = value.lower()
    if lowered == "today":
        return today
    if lowered == "tomorrow":
        return today.replace() if False else date.fromordinal(today.toordinal() + 1)
    if lowered in ("this week",):
        return today
    if lowered in ("next week",):
        return date.fromordinal(today.toordinal() + 7)

    formats = (
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d",
        "%b %d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year)
            return parsed
        except ValueError:
            continue
    return None


def search_memory(
    keywords: list[str] | None = None,
    meeting: Optional[str] = None,
    meeting_id: Optional[int] = None,
    owner: Optional[str] = None,
    exclude_meeting_id: Optional[int] = None,
    limit: int = 20,
) -> MemorySearchResult:
    normalized_keywords = [
        keyword.strip().lower()
        for keyword in (keywords or [])
        if keyword and keyword.strip()
    ]
    keyword_text = " ".join(normalized_keywords)

    decision_rows = []
    action_rows = []

    with get_db() as db:
        decision_query = """
            SELECT d.id AS decision_id, d.meeting_id, d.text AS decision,
                   d.source_passage, d.owner, d.created_at,
                   m.title AS meeting_title
            FROM decisions d
            JOIN meetings m ON m.id = d.meeting_id
            WHERE 1 = 1
        """
        decision_params = []
        if meeting_id is not None:
            decision_query += " AND d.meeting_id = ?"
            decision_params.append(meeting_id)
        if meeting:
            decision_query += " AND lower(m.title) LIKE ?"
            decision_params.append(f"%{meeting.lower()}%")
        if owner:
            decision_query += " AND lower(coalesce(d.owner, '')) LIKE ?"
            decision_params.append(f"%{owner.lower()}%")
        if exclude_meeting_id is not None:
            decision_query += " AND d.meeting_id != ?"
            decision_params.append(exclude_meeting_id)
        if normalized_keywords:
            decision_clauses = []
            for keyword in normalized_keywords:
                decision_clauses.append(
                    "(lower(d.text) LIKE ? OR lower(d.source_passage) LIKE ? "
                    "OR lower(m.title) LIKE ?)"
                )
                decision_params.extend(
                    [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]
                )
            decision_query += " AND (" + " OR ".join(decision_clauses) + ")"
        decision_query += " ORDER BY d.id DESC LIMIT ?"
        decision_params.append(limit)
        decision_rows = db.execute(decision_query, decision_params).fetchall()

        action_query = """
            SELECT a.id AS action_id, a.meeting_id, a.text AS action,
                   a.owner, a.deadline, a.status, a.source_passage,
                   m.title AS meeting_title
            FROM action_items a
            JOIN meetings m ON m.id = a.meeting_id
            WHERE a.status != 'Done'
        """
        action_params = []
        if meeting_id is not None:
            action_query += " AND a.meeting_id = ?"
            action_params.append(meeting_id)
        if meeting:
            action_query += " AND lower(m.title) LIKE ?"
            action_params.append(f"%{meeting.lower()}%")
        if owner:
            action_query += " AND lower(coalesce(a.owner, '')) LIKE ?"
            action_params.append(f"%{owner.lower()}%")
        if exclude_meeting_id is not None:
            action_query += " AND a.meeting_id != ?"
            action_params.append(exclude_meeting_id)
        if normalized_keywords:
            action_clauses = []
            for keyword in normalized_keywords:
                action_clauses.append(
                    "(lower(a.text) LIKE ? OR lower(a.source_passage) LIKE ? "
                    "OR lower(m.title) LIKE ?)"
                )
                action_params.extend(
                    [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]
                )
            action_query += " AND (" + " OR ".join(action_clauses) + ")"
        action_query += " ORDER BY a.id DESC LIMIT ?"
        action_params.append(limit)
        action_rows = db.execute(action_query, action_params).fetchall()

    return MemorySearchResult(
        keywords=normalized_keywords,
        decisions=[
            MemoryDecisionEvidence(
                meeting_id=row["meeting_id"],
                meeting_title=row["meeting_title"],
                decision_id=row["decision_id"],
                decision=row["decision"],
                source_passage=row["source_passage"],
                owner=row["owner"],
                created_at=row["created_at"],
            )
            for row in decision_rows
        ],
        open_actions=[
            MemoryActionEvidence(
                meeting_id=row["meeting_id"],
                meeting_title=row["meeting_title"],
                action_id=row["action_id"],
                action=row["action"],
                owner=row["owner"],
                deadline=row["deadline"],
                status=row["status"],
                source_passage=row["source_passage"],
            )
            for row in action_rows
        ],
    )


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                transcript TEXT NOT NULL,
                summary TEXT,
                analysis_mode TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                source_passage TEXT NOT NULL,
                owner TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                source_passage TEXT NOT NULL,
                owner TEXT,
                deadline TEXT,
                priority TEXT NOT NULL DEFAULT 'Medium',
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                new_decision TEXT NOT NULL,
                previous_decision TEXT NOT NULL,
                previous_meeting TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS speaker_statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS important_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                speaker TEXT,
                source_passage TEXT NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER,
                status TEXT NOT NULL DEFAULT 'planned',
                provider TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id)
            );
            CREATE TABLE IF NOT EXISTS agent_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                step_order INTEGER NOT NULL,
                agent_role TEXT NOT NULL,
                operation TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (run_id) REFERENCES agent_runs(id)
            );
            CREATE TABLE IF NOT EXISTS tool_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                validated_arguments TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'not_executed',
                result_summary TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (run_id) REFERENCES agent_runs(id)
            );
            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                action_arguments TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                decision_at TEXT,
                execution_status TEXT NOT NULL DEFAULT 'not_executed',
                execution_result TEXT,
                executed_at TEXT,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id)
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )


init_db()


PASSWORD_ITERATIONS = 200_000
SESSION_COOKIE_NAME = "meetmind_session"
SESSION_TTL_HOURS = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        salt.hex(),
        derived.hex(),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, stored_hex = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return secrets.compare_digest(candidate.hex(), stored_hex)
    except (ValueError, TypeError):
        return False


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def seed_demo_user() -> None:
    # MEETMIND_SKIP_SEED keeps administrative scripts from creating accounts
    # (including the demo account) in a database they are only inspecting.
    if IS_PRODUCTION or os.getenv("MEETMIND_SKIP_SEED") == "1":
        return
    email = os.getenv("DEMO_ADMIN_EMAIL", "admin@meetmind.demo").strip().lower()
    password = os.getenv("DEMO_ADMIN_PASSWORD", "MeetMind@123")
    display_name = "MeetMind Admin"
    with get_db() as db:
        existing = db.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if existing is None:
            db.execute(
                """
                INSERT INTO users (email, password_hash, display_name, role)
                VALUES (?, ?, ?, 'admin')
                """,
                (email, hash_password(password), display_name),
            )


def bootstrap_admin_user() -> None:
    """Create the first production account from environment variables.

    Production intentionally does not seed the demo account. Set
    BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD once to recreate an
    existing account (for example the local demo user) in the hosted database.
    Existing users are never overwritten.
    """
    if os.getenv("MEETMIND_SKIP_SEED") == "1":
        return
    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    if not EMAIL_PATTERN.match(email):
        LOGGER.warning("BOOTSTRAP_ADMIN_EMAIL is not a valid email address; skipping bootstrap account")
        return
    display_name = os.getenv("BOOTSTRAP_ADMIN_NAME", "MeetMind Admin").strip() or "MeetMind Admin"
    with get_db() as db:
        existing = db.execute(
            "SELECT id, password_hash, is_active FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if existing is None:
            try:
                db.execute(
                    """
                    INSERT INTO users (email, password_hash, display_name, role)
                    VALUES (?, ?, ?, 'admin')
                    """,
                    (email, hash_password(password), display_name),
                )
                LOGGER.info("Created bootstrap admin account for %s", mask_email(email))
            except Exception as exc:
                if not is_integrity_error(exc):
                    raise
        elif not verify_password(password, existing["password_hash"]):
            LOGGER.warning(
                "Bootstrap account %s already exists with a different password; "
                "existing accounts are never overwritten.",
                mask_email(email),
            )


seed_demo_user()
bootstrap_admin_user()


def split_sentences(transcript: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", transcript) if part.strip()]


def important_points_from_sentences(sentences: list[str]) -> list[ImportantDiscussionPoint]:
    points = []
    for sentence in sentences[:5]:
        speaker_match = re.match(r"^([^:\n]{1,40}):\s*(.+)$", sentence)
        points.append(
            ImportantDiscussionPoint(
                text=speaker_match.group(2).strip() if speaker_match else sentence,
                speaker=speaker_match.group(1).strip() if speaker_match else None,
                source_passage=sentence,
            )
        )
    return points


def parse_speaker_statements(transcript: str) -> list[SpeakerStatement]:
    statements = []
    for line in transcript.splitlines():
        match = re.match(r"^([^:\n]{1,40}):\s*(.+)$", line.strip())
        if match:
            statements.append(SpeakerStatement(speaker=match.group(1).strip(), text=match.group(2).strip()))
    return statements


def extract_owner(text: str) -> Optional[str]:
    speaker_match = re.match(r"^([A-Z][A-Za-z]+):\s*", text)
    if speaker_match:
        return speaker_match.group(1)
    match = re.search(r"\b(?:owner|assigned to|for)\s*:?\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", text)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z][A-Za-z]+)\s+(?:will|to|owns)\b", text)
    return match.group(1) if match else None


def extract_deadline(text: str) -> Optional[str]:
    match = re.search(
        r"\b(?:by|due|before|on)\s+((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[a-z]*\s+\d{1,2}(?:,\s+\d{4})?|(?:today|tomorrow|this week|next week|Monday|Tuesday|Wednesday|Thursday|Friday))\b",
        text,
        re.I,
    )
    return match.group(1) if match else None


def heuristic_analysis(title: str, transcript: str, fallback_reason: Optional[str] = None) -> MeetingAnalysis:
    sentences = split_sentences(transcript)
    decisions = []
    action_items = []

    for sentence in sentences:
        lower = sentence.lower()
        if any(marker in lower for marker in ("decided", "we will", "we have agreed", "agreed to", "decision")):
            decisions.append(
                Decision(
                    text=sentence,
                    source_passage=sentence,
                    owner=extract_owner(sentence),
                )
            )
        if any(marker in lower for marker in ("action item", "todo", "will ", "should ", "needs to ", "assigned")):
            action_items.append(
                ActionItem(
                    text=sentence,
                    owner=extract_owner(sentence),
                    deadline=extract_deadline(sentence),
                    priority="High" if extract_deadline(sentence) else "Medium",
                    source_passage=sentence,
                )
            )

    summary_source = " ".join(sentences[:3])
    summary = summary_source[:500] if summary_source else "No transcript summary could be derived."
    conflicts = detect_conflicts(decisions)
    return MeetingAnalysis(
        summary=summary,
        speaker_statements=parse_speaker_statements(transcript),
        decisions=decisions,
        action_items=action_items,
        conflicts=conflicts,
        important_points=important_points_from_sentences(sentences),
        analysis_mode="deterministic_rules",
        fallback_reason=fallback_reason or (
            "Gemini API key is not configured."
            if not os.getenv("GEMINI_API_KEY")
            else None
        ),
    )


def detect_conflicts(decisions: list[Decision]) -> list[Conflict]:
    conflicts = []
    with get_db() as db:
        previous = db.execute(
            """
            SELECT d.text, d.source_passage, m.title
            FROM decisions d
            JOIN meetings m ON m.id = d.meeting_id
            ORDER BY d.id DESC
            LIMIT 100
            """
        ).fetchall()

    contradiction_pairs = (
        ("launch", "delay"),
        ("increase", "decrease"),
        ("approve", "reject"),
        ("remote", "office"),
        ("yes", "no"),
        ("build", "buy"),
    )
    for decision in decisions:
        new_lower = decision.text.lower()
        for old in previous:
            old_lower = old["text"].lower()
            has_conflict = any(
                (left in new_lower and right in old_lower)
                or (right in new_lower and left in old_lower)
                for left, right in contradiction_pairs
            )
            if has_conflict:
                conflicts.append(
                    Conflict(
                        new_decision=decision.text,
                        previous_decision=old["text"],
                        previous_meeting=old["title"],
                        reason="Opposing keywords indicate possible decision drift.",
                    )
                )
                break
    return conflicts


def call_gemini(title: str, transcript: str) -> MeetingAnalysis:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return heuristic_analysis(title, transcript)

    try:
        from google import genai
        from google.genai import types

        timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))
        if timeout_ms < 1000:
            timeout_ms = 30000
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        prompt = (
            "Analyze this meeting transcript. Return valid structured JSON only. "
            "Extract summary, speaker statements, decisions with source passages, "
            "action items with owner/deadline/priority/source passage, and important "
            "discussion points with source passages and speaker attribution. "
            "Every decision and action item must quote a contiguous, exact passage "
            "from the transcript. Do not infer facts that are not stated. "
            f"Meeting title: {title}\n\nTranscript:\n{transcript}"
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": GeminiMeetingOutput,
            },
        )
        if not getattr(response, "text", None):
            LOGGER.warning("Gemini returned an empty structured response")
            return heuristic_analysis(
                title,
                transcript,
                "Gemini returned an empty response; rule-based fallback was used.",
            )

        parsed = GeminiMeetingOutput.model_validate_json(response.text)

        normalized_transcript = transcript.strip().lower()
        for decision in parsed.decisions:
            if not decision.source_passage.strip():
                raise ValueError("Gemini decision source passage is missing")
            if decision.source_passage.strip().lower() not in normalized_transcript:
                raise ValueError("Gemini decision source passage is not in the transcript")
        for action in parsed.action_items:
            if not action.source_passage.strip():
                raise ValueError("Gemini action source passage is missing")
            if action.source_passage.strip().lower() not in normalized_transcript:
                raise ValueError("Gemini action source passage is not in the transcript")

        result = MeetingAnalysis(
            summary=parsed.summary,
            speaker_statements=parsed.speaker_statements,
            decisions=parsed.decisions,
            action_items=parsed.action_items,
            conflicts=detect_conflicts(parsed.decisions),
            important_points=parsed.important_points,
            analysis_mode="gemini",
        )
        return result
    except TimeoutError:
        LOGGER.warning("Gemini request timed out")
        return heuristic_analysis(
            title,
            transcript,
            "Gemini timed out; rule-based fallback was used.",
        )
    except ValidationError:
        LOGGER.warning("Gemini returned malformed structured output")
        return heuristic_analysis(
            title,
            transcript,
            "Gemini output failed Pydantic validation; rule-based fallback was used.",
        )
    except ValueError as exc:
        LOGGER.warning("Gemini response validation failed: %s", type(exc).__name__)
        return heuristic_analysis(
            title,
            transcript,
            "Gemini output could not be validated against the transcript; rule-based fallback was used.",
        )
    except Exception as exc:
        detail = str(exc)
        if api_key:
            detail = detail.replace(api_key, "[REDACTED]")
        detail = re.sub(r"(?i)(api[_-]?key|authorization|x-goog-api-key)\s*[:=]\s*[^\s,&]+", r"\1=[REDACTED]", detail)
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status is None:
            for attr in ("status", "http_status"):
                if getattr(exc, attr, None) is not None:
                    status = getattr(exc, attr)
                    break
        LOGGER.warning(
            "Gemini API request failed | type=%s status=%s detail=%s",
            type(exc).__name__,
            status or "unknown",
            detail[:800],
        )
        return heuristic_analysis(
            title,
            transcript,
            "Gemini API request failed; rule-based fallback was used.",
        )


def validate_source_passages(
    parsed: GeminiMeetingOutput,
    transcript: str,
    provider_name: str,
) -> None:
    normalized_transcript = transcript.strip().lower()
    for decision in parsed.decisions:
        passage = decision.source_passage.strip()
        if not passage:
            raise ValueError(f"{provider_name} decision source passage is missing")
        if passage.lower() not in normalized_transcript:
            raise ValueError(f"{provider_name} decision source passage is not in the transcript")
    for action in parsed.action_items:
        passage = action.source_passage.strip()
        if not passage:
            raise ValueError(f"{provider_name} action source passage is missing")
        if passage.lower() not in normalized_transcript:
            raise ValueError(f"{provider_name} action source passage is not in the transcript")


def extract_json_object(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start:end + 1]
            json.loads(candidate)
            return candidate
    raise ValueError("Response does not contain valid JSON")


def safe_groq_error_detail(response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        message = error.get("message") if isinstance(error, dict) else None
        if message:
            return str(message).replace(
                os.getenv("GROQ_API_KEY", ""),
                "[REDACTED]",
            )[:500]
    except Exception:
        pass
    return f"HTTP {response.status_code}"


def call_groq(title: str, transcript: str) -> Optional[MeetingAnalysis]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        LOGGER.warning("Groq API key is not configured")
        return None

    try:
        import requests

        timeout_seconds = float(os.getenv("GROQ_TIMEOUT_MS", "30000")) / 1000
        if timeout_seconds < 1:
            timeout_seconds = 30

        model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        max_completion_tokens = int(os.getenv("GROQ_MAX_COMPLETION_TOKENS", "4096"))
        if max_completion_tokens < 256:
            max_completion_tokens = 4096
        prompt = (
            "Analyze the meeting transcript accurately and conservatively. "
            "Return valid JSON only. Do not invent facts, names, owners, deadlines, "
            "decisions or commitments. Preserve the transcript's meaning.\n\n"
            "Required JSON shape:\n"
            "{"
            "\"summary\":\"string\","
            "\"speaker_statements\":[{\"speaker\":\"string\",\"text\":\"string\"}],"
            "\"decisions\":[{\"text\":\"string\",\"source_passage\":\"exact transcript substring\",\"owner\":\"string or null\"}],"
            "\"action_items\":[{\"text\":\"string\",\"owner\":\"string or null\",\"deadline\":\"string or null\","
            "\"priority\":\"Low|Medium|High\",\"source_passage\":\"exact transcript substring\"}],"
            "\"important_points\":[{\"text\":\"string\",\"speaker\":\"string or null\",\"source_passage\":\"exact transcript substring\"}]"
            "}\n\n"
            "Rules:\n"
            "1. Separate confirmed decisions from suggestions or discussion points.\n"
            "2. Extract explicit responsible persons only. Never infer owners.\n"
            "3. Extract deadlines exactly as stated. When a date is revised, prefer the latest confirmed revised date.\n"
            "4. Include important risks or unresolved questions in important_points only when stated.\n"
            "5. Every source_passage must be an exact contiguous substring of the transcript.\n"
            "6. Preserve revised-date meaning: for example, original December 15 revised to December 22 means December 22 is final.\n\n"
            f"Meeting title: {title}\n\nTranscript:\n{transcript}"
        )

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a meeting analysis system. Return only JSON matching the requested schema.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_completion_tokens": max_completion_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout_seconds,
        )

        if response.status_code >= 400:
            LOGGER.warning(
                "Groq API request failed | type=HTTPError status=%s detail=%s",
                response.status_code,
                safe_groq_error_detail(response),
            )
            return None

        payload = response.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            LOGGER.warning("Groq returned an empty structured response")
            return None

        parsed = GeminiMeetingOutput.model_validate_json(extract_json_object(content))
        validate_source_passages(parsed, transcript, "Groq")

        return MeetingAnalysis(
            summary=parsed.summary,
            speaker_statements=parsed.speaker_statements,
            decisions=parsed.decisions,
            action_items=parsed.action_items,
            conflicts=detect_conflicts(parsed.decisions),
            important_points=parsed.important_points,
            analysis_mode="groq",
        )
    except requests.Timeout:
        LOGGER.warning("Groq request timed out")
        return None
    except ValidationError:
        LOGGER.warning("Groq returned malformed structured output")
        return None
    except ValueError as exc:
        LOGGER.warning(
            "Groq response validation failed | type=%s detail=%s",
            type(exc).__name__,
            str(exc)[:300],
        )
        return None
    except requests.RequestException as exc:
        LOGGER.warning(
            "Groq API request failed | type=%s detail=%s",
            type(exc).__name__,
            str(exc).replace(api_key, "[REDACTED]")[:500],
        )
        return None
    except Exception as exc:
        LOGGER.warning(
            "Groq analysis failed | type=%s detail=%s",
            type(exc).__name__,
            str(exc).replace(api_key, "[REDACTED]")[:500],
        )
        return None


def call_ai_provider(title: str, transcript: str) -> MeetingAnalysis:
    groq_result = call_groq(title, transcript)
    if groq_result is not None:
        return groq_result

    gemini_result = call_gemini(title, transcript)
    if gemini_result.analysis_mode == "gemini":
        return gemini_result

    fallback_reason = "Groq and Gemini were unavailable; rule-based fallback was used."
    if not os.getenv("GROQ_API_KEY"):
        fallback_reason = "Groq API key is not configured; Gemini was unavailable or failed; rule-based fallback was used."
    return heuristic_analysis(title, transcript, fallback_reason)


def deterministic_decision_comparison(
    new_decision: str,
    evidence_items: list[MemoryDecisionEvidence],
) -> DecisionComparisonResult:
    if not evidence_items:
        return DecisionComparisonResult(
            classification="unrelated",
            confidence=0.95,
            explanation="No relevant historical decision was found.",
            provider="deterministic",
            evidence=[],
        )

    new_lower = new_decision.lower()
    evidence = evidence_items[0]
    old_lower = evidence.decision.lower()

    new_words = set(re.findall(r"[a-z0-9]+", new_lower))
    old_words = set(re.findall(r"[a-z0-9]+", old_lower))
    stop_words = {
        "the", "a", "an", "and", "or", "to", "of", "for", "we", "i",
        "will", "should", "has", "have", "is", "was", "be", "with",
    }
    new_meaning = new_words - stop_words
    old_meaning = old_words - stop_words
    overlap = len(new_meaning & old_meaning)
    union = len(new_meaning | old_meaning)
    similarity = overlap / union if union else 0.0

    approved_markers = (
        "approved",
        "approved change",
        "explicitly",
        "change approved",
        "team approved",
        "revised",
        "updated",
    )
    contradiction_pairs = (
        ("launch", "delay"),
        ("launch", "cancel"),
        ("increase", "decrease"),
        ("increase", "reduce"),
        ("approve", "reject"),
        ("remote", "office"),
        ("yes", "no"),
        ("build", "buy"),
        ("before", "after"),
    )

    evidence_output = [
        DecisionEvidence(
            meeting_id=item.meeting_id,
            meeting_title=item.meeting_title,
            decision_id=item.decision_id,
            decision=item.decision,
            source_passage=item.source_passage,
            created_at=item.created_at,
        )
        for item in evidence_items[:5]
    ]

    if similarity >= 0.82:
        classification = "repetition"
        confidence = min(0.99, 0.82 + similarity * 0.15)
        explanation = "The new decision substantially repeats the historical decision."
    elif any(marker in new_lower for marker in approved_markers):
        classification = "approved_change"
        confidence = 0.88 if overlap >= 2 else 0.72
        explanation = "The new text explicitly describes an approved or revised decision."
    elif any(
        (left in new_lower and right in old_lower)
        or (right in new_lower and left in old_lower)
        for left, right in contradiction_pairs
    ):
        classification = "contradiction"
        confidence = 0.86 if overlap >= 2 else 0.68
        explanation = "The decisions contain opposing direction or status terms."
    elif similarity >= 0.42:
        classification = "clarification"
        confidence = min(0.86, 0.55 + similarity * 0.4)
        explanation = "The decision remains related but adds or clarifies context."
    else:
        classification = "unrelated"
        confidence = min(0.92, 0.58 + (1 - similarity) * 0.25)
        explanation = "The historical decision is not sufficiently related to this decision."

    return DecisionComparisonResult(
        classification=classification,
        confidence=round(confidence, 2),
        explanation=explanation,
        provider="deterministic",
        evidence=evidence_output,
    )


def call_groq_decision_comparison(
    new_decision: str,
    evidence_items: list[MemoryDecisionEvidence],
) -> Optional[DecisionComparisonProviderOutput]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or not evidence_items:
        return None

    import requests

    evidence_payload = [
        {
            "meeting_id": item.meeting_id,
            "meeting_title": item.meeting_title,
            "decision": item.decision,
            "source_passage": item.source_passage,
        }
        for item in evidence_items[:5]
    ]
    prompt = (
        "Compare one new decision with historical decisions. "
        "Choose exactly one classification: contradiction, approved_change, "
        "clarification, repetition, or unrelated. "
        "Do not treat wording differences as contradictions. "
        "Distinguish an approved change from an unapproved contradiction. "
        "Return only structured JSON with classification, confidence, and explanation.\n\n"
        f"New decision: {new_decision}\n"
        f"Historical evidence: {json.dumps(evidence_payload, ensure_ascii=False)}"
    )

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            "messages": [
                {
                    "role": "system",
                    "content": "Return only a JSON object matching the requested decision comparison schema.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_completion_tokens": int(
                os.getenv("GROQ_MAX_COMPLETION_TOKENS", "4096")
            ),
            "response_format": {"type": "json_object"},
        },
        timeout=float(os.getenv("GROQ_TIMEOUT_MS", "30000")) / 1000,
    )
    if response.status_code >= 400:
        LOGGER.warning(
            "Groq decision comparison failed | status=%s detail=%s",
            response.status_code,
            safe_groq_error_detail(response),
        )
        return None
    payload = response.json()
    content = payload.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(content, str):
        return None
    return DecisionComparisonProviderOutput.model_validate_json(
        extract_json_object(content)
    )


def call_gemini_decision_comparison(
    new_decision: str,
    evidence_items: list[MemoryDecisionEvidence],
) -> Optional[DecisionComparisonProviderOutput]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not evidence_items:
        return None

    from google import genai
    from google.genai import types

    evidence_payload = [
        {
            "meeting_id": item.meeting_id,
            "meeting_title": item.meeting_title,
            "decision": item.decision,
            "source_passage": item.source_passage,
        }
        for item in evidence_items[:5]
    ]
    prompt = (
        "Compare the new decision with historical decisions. "
        "Classify as contradiction, approved_change, clarification, repetition, or unrelated. "
        "Do not treat wording differences as contradictions. "
        f"New decision: {new_decision}\n"
        f"Historical evidence: {json.dumps(evidence_payload, ensure_ascii=False)}"
    )
    response = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))
        ),
    ).models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": DecisionComparisonProviderOutput,
        },
    )
    if not getattr(response, "text", None):
        return None
    return DecisionComparisonProviderOutput.model_validate_json(response.text)


def compare_decisions(
    new_decision: str,
    meeting_id: Optional[int] = None,
    source_passage: Optional[str] = None,
    owner: Optional[str] = None,
    memory_evidence: Optional[list[MemoryDecisionEvidence]] = None,
) -> DecisionComparisonResult:
    if memory_evidence is None:
        words = re.findall(r"[a-zA-Z0-9]+", new_decision)
        stop_words = {
            "the", "a", "an", "and", "or", "to", "of", "for", "we", "i",
            "will", "should", "has", "have", "is", "was", "be", "with",
        }
        keywords = [
            word.lower()
            for word in words
            if len(word) >= 4 and word.lower() not in stop_words
        ][:8]
        memory = search_memory(
            keywords=keywords,
            owner=owner,
            exclude_meeting_id=meeting_id,
            limit=20,
        )
        evidence_items = memory.decisions
    else:
        evidence_items = memory_evidence

    try:
        provider_output = call_groq_decision_comparison(new_decision, evidence_items)
        provider = "groq"
    except Exception as exc:
        LOGGER.warning("Groq decision comparison failed | type=%s", type(exc).__name__)
        provider_output = None
        provider = "deterministic"

    if provider_output is None:
        try:
            provider_output = call_gemini_decision_comparison(new_decision, evidence_items)
            provider = "gemini"
        except Exception as exc:
            LOGGER.warning("Gemini decision comparison failed | type=%s", type(exc).__name__)
            provider_output = None
            provider = "deterministic"

    evidence_output = [
        DecisionEvidence(
            meeting_id=item.meeting_id,
            meeting_title=item.meeting_title,
            decision_id=item.decision_id,
            decision=item.decision,
            source_passage=item.source_passage,
            created_at=item.created_at,
        )
        for item in evidence_items[:5]
    ]

    if provider_output is not None:
        return DecisionComparisonResult(
            classification=provider_output.classification,
            confidence=provider_output.confidence,
            explanation=provider_output.explanation,
            provider=provider,
            evidence=evidence_output,
        )

    return deterministic_decision_comparison(new_decision, evidence_items)


FOUNDATION_AGENT_STEPS = [
    (
        "orchestrator",
        "plan_workflow",
        "Prepare a meeting execution workflow",
    ),
    (
        "memory",
        "retrieve_context",
        "Retrieve relevant prior meeting context",
    ),
    (
        "decision",
        "compare_decisions",
        "Plan decision extraction and drift review",
    ),
    (
        "commitment",
        "assess_commitments",
        "Analyze ownership, deadlines and duplicate commitments",
    ),
    (
        "risk",
        "prioritize_risk",
        "Calculate and rank commitment risk",
    ),
    (
        "intervention",
        "recommend_interventions",
        "Recommend practical next actions",
    ),
    (
        "coordinator",
        "coordinate_workflow",
        "Share agent results and coordinate final run status",
    ),
]


def resolve_run_provider(requested_provider: Optional[str], analysis_mode: Optional[str] = None) -> str:
    if analysis_mode in ("groq", "gemini", "deterministic_rules"):
        return analysis_mode
    if requested_provider in ("groq", "gemini", "deterministic_rules"):
        return requested_provider
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return "deterministic_rules"


def create_agent_run(meeting_id: Optional[int], provider: str) -> dict:
    if meeting_id is not None:
        with get_db() as db:
            meeting = db.execute(
                "SELECT id FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
            if meeting is None:
                raise HTTPException(status_code=404, detail="Meeting not found")

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO agent_runs (meeting_id, status, provider)
            VALUES (?, 'planned', ?)
            """,
            (meeting_id, provider),
        )
        run_id = cursor.lastrowid
        for step_order, (agent_role, operation, description) in enumerate(
            FOUNDATION_AGENT_STEPS,
            start=1,
        ):
            db.execute(
                """
                INSERT INTO agent_steps
                    (run_id, step_order, agent_role, operation, description, status)
                VALUES (?, ?, ?, ?, ?, 'planned')
                """,
                (run_id, step_order, agent_role, operation, description),
            )

    return get_agent_run(run_id)


def get_agent_run(run_id: int) -> dict:
    with get_db() as db:
        run = db.execute(
            """
            SELECT id, meeting_id, status, provider, created_at, updated_at
            FROM agent_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Agent run not found")

        steps = db.execute(
            """
            SELECT id, run_id, step_order, agent_role, operation, description,
                   status, created_at
            FROM agent_steps
            WHERE run_id = ?
            ORDER BY step_order ASC
            """,
            (run_id,),
        ).fetchall()
        tools = db.execute(
            """
            SELECT id, run_id, tool_name, validated_arguments, status,
                   result_summary, created_at
            FROM tool_executions
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()

    return {
        **dict(run),
        "steps": [dict(step) for step in steps],
        "tool_executions": [dict(tool) for tool in tools],
    }


def record_tool_execution(
    run_id: int,
    tool_name: str,
    arguments: dict,
    status: str,
    result_summary: Optional[str] = None,
) -> int:
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO tool_executions
                (run_id, tool_name, validated_arguments, status, result_summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                status,
                result_summary,
            ),
        )
        return cursor.lastrowid


def update_agent_step(run_id: int, operation: str, status: str) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE agent_steps
            SET status = ?
            WHERE run_id = ? AND operation = ?
            """,
            (status, run_id, operation),
        )
        db.execute(
            """
            UPDATE agent_runs
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (status, run_id),
        )


def execute_memory_and_decision_tools(
    run_id: int,
    meeting_id: Optional[int],
) -> dict:
    memory_status = "success"
    comparison_status = "success"
    summaries = []

    try:
        memory = search_memory(
            exclude_meeting_id=meeting_id,
            limit=20,
        )
        memory_summary = {
            "meeting_ids": sorted(
                {item.meeting_id for item in memory.decisions}
            ),
            "decision_count": len(memory.decisions),
            "open_action_count": len(memory.open_actions),
            "decisions": [
                {
                    "meeting_id": item.meeting_id,
                    "decision_id": item.decision_id,
                    "decision": item.decision,
                    "source_passage": item.source_passage,
                }
                for item in memory.decisions[:10]
            ],
        }
        record_tool_execution(
            run_id,
            "search_memory",
            {
                "exclude_meeting_id": meeting_id,
                "limit": 20,
            },
            "success",
            json.dumps(memory_summary, ensure_ascii=False),
        )
        update_agent_step(run_id, "retrieve_context", "success")
    except Exception as exc:
        memory_status = "failed"
        record_tool_execution(
            run_id,
            "search_memory",
            {"exclude_meeting_id": meeting_id, "limit": 20},
            "failed",
            f"Memory retrieval failed: {type(exc).__name__}",
        )
        update_agent_step(run_id, "retrieve_context", "failed")

    if meeting_id is not None:
        with get_db() as db:
            decisions = db.execute(
                """
                SELECT id, text, source_passage, owner
                FROM decisions
                WHERE meeting_id = ?
                ORDER BY id ASC
                """,
                (meeting_id,),
            ).fetchall()
    else:
        decisions = []

    for decision in decisions:
        try:
            comparison = compare_decisions(
                decision["text"],
                meeting_id=meeting_id,
                source_passage=decision["source_passage"],
                owner=decision["owner"],
                memory_evidence=memory.decisions if "memory" in locals() else None,
            )
            comparison_summary = {
                "new_decision": decision["text"],
                "classification": comparison.classification,
                "confidence": comparison.confidence,
                "explanation": comparison.explanation,
                "provider": comparison.provider,
                "evidence": [
                    {
                        "meeting_id": item.meeting_id,
                        "meeting_title": item.meeting_title,
                        "decision_id": item.decision_id,
                        "source_passage": item.source_passage,
                    }
                    for item in comparison.evidence
                ],
            }
            record_tool_execution(
                run_id,
                "compare_decisions",
                {
                    "decision_id": decision["id"],
                    "meeting_id": meeting_id,
                },
                "success",
                json.dumps(comparison_summary, ensure_ascii=False),
            )
            summaries.append(comparison_summary)
        except Exception as exc:
            comparison_status = "failed"
            record_tool_execution(
                run_id,
                "compare_decisions",
                {
                    "decision_id": decision["id"],
                    "meeting_id": meeting_id,
                },
                "failed",
                f"Decision comparison failed: {type(exc).__name__}",
            )

    if decisions:
        update_agent_step(
            run_id,
            "compare_decisions",
            comparison_status,
        )
    else:
        update_agent_step(run_id, "compare_decisions", "success")

    commitment_status = "success"
    risk_status = "success"
    intervention_status = "success"

    try:
        commitments = analyze_commitments(meeting_id)
        record_tool_execution(
            run_id,
            "analyze_commitments",
            {"meeting_id": meeting_id},
            "success",
            json.dumps(commitments.model_dump(), ensure_ascii=False),
        )
        update_agent_step(run_id, "assess_commitments", "success")
    except Exception as exc:
        commitment_status = "failed"
        record_tool_execution(
            run_id,
            "analyze_commitments",
            {"meeting_id": meeting_id},
            "failed",
            f"Commitment analysis failed: {type(exc).__name__}",
        )
        update_agent_step(run_id, "assess_commitments", "failed")

    try:
        risks = calculate_commitment_risk(meeting_id)
        record_tool_execution(
            run_id,
            "calculate_commitment_risk",
            {"meeting_id": meeting_id},
            "success",
            json.dumps(risks.model_dump(), ensure_ascii=False),
        )
        update_agent_step(run_id, "prioritize_risk", "success")
    except Exception as exc:
        risk_status = "failed"
        record_tool_execution(
            run_id,
            "calculate_commitment_risk",
            {"meeting_id": meeting_id},
            "failed",
            f"Risk calculation failed: {type(exc).__name__}",
        )
        update_agent_step(run_id, "prioritize_risk", "failed")

    try:
        interventions = recommend_interventions(meeting_id)
        record_tool_execution(
            run_id,
            "recommend_interventions",
            {"meeting_id": meeting_id},
            "success",
            json.dumps(
                [item.model_dump() for item in interventions],
                ensure_ascii=False,
            ),
        )
        update_agent_step(run_id, "recommend_interventions", "success")

        approval_count = 0
        for intervention in interventions:
            if meeting_id is None:
                continue
            priority = (
                "High"
                if intervention.urgency in ("high", "critical")
                else "Medium"
            )
            arguments = {
                "meeting_id": meeting_id,
                "text": f"Follow up: {intervention.task}",
                "owner": None,
                "deadline": None,
                "priority": priority,
                "source_passage": intervention.task,
            }
            with get_db() as db:
                existing_action = db.execute(
                    """
                    SELECT id FROM action_items
                    WHERE meeting_id = ? AND text = ? AND status != 'Done'
                    LIMIT 1
                    """,
                    (meeting_id, arguments["text"]),
                ).fetchone()
                existing_approval = db.execute(
                    """
                    SELECT id FROM approval_requests
                    WHERE tool_name = 'create_local_task'
                      AND action_arguments = ?
                      AND status IN ('pending', 'approved')
                    LIMIT 1
                    """,
                    (json.dumps(arguments, ensure_ascii=False),),
                ).fetchone()
            if existing_action is not None or existing_approval is not None:
                continue
            try:
                create_approval_request(
                    run_id,
                    "create_local_task",
                    arguments,
                    intervention.reason,
                    {
                        "intervention": intervention.intervention,
                        "urgency": intervention.urgency,
                        "action_id": intervention.action_id,
                        "meeting_id": intervention.meeting_id,
                        "task": intervention.task,
                    },
                )
                approval_count += 1
            except HTTPException:
                continue
        if approval_count > 0:
            with get_db() as db:
                db.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'waiting_approval', updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (run_id,),
                )
    except Exception as exc:
        intervention_status = "failed"
        record_tool_execution(
            run_id,
            "recommend_interventions",
            {"meeting_id": meeting_id},
            "failed",
            f"Intervention planning failed: {type(exc).__name__}",
        )
        update_agent_step(run_id, "recommend_interventions", "failed")

    failure_statuses = [
        memory_status,
        comparison_status,
        commitment_status,
        risk_status,
        intervention_status,
    ]
    has_failures = "failed" in failure_statuses
    with get_db() as db:
        pending = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM approval_requests arq
            JOIN agent_runs ar ON ar.id = arq.run_id
            WHERE ar.meeting_id = ? AND arq.status = 'pending'
            """,
            (meeting_id,),
        ).fetchone()["count"]
    if has_failures:
        overall_status = "partial_failed"
    elif pending > 0:
        overall_status = "waiting_approval"
    else:
        overall_status = "completed"

    workflow_summary = {
        "memory_status": memory_status,
        "comparison_status": comparison_status,
        "commitment_status": commitment_status,
        "risk_status": risk_status,
        "intervention_status": intervention_status,
        "pending_approvals": pending,
        "comparisons": len(summaries),
    }
    record_tool_execution(
        run_id,
        "coordinated_workflow",
        {"meeting_id": meeting_id},
        "failed" if has_failures else "success",
        json.dumps(workflow_summary, ensure_ascii=False),
    )
    update_agent_step(
        run_id,
        "coordinate_workflow",
        "failed" if has_failures else "success",
    )
    with get_db() as db:
        db.execute(
            """
            UPDATE agent_runs
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (overall_status, run_id),
        )

    return {
        "memory_status": memory_status,
        "comparison_status": comparison_status,
        "commitment_status": commitment_status,
        "risk_status": risk_status,
        "intervention_status": intervention_status,
        "comparisons": summaries,
    }


@app.post("/meetings/analyze", response_model=dict)
def analyze_meeting(
    submission: TranscriptSubmission,
    user: dict = Depends(require_user),
):
    title = submission.title.strip()
    transcript = submission.transcript.strip()
    analysis = call_ai_provider(title, transcript)

    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO meetings (title, transcript, summary, analysis_mode) VALUES (?, ?, ?, ?)",
            (title, transcript, analysis.summary, analysis.analysis_mode),
        )
        meeting_id = cursor.lastrowid
        for decision in analysis.decisions:
            db.execute(
                "INSERT INTO decisions (meeting_id, text, source_passage, owner) VALUES (?, ?, ?, ?)",
                (meeting_id, decision.text, decision.source_passage, decision.owner),
            )
        for action in analysis.action_items:
            db.execute(
                "INSERT INTO action_items (meeting_id, text, source_passage, owner, deadline, priority) VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, action.text, action.source_passage, action.owner, action.deadline, action.priority),
            )
        for statement in analysis.speaker_statements:
            db.execute(
                "INSERT INTO speaker_statements (meeting_id, speaker, text) VALUES (?, ?, ?)",
                (meeting_id, statement.speaker, statement.text),
            )
        for point in analysis.important_points:
            db.execute(
                "INSERT INTO important_points (meeting_id, text, speaker, source_passage) VALUES (?, ?, ?, ?)",
                (meeting_id, point.text, point.speaker, point.source_passage),
            )
        for conflict in analysis.conflicts:
            db.execute(
                "INSERT INTO conflicts (meeting_id, new_decision, previous_decision, previous_meeting, reason) VALUES (?, ?, ?, ?, ?)",
                (meeting_id, conflict.new_decision, conflict.previous_decision, conflict.previous_meeting, conflict.reason),
            )

    agent_run = create_agent_run(
        meeting_id,
        resolve_run_provider(None, analysis.analysis_mode),
    )
    execute_memory_and_decision_tools(agent_run["id"], meeting_id)
    agent_run = get_agent_run(agent_run["id"])

    return {
        "meeting_id": meeting_id,
        "title": title,
        "agent_run_id": agent_run["id"],
        **analysis.model_dump(),
    }


@app.post("/agent/runs")
def start_agent_run(
    request: AgentRunCreate,
    user: dict = Depends(require_user),
):
    provider = resolve_run_provider(request.provider)
    run = create_agent_run(request.meeting_id, provider)
    execute_memory_and_decision_tools(run["id"], request.meeting_id)
    return get_agent_run(run["id"])


@app.get("/agent/runs")
def list_agent_runs(user: dict = Depends(require_user)):
    with get_db() as db:
        runs = db.execute(
            """
            SELECT ar.id, ar.meeting_id, ar.status, ar.provider,
                   ar.created_at, ar.updated_at,
                   m.title AS meeting_title
            FROM agent_runs ar
            LEFT JOIN meetings m ON m.id = ar.meeting_id
            ORDER BY ar.id DESC
            """
        ).fetchall()
    return [dict(run) for run in runs]


@app.get("/agent/runs/{run_id}")
def retrieve_agent_run(run_id: int, user: dict = Depends(require_user)):
    return get_agent_run(run_id)


@app.get("/agent/tool-executions")
def retrieve_tool_executions(
    run_id: Optional[int] = None,
    user: dict = Depends(require_user),
):
    with get_db() as db:
        if run_id is None:
            executions = db.execute(
                """
                SELECT id, run_id, tool_name, validated_arguments, status,
                       result_summary, created_at
                FROM tool_executions
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            executions = db.execute(
                """
                SELECT id, run_id, tool_name, validated_arguments, status,
                       result_summary, created_at
                FROM tool_executions
                WHERE run_id = ?
                ORDER BY id DESC
                """,
                (run_id,),
            ).fetchall()
    return [dict(execution) for execution in executions]


@app.post("/tools/search-memory")
def tool_search_memory(
    request: MemorySearchRequest,
    user: dict = Depends(require_user),
):
    result = search_memory(
        keywords=request.keywords,
        meeting=request.meeting,
        meeting_id=request.meeting_id,
        owner=request.owner,
        exclude_meeting_id=request.exclude_meeting_id,
        limit=request.limit,
    )
    if request.run_id is not None:
        record_tool_execution(
            request.run_id,
            "search_memory",
            {
                "keywords": request.keywords,
                "meeting": request.meeting,
                "meeting_id": request.meeting_id,
                "owner": request.owner,
                "exclude_meeting_id": request.exclude_meeting_id,
                "limit": request.limit,
            },
            "success",
            json.dumps(
                {
                    "meeting_ids": sorted(
                        {item.meeting_id for item in result.decisions}
                    ),
                    "decision_count": len(result.decisions),
                    "open_action_count": len(result.open_actions),
                    "decisions": [
                        {
                            "meeting_id": item.meeting_id,
                            "decision_id": item.decision_id,
                            "decision": item.decision,
                            "source_passage": item.source_passage,
                        }
                        for item in result.decisions[:10]
                    ],
                },
                ensure_ascii=False,
            ),
        )
        update_agent_step(request.run_id, "retrieve_context", "success")
    return result.model_dump()


@app.post("/tools/compare-decisions")
def tool_compare_decisions(
    request: DecisionComparisonRequest,
    user: dict = Depends(require_user),
):
    result = compare_decisions(
        request.new_decision,
        meeting_id=request.meeting_id,
        source_passage=request.source_passage,
        owner=request.owner,
    )
    if request.run_id is not None:
        record_tool_execution(
            request.run_id,
            "compare_decisions",
            {
                "new_decision": request.new_decision,
                "meeting_id": request.meeting_id,
                "owner": request.owner,
            },
            "success",
            json.dumps(
                {
                    "classification": result.classification,
                    "confidence": result.confidence,
                    "explanation": result.explanation,
                    "provider": result.provider,
                    "evidence": [
                        {
                            "meeting_id": item.meeting_id,
                            "meeting_title": item.meeting_title,
                            "decision_id": item.decision_id,
                            "source_passage": item.source_passage,
                        }
                        for item in result.evidence
                    ],
                },
                ensure_ascii=False,
            ),
        )
        update_agent_step(request.run_id, "compare_decisions", "success")
    return result.model_dump()


@app.post("/tools/analyze-commitments")
def tool_analyze_commitments(
    request: CommitmentToolRequest,
    user: dict = Depends(require_user),
):
    result = analyze_commitments(request.meeting_id)
    if request.run_id is not None:
        record_tool_execution(
            request.run_id,
            "analyze_commitments",
            {"meeting_id": request.meeting_id},
            "success",
            json.dumps(result.model_dump(), ensure_ascii=False),
        )
        update_agent_step(request.run_id, "assess_commitments", "success")
    return result.model_dump()


@app.post("/tools/calculate-risk")
def tool_calculate_commitment_risk(
    request: CommitmentToolRequest,
    user: dict = Depends(require_user),
):
    result = calculate_commitment_risk(request.meeting_id)
    if request.run_id is not None:
        record_tool_execution(
            request.run_id,
            "calculate_commitment_risk",
            {"meeting_id": request.meeting_id},
            "success",
            json.dumps(result.model_dump(), ensure_ascii=False),
        )
        update_agent_step(request.run_id, "prioritize_risk", "success")
    return result.model_dump()


@app.post("/tools/recommend-interventions")
def tool_recommend_interventions(
    request: CommitmentToolRequest,
    user: dict = Depends(require_user),
):
    result = recommend_interventions(request.meeting_id)
    if request.run_id is not None:
        record_tool_execution(
            request.run_id,
            "recommend_interventions",
            {"meeting_id": request.meeting_id},
            "success",
            json.dumps([item.model_dump() for item in result], ensure_ascii=False),
        )
        update_agent_step(request.run_id, "recommend_interventions", "success")
    return [item.model_dump() for item in result]


@app.post("/approvals")
def create_approval(
    request: ApprovalCreateRequest,
    user: dict = Depends(require_user),
):
    return create_approval_request(
        request.run_id,
        request.tool_name,
        request.arguments,
        request.reason,
        request.evidence,
    )


@app.get("/approvals")
def list_approvals(
    status: Optional[str] = None,
    user: dict = Depends(require_user),
):
    with get_db() as db:
        if status:
            rows = db.execute(
                """
                SELECT ar.id, ar.run_id, ar.tool_name, ar.action_arguments,
                       ar.reason, ar.evidence, ar.status, ar.created_at,
                       ar.updated_at, ar.decision_at, ar.execution_status,
                       ar.execution_result, ar.executed_at,
                       run.meeting_id
                FROM approval_requests ar
                JOIN agent_runs run ON run.id = ar.run_id
                WHERE ar.status = ?
                ORDER BY ar.id DESC
                """,
                (status,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT ar.id, ar.run_id, ar.tool_name, ar.action_arguments,
                       ar.reason, ar.evidence, ar.status, ar.created_at,
                       ar.updated_at, ar.decision_at, ar.execution_status,
                       ar.execution_result, ar.executed_at,
                       run.meeting_id
                FROM approval_requests ar
                JOIN agent_runs run ON run.id = ar.run_id
                ORDER BY ar.id DESC
                """,
            ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["action_arguments"] = json.loads(item["action_arguments"])
        item["evidence"] = json.loads(item["evidence"])
        item["execution_result"] = (
            json.loads(item["execution_result"])
            if item["execution_result"]
            else None
        )
        results.append(item)
    return results


@app.get("/approvals/{approval_id}")
def retrieve_approval(
    approval_id: int,
    user: dict = Depends(require_user),
):
    return get_approval_request(approval_id)


@app.post("/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: int,
    request: ApprovalDecisionRequest,
    user: dict = Depends(require_user),
):
    approval = get_approval_request(approval_id)
    if approval["status"] in ("approved", "rejected"):
        return approval

    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="Approval request is no longer pending")

    with get_db() as db:
        db.execute(
            """
            UPDATE approval_requests
            SET status = ?, updated_at = datetime('now'), decision_at = datetime('now')
            WHERE id = ? AND status = 'pending'
            """,
            (request.decision, approval_id),
        )

    record_tool_execution(
        approval["run_id"],
        "approval_decision",
        {
            "approval_id": approval_id,
            "decision": request.decision,
            "tool_name": approval["tool_name"],
        },
        "success",
        f"Approval request {approval_id} was {request.decision}.",
    )
    return get_approval_request(approval_id)


@app.post("/approvals/{approval_id}/execute")
def execute_approval(
    approval_id: int,
    user: dict = Depends(require_user),
):
    approval = get_approval_request(approval_id)
    if approval["status"] == "rejected":
        raise HTTPException(status_code=409, detail="Rejected approval cannot execute")
    if approval["status"] == "expired":
        raise HTTPException(status_code=409, detail="Expired approval cannot execute")
    if approval["status"] != "approved":
        raise HTTPException(status_code=409, detail="Approval is pending and cannot execute")

    if approval["execution_status"] in ("executed", "failed"):
        return approval

    try:
        validated_arguments = validate_local_tool_arguments(
            approval["tool_name"],
            approval["action_arguments"],
        )
    except (ValidationError, ValueError) as exc:
        execution_result = {
            "success": False,
            "tool": approval["tool_name"],
            "message": "Invalid tool arguments.",
        }
        with get_db() as db:
            db.execute(
                """
                UPDATE approval_requests
                SET execution_status = 'failed',
                    execution_result = ?,
                    executed_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (json.dumps(execution_result), approval_id),
            )
        record_tool_execution(
            approval["run_id"],
            approval["tool_name"],
            approval["action_arguments"],
            "failed",
            "Invalid arguments at execution time.",
        )
        return get_approval_request(approval_id)

    result = execute_registered_local_tool(
        approval["run_id"],
        approval["tool_name"],
        validated_arguments,
    )
    execution_status = "executed" if result["success"] else "failed"
    with get_db() as db:
        db.execute(
            """
            UPDATE approval_requests
            SET execution_status = ?,
                execution_result = ?,
                executed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                execution_status,
                json.dumps(result, ensure_ascii=False),
                approval_id,
            ),
        )
        pending = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM approval_requests
            WHERE run_id = ? AND status = 'pending'
            """,
            (approval["run_id"],),
        ).fetchone()["count"]
        approved_pending = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM approval_requests
            WHERE run_id = ? AND status = 'approved'
              AND execution_status = 'not_executed'
            """,
            (approval["run_id"],),
        ).fetchone()["count"]
        failed_steps = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM agent_steps
            WHERE run_id = ? AND status = 'failed'
            """,
            (approval["run_id"],),
        ).fetchone()["count"]
        if execution_status == "failed" or failed_steps:
            run_status = "partial_failed"
        elif pending or approved_pending:
            run_status = "waiting_approval"
        else:
            run_status = "completed"
        db.execute(
            """
            UPDATE agent_runs
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (run_status, approval["run_id"]),
        )
    return get_approval_request(approval_id)


@app.get("/meetings")
def list_meetings(user: dict = Depends(require_user)):
    with get_db() as db:
        meetings = db.execute(
            """
            SELECT m.id, m.title, m.summary, m.analysis_mode, m.created_at,
                   (SELECT COUNT(*) FROM decisions d WHERE d.meeting_id = m.id) AS decision_count,
                   (SELECT COUNT(*) FROM action_items a WHERE a.meeting_id = m.id) AS action_count
            FROM meetings m
            ORDER BY m.id DESC
            """
        ).fetchall()
    return [dict(row) for row in meetings]


@app.get("/actions")
def list_actions(user: dict = Depends(require_user)):
    with get_db() as db:
        actions = db.execute(
            """
            SELECT a.id, a.text, a.owner, a.deadline, a.priority, a.status,
                   m.title AS meeting_title
            FROM action_items a
            JOIN meetings m ON m.id = a.meeting_id
            ORDER BY a.id DESC
            """
        ).fetchall()
    return [dict(row) for row in actions]


@app.get("/decisions")
def list_decisions(user: dict = Depends(require_user)):
    with get_db() as db:
        decisions = db.execute(
            """
            SELECT d.id, d.text, d.source_passage, d.owner, m.title AS meeting_title,
                   m.created_at
            FROM decisions d
            JOIN meetings m ON m.id = d.meeting_id
            ORDER BY d.id DESC
            """
        ).fetchall()
    return [dict(row) for row in decisions]


@app.get("/drift")
def list_conflicts(user: dict = Depends(require_user)):
    with get_db() as db:
        conflicts = db.execute(
            """
            SELECT c.*, m.title AS meeting_title
            FROM conflicts c
            JOIN meetings m ON m.id = c.meeting_id
            ORDER BY c.id DESC
            """
        ).fetchall()
    return [dict(row) for row in conflicts]


@app.get("/risk")
def commitment_risk_radar(user: dict = Depends(require_user)):
    today = date.today()
    with get_db() as db:
        actions = db.execute(
            """
            SELECT a.id, a.text, a.owner, a.deadline, a.priority, a.status,
                   m.title AS meeting_title
            FROM action_items a
            JOIN meetings m ON m.id = a.meeting_id
            WHERE a.status != 'Done'
            ORDER BY a.id DESC
            """
        ).fetchall()

    risks = []
    for action in actions:
        risk = "Medium"
        reason = "Ownership or deadline needs attention."
        deadline = action["deadline"]
        if not action["owner"] or not deadline:
            risk = "High"
            reason = "Owner or deadline is unclear."
        elif deadline:
            parsed = None
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"):
                try:
                    parsed = datetime.strptime(deadline, fmt).date()
                    if parsed.year == 1900:
                        parsed = parsed.replace(year=today.year)
                    break
                except ValueError:
                    continue
            if parsed and parsed < today:
                risk = "High"
                reason = "Deadline is overdue."
            elif parsed and (parsed - today).days <= 3:
                risk = "High"
                reason = "Deadline is approaching."
        risks.append({**dict(action), "risk": risk, "reason": reason})
    return risks

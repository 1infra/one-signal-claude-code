#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Claude Code -> One Signal (via One Connector) hook.

Derived from Langfuse's Claude-Observability-Plugin
(https://github.com/langfuse/Claude-Observability-Plugin), MIT License,
Copyright (c) 2026 Langfuse GmbH. See ../LICENSE for the full original
license text and the additional One Infra license covering the changes
below.

The ONLY architectural change from the original: instead of talking to
Langfuse directly with Langfuse API keys (via the `langfuse` Python SDK,
which in SDK v4 exports over OpenTelemetry/OTLP), this hook constructs the
same Langfuse *classic ingestion* batch shape by hand --
`{"batch": [...], "metadata": {...}}`, where each element is an envelope
`{id, timestamp, type, body}` per
https://api.reference.langfuse.com/#post-/api/public/ingestion -- and POSTs
it to our own One Connector ingest proxy, authenticated with a One
Connector access token (`Authorization: Bearer oc_...`). Users never see or
handle Langfuse credentials; the organization's real Langfuse project lives
behind One Connector. This also drops the `langfuse` / `opentelemetry`
third-party dependencies entirely -- everything below uses only the Python
standard library.

All transcript-parsing, turn-assembly, truncation, and incremental-upload-
state logic (roughly the first two-thirds of this file) is carried over
from the original almost verbatim; only the emit layer at the bottom
(originally OTel spans via the Langfuse SDK) has been rewritten to build
plain ingestion-event dicts and ship them over HTTP with urllib.
"""

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_VERSION = "0.0.1"

# --- Paths ---
STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "one_signal_hook.log"
STATE_FILE = STATE_DIR / "one_signal_state.json"
LOCK_FILE = STATE_DIR / "one_signal_state.lock"

def _opt(name: str) -> str:
    """Read a plugin userConfig value (CLAUDE_PLUGIN_OPTION_<NAME>) with a fallback to a plain env var."""
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}") or os.environ.get(name) or ""

DEBUG = _opt("CC_ONE_SIGNAL_DEBUG").lower() == "true"
SKILL_TAGS = (_opt("CC_ONE_SIGNAL_SKILL_TAGS") or "true").lower() == "true"
CAPTURE_SKILL_CONTENT = _opt("CC_ONE_SIGNAL_CAPTURE_SKILL_CONTENT").lower() == "true"
try:
    MAX_CHARS = int(_opt("CC_ONE_SIGNAL_MAX_CHARS") or "20000")
except ValueError:
    MAX_CHARS = 20000

# Server-side caps for the ingest proxy (mirrors Langfuse's own
# /api/public/ingestion batch-size constraints: "Batch sizes are limited to
# 3.5 MB in total" and a per-batch event-count cap of 200).
MAX_EVENTS_PER_BATCH = 200
MAX_BYTES_PER_BATCH = 3_500_000

# ----------------- Logging -----------------
_logger: Optional[logging.Logger] = None

def _get_logger() -> Optional[logging.Logger]:
    global _logger
    if _logger is not None:
        return _logger
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("one_signal_hook")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=3)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(h)
        _logger = lg
        return _logger
    except Exception:
        return None

def debug(msg: str) -> None:
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.debug(msg)
        except Exception:
            pass

def info(msg: str) -> None:
    lg = _get_logger()
    if lg is not None:
        try:
            lg.info(msg)
        except Exception:
            pass

def warning(msg: str) -> None:
    lg = _get_logger()
    if lg is not None:
        try:
            lg.warning(msg)
        except Exception:
            pass

# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        self.acquired = False
        try:
            import fcntl  # Unix only
        except ImportError:
            # No fcntl available (e.g. Windows) — proceed without lock.
            return self
        deadline = time.time() + self.timeout_s
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.acquired = True
                    return self
                except BlockingIOError:
                    if time.time() > deadline:
                        raise TimeoutError(
                            f"could not acquire {self.path} within {self.timeout_s}s"
                        )
                    time.sleep(0.05)
        except BaseException:
            # __exit__ is not called when __enter__ raises — close the fh
            # we just opened so it doesn't leak.
            try:
                self._fh.close()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    tmp: Optional[Path] = None
    try:
        # Drop session entries older than 30 days to keep the file bounded.
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for k in list(state.keys()):
            entry = state.get(k)
            if not isinstance(entry, dict):
                continue
            updated = entry.get("updated")
            if not isinstance(updated, str):
                continue
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                del state[k]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp filename (pid + random suffix) instead of a fixed ".tmp"
        # name, and fsync before the atomic rename -- a fixed temp name can be
        # clobbered by a concurrent Stop/SessionEnd hook process racing to write
        # the same state file, and without fsync the rename can land before the
        # new bytes are durable.
        tmp = STATE_DIR / f"{STATE_FILE.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        tmp = None
    except Exception as e:
        debug(f"save_state failed: {e}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

def state_key(session_id: str, transcript_path: str) -> str:
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    """
    Claude Code hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        debug(f"stdin received {len(data)} chars")
        if not data.strip():
            return {}
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            debug(f"payload top-level keys: {sorted(parsed.keys())}")
        return parsed
    except Exception as e:
        debug(f"read_hook_payload exception: {e!r}")
        return {}

def extract_session_id_and_transcript_path(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Path]]:
    """
    Tries a few plausible field names; exact keys can vary across hook types/versions.
    Prefer structured values from stdin over heuristics.
    """
    session_id = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )

    transcript_path_raw = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or payload.get("transcript", {}).get("path")
    )

    if transcript_path_raw:
        try:
            transcript_path = Path(transcript_path_raw).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path

# ----------------- Transcript parsing helpers -----------------
def get_content_from_row(row: Dict[str, Any]) -> Any:
    if not isinstance(row, dict):
        return None
    message = row.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return row.get("content")

def get_user_or_assistant_role_from_row(row: Dict[str, Any]) -> Optional[str]:
    # Claude Code transcript row format is internal. Prefer top-level row.type
    # when it marks a chat row, then fall back to nested message.role.
    row_type = row.get("type")
    if row_type in ("user", "assistant"):
        return row_type

    message = row.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if role in ("user", "assistant"):
            return role
    return None

def is_tool_result(row: Dict[str, Any]) -> bool:
    role = get_user_or_assistant_role_from_row(row)
    if role != "user":
        return False
    content = get_content_from_row(row)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def get_tool_result_blocks(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def get_tool_use_blocks(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"

def get_usage_details_from_row(row: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Extract Anthropic token usage from an assistant message, if present."""
    m = row.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage")
    if not isinstance(u, dict):
        return None
    details: Dict[str, int] = {}
    for src, dst in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    ):
        v = u.get(src)
        if isinstance(v, int) and v > 0:
            details[dst] = v
    return details or None

def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None

def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a Claude Code jsonl row timestamp (ISO 8601 with trailing Z)."""
    if isinstance(value, dict):
        value = value.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0       # Last byte position fully committed for this session (safe
                           # to never re-read below this point). Only ever advanced up
                           # to the end of a turn that was BOTH completely parsed (FIX
                           # B) AND durably accepted upstream (FIX A) -- see main().
    turn_count: int = 0   # Turns already committed (built + fully accepted) for this
                           # session. Used both as the resume point for turn numbering
                           # and as the count that gets reported/tested.

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, start_offset: int) -> List[Tuple[Dict[str, Any], int]]:
    """
    Reads raw bytes from start_offset to the current end of the transcript file and
    returns only COMPLETE JSONL rows, each paired with the absolute byte offset in the
    file immediately after that row's line (including its terminating newline).

    FIX C (multibyte-safe reading): bytes are split on the raw b"\\n" separator BEFORE
    any decoding happens, and only complete (newline-terminated) lines are decoded/
    parsed. A JSON serializer never emits a raw 0x0A byte inside a string value (it
    escapes real newlines as the two-byte sequence "\\n"), and 0x0A never appears as a
    continuation byte in a multi-byte UTF-8 sequence -- so splitting on b"\\n" first is
    always safe and each decode() call is anchored on a real, fully-written line
    boundary. A trailing line with no terminating newline (still being written, or a
    snapshot taken mid-write) is intentionally NOT decoded or returned here -- it, and
    anything appended after it, will be re-read from the same start_offset on a later
    call once the caller decides how much of THIS batch to actually commit (see FIX A/
    FIX B commit logic in main()).

    Never raises -- returns [] on any I/O surprise.
    """
    if not transcript_path.exists():
        return []

    offset = start_offset
    try:
        file_size = transcript_path.stat().st_size
        if file_size < offset:
            # Transcript was rotated or truncated — restart from the beginning.
            debug(f"transcript shrank ({file_size} < {offset}); restarting")
            offset = 0
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return []

    if not chunk:
        return []

    lines = chunk.split(b"\n")
    complete_lines = lines[:-1]  # last element has no trailing "\n" -- partial or empty

    rows: List[Tuple[Dict[str, Any], int]] = []
    pos = offset
    for line_bytes in complete_lines:
        pos += len(line_bytes) + 1  # +1 for the newline byte consumed by split()
        stripped = line_bytes.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped.decode("utf-8"))
        except Exception as e:
            debug(f"skipping unparsable jsonl line: {type(e).__name__}: {e}")
            continue
        rows.append((row, pos))

    return rows

# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]
    # Injected context (e.g. skill instructions) keyed by the tool_use id it
    # belongs to, taken from isMeta rows carrying sourceToolUseID.
    injected_by_tool_id: Dict[str, str]
    # FIX B: absolute byte offset in the transcript file immediately after the
    # last row that contributed to this turn. This is the furthest point the
    # incremental-read checkpoint may ever advance to on behalf of this turn --
    # never past it, since bytes after it may belong to a still-incomplete next
    # turn (e.g. a trailing user row with no assistant response yet).
    end_offset: int

def merge_assistant_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Claude Code can split one assistant message across multiple JSONL rows that
    share message.id. Merge them back into one logical message by concatenating
    content blocks in row order.
    """
    base: Dict[str, Any] = dict(rows[-1])
    last_message = rows[-1].get("message")
    merged_message: Dict[str, Any] = dict(last_message) if isinstance(last_message, dict) else {}

    merged_content: List[Any] = []
    for row in rows:
        message_obj = row.get("message")
        if not isinstance(message_obj, dict):
            continue

        content_blocks = message_obj.get("content")
        if isinstance(content_blocks, list):
            merged_content.extend(content_blocks)
        elif isinstance(content_blocks, str) and content_blocks:
            merged_content.append({"type": "text", "text": content_blocks})

    merged_message["content"] = merged_content
    base["message"] = merged_message
    return base


def build_turns(rows: List[Tuple[Dict[str, Any], int]]) -> List[Turn]:
    """
    Groups incremental transcript rows into turns:
    user (non-tool-result) -> assistant messages -> (tool_result rows, possibly interleaved)
    Uses:
    - assistant rows merged by message.id (all content blocks concatenated)
    - tool results dedupe by tool_use_id (latest wins)

    `rows` is a list of (row, end_offset) pairs as produced by read_new_jsonl(), where
    end_offset is the absolute byte offset immediately after that row's line. Each
    emitted Turn carries the end_offset of the LAST row that contributed to it (FIX B),
    so a caller can commit the read checkpoint exactly at a turn boundary and never
    past a still-incomplete trailing turn (e.g. a user row with no assistant reply yet)
    -- such a trailing turn is simply never flushed into `turns` below, exactly as
    before; the only change here is threading end_offset through so the byte
    checkpoint can finally track that same boundary instead of jumping to EOF.
    """
    turns: List[Turn] = []
    current_turn_user_row: Optional[Dict[str, Any]] = None
    pending_end_offset: int = 0  # end_offset of the last row consumed for the in-progress turn

    # assistant messages for current turn:
    assistant_message_ids: List[str] = []             # message ids in order of first appearance (or synthetic)
    assistant_rows_by_message_id: Dict[str, List[Dict[str, Any]]] = {}  # id -> all rows (merged at flush)

    tool_results_by_id: Dict[str, Any] = {}     # tool_use_id -> content
    injected_by_tool_id: Dict[str, str] = {}    # tool_use_id -> injected text (skill instructions)

    def flush_turn():
        nonlocal current_turn_user_row, assistant_message_ids, assistant_rows_by_message_id, tool_results_by_id, injected_by_tool_id, turns
        if current_turn_user_row is None:
            return
        if not assistant_rows_by_message_id:
            return
        # Rebuild one assistant message per message.id, in the order the ids
        # first appeared. assistant_rows_by_message_id[message_id] holds all raw rows that shared that
        # id; merge_assistant_rows concatenates their content blocks into one.
        merged_assistant_rows: List[Dict[str, Any]] = []
        for message_id in assistant_message_ids:
            rows_for_id = assistant_rows_by_message_id.get(message_id)
            if not rows_for_id:
                continue
            merged_assistant_rows.append(merge_assistant_rows(rows_for_id))
        turns.append(Turn(
            user_msg=current_turn_user_row,
            assistant_msgs=merged_assistant_rows,
            tool_results_by_id=dict(tool_results_by_id),
            injected_by_tool_id=dict(injected_by_tool_id),
            end_offset=pending_end_offset,
        ))

    for row, row_offset in rows:
        # Injected user rows (slash-command expansions, caveats, skill instructions)
        # carry isMeta=true. They are not real prompts — treating them as turn starts
        # creates phantom turns and prematurely flushes the real one.
        if row.get("isMeta"):
            # Skill invocations link their injected instructions to the originating
            # tool_use via sourceToolUseID; keep the text so emit can optionally
            # attach it to that tool span.
            src = row.get("sourceToolUseID")
            if src:
                txt = extract_text(get_content_from_row(row))
                if txt:
                    injected_by_tool_id[str(src)] = txt
            if current_turn_user_row is not None:
                pending_end_offset = row_offset
            continue

        role = get_user_or_assistant_role_from_row(row)

        # tool_result rows show up as role=user with content blocks of type tool_result
        if is_tool_result(row):
            row_ts = row.get("timestamp")
            for tr in get_tool_result_blocks(get_content_from_row(row)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = {
                        "content": tr.get("content"),
                        "timestamp": row_ts,
                        "is_error": tr.get("is_error"),
                    }
            if current_turn_user_row is not None:
                pending_end_offset = row_offset
            continue

        if role == "user":
            # new user message -> finalize previous turn
            flush_turn()

            # start a new turn
            current_turn_user_row = row
            pending_end_offset = row_offset
            assistant_message_ids = []
            assistant_rows_by_message_id = {}
            tool_results_by_id = {}
            injected_by_tool_id = {}
            continue

        if role == "assistant":
            if current_turn_user_row is None:
                # ignore assistant rows until we see a user message
                continue

            message_id = get_message_id(row) or f"noid:{len(assistant_message_ids)}"
            if message_id not in assistant_rows_by_message_id:
                assistant_message_ids.append(message_id)
                assistant_rows_by_message_id[message_id] = []
            assistant_rows_by_message_id[message_id].append(row)
            pending_end_offset = row_offset
            continue

        # ignore unknown rows, but still track their offset if they fall inside an
        # in-progress turn's byte range
        if current_turn_user_row is not None:
            pending_end_offset = row_offset

    # flush last -- if it has at least one assistant reply, it's a complete turn (even
    # though nothing closed it via a following user row); if it's just a trailing user
    # row with no reply yet, flush_turn() is a no-op and its bytes stay uncommitted.
    flush_turn()
    return turns

# ----------------- One Signal ingestion-batch construction -----------------
# The classic Langfuse ingestion envelope shape: {"batch": [event, ...], "metadata": {...}}
# where each event is {"id": <event-uuid>, "timestamp": <iso8601>, "type": <event-type>,
# "body": {...}}. See https://api.reference.langfuse.com/#post-/api/public/ingestion.
# We build this by hand (no `langfuse` SDK / OpenTelemetry dependency) since the
# proxy this hook talks to is our own One Connector endpoint, not Langfuse directly.

def _iso(ts: Optional[datetime]) -> Optional[str]:
    """Format a datetime as millisecond-resolution ISO-8601 UTC ('...Z'), as the ingestion API expects."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _event_envelope(event_type: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": _iso(datetime.now(timezone.utc)),
        "type": event_type,
        "body": {k: v for k, v in body.items() if v is not None},
    }

def _trace_create(*, trace_id: str, name: str, user_id: Optional[str], session_id: str,
                   input_: Any, output: Any, metadata: Dict[str, Any], tags: List[str],
                   timestamp: Optional[datetime]) -> Dict[str, Any]:
    return _event_envelope("trace-create", {
        "id": trace_id,
        "timestamp": _iso(timestamp),
        "name": name,
        "userId": user_id,
        "sessionId": session_id,
        "input": input_,
        "output": output,
        "metadata": metadata,
        "tags": tags,
    })

def _observation_create(*, obs_id: str, trace_id: str, parent_id: Optional[str], obs_type: str,
                         name: str, start_time: Optional[datetime], end_time: Optional[datetime],
                         input_: Any = None, output: Any = None, model: Optional[str] = None,
                         usage_details: Optional[Dict[str, int]] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _event_envelope("observation-create", {
        "id": obs_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "type": obs_type,
        "name": name,
        "startTime": _iso(start_time),
        "endTime": _iso(end_time),
        "input": input_,
        "output": output,
        "model": model,
        "usageDetails": usage_details,
        "metadata": metadata,
    })

def collect_skill_names(turn: Turn) -> List[str]:
    """Return every explicitly invoked Skill name once, preserving call order."""
    names: List[str] = []
    for am in turn.assistant_msgs:
        for tu in get_tool_use_blocks(get_content_from_row(am)):
            if tu.get("name") != "Skill":
                continue
            tu_input = tu.get("input")
            if not isinstance(tu_input, dict):
                continue
            skill = next((tu_input.get(key) for key in ("name", "skill", "skill_name", "skillName")
                          if isinstance(tu_input.get(key), str) and tu_input.get(key)), None)
            if isinstance(skill, str) and skill not in names:
                names.append(skill)
    return names


def mcp_attribution(tool_name: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    return (parts[1], "__".join(parts[2:])) if len(parts) >= 3 else None


def collect_mcp_tags(turn: Turn) -> List[str]:
    tags: List[str] = []
    for am in turn.assistant_msgs:
        for tool_use in get_tool_use_blocks(get_content_from_row(am)):
            mcp = mcp_attribution(tool_use.get("name"))
            tag = f"mcp:{mcp[0]}:{mcp[1]}" if mcp else None
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def short_session_label(session_id: str, max_len: int = 12) -> str:
    """Return a compact session label for trace names."""
    sid = session_id.strip()
    if not sid:
        return "unknown"
    parts = sid.split("-")
    if len(parts) == 5 and len(parts[0]) == 8:
        return parts[0]
    return sid if len(sid) <= max_len else sid[:max_len].rstrip("-")


def trace_display_name(session_id: str, turn_num: int) -> str:
    return f"Claude Code - Turn {turn_num} ({short_session_label(session_id)})"


def build_turn_events(session_id: str, turn_num: int, turn: Turn, transcript_path: Path,
                       user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Builds the ingestion-batch events for one turn: one trace-create, one root
    SPAN observation ("Turn N"), one GENERATION observation per assistant
    message (nested under the root span), and one TOOL observation per tool
    call (nested under the generation that issued it) -- the same tree shape
    the original built with Langfuse SDK / OTel spans, just expressed as
    plain ingestion-event dicts instead.
    """
    events: List[Dict[str, Any]] = []

    user_text_raw = extract_text(get_content_from_row(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    final_assistant_text, _ = truncate_text(extract_text(get_content_from_row(last_assistant)))

    user_ts = parse_timestamp(turn.user_msg)
    last_assistant_ts = parse_timestamp(last_assistant)
    # Pick a turn end_time: latest among final assistant message or any tool result
    candidate_end_ts = [t for t in [last_assistant_ts] if t is not None]
    for tr in turn.tool_results_by_id.values():
        t = parse_timestamp(tr)
        if t is not None:
            candidate_end_ts.append(t)
    turn_end_ts = max(candidate_end_ts) if candidate_end_ts else None

    trace_metadata: Dict[str, Any] = {
        "source": "claude-code",
        "session_id": session_id,
        "turn_number": turn_num,
        "transcript_path": str(transcript_path),
        "user_text": user_text_meta,
        "assistant_message_count": len(turn.assistant_msgs),
    }
    # Transcript rows carry the project dir and git branch — surface them so
    # traces from different projects/worktrees are distinguishable in One Signal.
    for src_key, dst_key in (("cwd", "cwd"), ("gitBranch", "git_branch")):
        v = turn.user_msg.get(src_key)
        if isinstance(v, str) and v:
            trace_metadata[dst_key] = v

    skill_names = collect_skill_names(turn) if SKILL_TAGS else []
    if skill_names:
        trace_metadata["skill_names"] = skill_names

    tags = ["claude-code", *collect_mcp_tags(turn)]
    if SKILL_TAGS:
        tags += [f"skill:{name}" for name in skill_names]

    trace_name = trace_display_name(session_id, turn_num)
    root_observation_name = f"Turn {turn_num}"

    # Deterministic ids (not random UUIDs) so that re-emitting the same
    # session_id + turn_num — which should not normally happen once state has
    # advanced past it, but keeps behavior safe if it ever does — upserts the
    # same trace/observations in One Signal rather than forking new ones.
    trace_id = f"{session_id}-t{turn_num}"
    root_obs_id = f"{trace_id}-root"

    events.append(_trace_create(
        trace_id=trace_id,
        name=trace_name,
        user_id=user_id,
        session_id=session_id,
        input_={"role": "user", "content": user_text},
        output={"role": "assistant", "content": final_assistant_text},
        metadata=trace_metadata,
        tags=tags,
        timestamp=user_ts,
    ))
    events.append(_observation_create(
        obs_id=root_obs_id,
        trace_id=trace_id,
        parent_id=None,
        obs_type="SPAN",
        name=root_observation_name,
        start_time=user_ts,
        end_time=turn_end_ts or last_assistant_ts or user_ts,
        input_={"role": "user", "content": user_text},
        output={"role": "assistant", "content": final_assistant_text},
        metadata=trace_metadata,
    ))

    # Iterate each assistant message: emit a generation, then its tool_use children.
    # prev_ts = the moment the next generation could have started (= when the previous
    # batch of tool results all returned, or the original user message timestamp).
    prev_ts = user_ts
    prev_tool_results: List[Dict[str, Any]] = []  # populated after each batch, surfaced as next gen's input

    for idx, am in enumerate(turn.assistant_msgs):
        am_ts = parse_timestamp(am)
        am_text_raw = extract_text(get_content_from_row(am))
        am_text, am_text_meta = truncate_text(am_text_raw)
        model = get_model(am)
        tool_uses = get_tool_use_blocks(get_content_from_row(am))

        # Build generation input: user message for first generation, otherwise tool results from
        # the prior batch (best partial reconstruction of the prompt context).
        if idx == 0:
            gen_input: Any = {"role": "user", "content": user_text}
        elif prev_tool_results:
            gen_input = {"role": "tool", "tool_results": prev_tool_results}
        else:
            gen_input = None

        # Build generation output: include both the text response and any tool calls the LLM
        # decided to make. Most assistant messages in tool-using turns are tool-call-only, so
        # without tool_calls in the output, the observation looks empty.
        gen_tool_calls = []
        for tu in tool_uses:
            tu_input = tu.get("input")
            if isinstance(tu_input, str):
                tu_input_serialized, _ = truncate_text(tu_input)
            else:
                tu_input_serialized = tu_input
            gen_tool_calls.append({
                "id": tu.get("id"),
                "name": tu.get("name"),
                "input": tu_input_serialized,
            })

        gen_output: Dict[str, Any] = {"role": "assistant"}
        if am_text:
            gen_output["content"] = am_text
        if gen_tool_calls:
            gen_output["tool_calls"] = gen_tool_calls

        usage_details = get_usage_details_from_row(am)
        gen_id = f"{root_obs_id}-gen{idx + 1}"

        # Tool observations: nested under this generation. Each starts when the assistant
        # emitted the tool_use (am_ts) and ends when its tool_result row arrived.
        batch_result_ts: List[datetime] = []
        batch_tool_results: List[Dict[str, Any]] = []
        tool_events: List[Dict[str, Any]] = []
        for t_idx, tu in enumerate(tool_uses):
            tid = str(tu.get("id") or "")
            tname = tu.get("name") or "unknown"
            tinput_raw = tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {}
            if isinstance(tinput_raw, str):
                tinput, tinput_meta = truncate_text(tinput_raw)
            else:
                tinput, tinput_meta = tinput_raw, None

            tr_entry = turn.tool_results_by_id.get(tid) if tid else None
            if tr_entry:
                out_raw = tr_entry.get("content")
                out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
                out_trunc, out_meta = truncate_text(out_str)
                tr_ts = parse_timestamp(tr_entry.get("timestamp"))
            else:
                out_trunc, out_meta, tr_ts = None, None, None
            if tr_ts is not None:
                batch_result_ts.append(tr_ts)

            # Skill invocations inject their instructions as a separate transcript
            # row; optionally surface them on the tool span they belong to.
            tool_output: Any = out_trunc
            if CAPTURE_SKILL_CONTENT:
                injected = turn.injected_by_tool_id.get(tid) if tid else None
                if injected:
                    injected_trunc, _ = truncate_text(injected)
                    tool_output = {"result": out_trunc, "injected_instructions": injected_trunc}

            tool_obs_id = f"{gen_id}-tool{t_idx + 1}"
            mcp = mcp_attribution(tname)
            result_status = (
                "error" if tr_entry and tr_entry.get("is_error") is True
                # Claude Code normally omits `is_error` for successful tool
                # results, so the presence of a matched result is success.
                else "success" if tr_entry is not None
                else "unknown"
            )
            tool_events.append(_observation_create(
                obs_id=tool_obs_id,
                trace_id=trace_id,
                parent_id=gen_id,
                # The classic Langfuse ingestion API (POST /api/public/ingestion) only
                # accepts GENERATION | SPAN | EVENT as an observation-create `type` --
                # a `type: "TOOL"` event is silently rejected inside the batch's
                # per-event 207 response (see post_batch()/deliver() below for how
                # those per-event errors are now surfaced). Emit "SPAN" here and mark
                # the tool-call intent via metadata.tool_name/tool_id instead; the
                # One Signal read side (apps/server/src/modules/signal/service.ts)
                # infers type "TOOL" back from that metadata when rendering the span
                # tree.
                obs_type="SPAN",
                name=f"Tool: {tname}",
                start_time=am_ts,
                end_time=tr_ts or am_ts,
                input_=tinput,
                output=tool_output,
                metadata={
                    "tool_name": tname,
                    "tool_id": tid,
                    "result_status": result_status,
                    **({"mcp_server": mcp[0], "mcp_tool": mcp[1]} if mcp else {}),
                    "input_meta": tinput_meta,
                    "output_meta": out_meta,
                },
            ))

            batch_tool_results.append({
                "tool_use_id": tid,
                "tool_name": tname,
                "output": out_trunc,
            })

        # End the generation AFTER its tools so the timeline cleanly contains them.
        # If there were tool calls, gen ends with the last result; otherwise at am_ts.
        gen_end_ts = max(batch_result_ts) if batch_result_ts else am_ts
        events.append(_observation_create(
            obs_id=gen_id,
            trace_id=trace_id,
            parent_id=root_obs_id,
            obs_type="GENERATION",
            name=f"Claude Generation {idx + 1}",
            start_time=prev_ts or am_ts,
            end_time=gen_end_ts or am_ts or prev_ts,
            input_=gen_input,
            output=gen_output,
            model=model,
            usage_details=usage_details,
            metadata={
                "assistant_index": idx,
                "assistant_text": am_text_meta,
                "tool_count": len(tool_uses),
            },
        ))
        events.extend(tool_events)

        # Carry this batch's results into the next generation's input.
        prev_tool_results = batch_tool_results

        # Advance prev_ts: next generation can only start after this batch's tool results returned.
        if batch_result_ts:
            prev_ts = max(batch_result_ts)
        elif am_ts is not None:
            prev_ts = am_ts

    return events

# ----------------- Chunking (server caps: events count + body bytes) -----------------
def _event_size_bytes(event: Dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))

def chunk_indices(events: List[Dict[str, Any]], max_events: int = MAX_EVENTS_PER_BATCH,
                   max_bytes: int = MAX_BYTES_PER_BATCH) -> List[List[int]]:
    """Same greedy chunking as chunk_batch(), but returns index groups (into `events`)
    instead of the events themselves, so a caller can keep a parallel per-event array
    (e.g. which turn each event belongs to, for FIX A's per-turn accept tracking) in
    sync with the chunks actually sent over the wire."""
    groups: List[List[int]] = []
    current: List[int] = []
    current_bytes = 0
    envelope_slack = 2048
    budget = max(max_bytes - envelope_slack, 1)

    for i, event in enumerate(events):
        size = _event_size_bytes(event)
        if current and (len(current) >= max_events or current_bytes + size > budget):
            groups.append(current)
            current = []
            current_bytes = 0
        current.append(i)
        current_bytes += size

    if current:
        groups.append(current)

    return groups

def chunk_batch(events: List[Dict[str, Any]], max_events: int = MAX_EVENTS_PER_BATCH,
                 max_bytes: int = MAX_BYTES_PER_BATCH) -> List[List[Dict[str, Any]]]:
    """Splits events into request-sized chunks respecting both the per-batch event-count
    cap and an approximate byte-size cap (leaving slack for the {"batch":[...],"metadata":
    {...}} envelope and per-request headers). A single event larger than the byte budget
    still gets shipped alone in its own chunk — it can't be split further."""
    return [[events[i] for i in group] for group in chunk_indices(events, max_events, max_bytes)]

# ----------------- Transport -----------------
def _extract_ingestion_errors(body: bytes) -> List[Dict[str, Any]]:
    """Best-effort parse of a Langfuse classic-ingestion 207 response body's per-event
    `errors` list (shape: {"successes": [...], "errors": [{"id", "status", "message"}, ...]}).
    Never raises -- returns [] on any parse surprise so a malformed/empty body can't
    turn a partial-success delivery into a crash."""
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    errors = parsed.get("errors")
    if not isinstance(errors, list):
        return []
    return [e for e in errors if isinstance(e, dict)]

def post_batch(events: List[Dict[str, Any]], base_url: str, api_token: str,
               metadata: Dict[str, Any]) -> bool:
    """POST one chunk to the One Signal ingest endpoint. Never raises — always returns a
    bool so the hook can continue and ultimately exit 0 (hooks must never break the
    user's Claude session).

    FIX A: the returned bool means "every event in this chunk was DURABLY accepted
    upstream" -- true for a plain 2xx, or a 207 whose per-event `errors` list is empty.
    A 207 with a non-empty `errors` list returns False even though the HTTP request
    itself succeeded, because at least one event in the chunk was rejected -- the
    caller (deliver()/main()) must not advance the checkpoint past any turn that had an
    event in this chunk."""
    url = base_url.rstrip("/") + "/api/v1/observe/ingest"
    payload = json.dumps({"batch": events, "metadata": metadata}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            body = resp.read()
            if status == 207:
                # 207 Multi-Status means Langfuse accepted the request but may have
                # rejected individual events inside the batch (e.g. an unsupported
                # observation `type`) -- surface those per-event errors instead of
                # letting a "partial success" look identical to a full one, and treat
                # ANY per-event error as "this chunk was not fully accepted" for
                # checkpoint purposes.
                errors = _extract_ingestion_errors(body)
                for err in errors:
                    warning(
                        "ingest event failed: "
                        f"id={err.get('id')} status={err.get('status')} message={err.get('message')}"
                    )
                debug(f"ingest ok: status={status} events={len(events)} failed={len(errors)}")
                return len(errors) == 0
            elif 200 <= status < 300:
                debug(f"ingest ok: status={status} events={len(events)}")
                return True
            else:
                info(f"ingest unexpected status {status}: {body[:500]!r}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code == 503:
            code = None
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    err = parsed.get("error")
                    code = err.get("code") if isinstance(err, dict) else err
            except Exception:
                pass
            if code == "signal_not_configured":
                debug(
                    "One Signal is not connected for this organization yet — "
                    "connect Langfuse in Console -> Integrations. Dropping this batch."
                )
                return False
        info(f"ingest failed: HTTP {e.code}: {body[:500]!r}")
        return False
    except urllib.error.URLError as e:
        debug(f"ingest network error: {e}")
        return False
    except Exception as e:
        debug(f"ingest request failed unexpectedly: {type(e).__name__}: {e}")
        return False

def deliver(events: List[Dict[str, Any]], event_turn_idx: List[int], base_url: str, api_token: str) -> set:
    """Chunks and delivers all events. Returns the set of turn-local indices (positions
    into the caller's `turns` list for this run) whose events were ALL durably accepted
    upstream.

    FIX A: a turn's events can land in more than one chunk (chunking is purely
    size/count driven and knows nothing about turn boundaries), so a turn only counts
    as accepted if EVERY chunk containing at least one of its events came back fully
    accepted (see post_batch()). event_turn_idx[i] must give the turn-local index that
    events[i] belongs to (same length/order as events)."""
    chunk_idx_groups = chunk_indices(events)
    debug(f"delivering {len(events)} events in {len(chunk_idx_groups)} chunk(s)")

    turn_ok: Dict[int, bool] = {}
    for i, idx_group in enumerate(chunk_idx_groups):
        chunk = [events[j] for j in idx_group]
        fully_accepted = post_batch(chunk, base_url, api_token, metadata={
            "sdk_name": "one-signal-hook",
            "sdk_version": PLUGIN_VERSION,
            "chunk_index": i,
            "chunk_count": len(chunk_idx_groups),
        })
        if not fully_accepted:
            info(f"chunk {i + 1}/{len(chunk_idx_groups)} failed to deliver ({len(chunk)} events)")
        for j in idx_group:
            t = event_turn_idx[j]
            turn_ok[t] = turn_ok.get(t, True) and fully_accepted

    return {t for t, ok in turn_ok.items() if ok}

# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    base_url = _opt("ONE_SIGNAL_BASE_URL") or "https://connector.1infra.io"
    api_token = _opt("ONE_SIGNAL_API_TOKEN")
    user_id = _opt("ONE_SIGNAL_USER_ID") or None

    if not api_token:
        debug("Missing ONE_SIGNAL_API_TOKEN; exiting without emitting.")
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_id_and_transcript_path(payload)

    if not session_id or not transcript_path:
        # No structured payload; fail open (do not guess)
        debug("Missing session_id or transcript_path from hook payload; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript path does not exist: {transcript_path}")
        return 0

    emitted = 0
    committed = 0
    total_events = 0
    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            rows = read_new_jsonl(transcript_path, ss.offset)
            if not rows:
                # Nothing new since the last committed offset.
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(rows)
            if not turns:
                # FIX B: no turn in this batch reached completion yet (e.g. a trailing
                # user row with no assistant reply, or a turn still being written).
                # ss.offset is untouched, so the next hook re-reads these same bytes
                # once the turn actually completes -- nothing is discarded.
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            emitted = len(turns)

            # Build each turn's events separately, keeping track of which turn each
            # event belongs to (event_turn_idx), so delivery acceptance can be
            # attributed back to individual turns for the FIX A commit decision below.
            events: List[Dict[str, Any]] = []
            event_turn_idx: List[int] = []
            for i, t in enumerate(turns):
                turn_num = ss.turn_count + i + 1
                try:
                    turn_events = build_turn_events(session_id, turn_num, t, transcript_path, user_id=user_id)
                except Exception as e:
                    # Log at INFO so build failures are visible without needing
                    # CC_ONE_SIGNAL_DEBUG=true. A turn that fails to build produced no
                    # events, so it can never appear in accepted_idx below -- per the
                    # overriding "never advance past unaccepted data" principle, the
                    # checkpoint holds here too and this turn is retried next run
                    # (same turn_num, since turn_count won't advance past it either).
                    info(f"build_turn_events failed: {type(e).__name__}: {e}")
                    turn_events = []
                events.extend(turn_events)
                event_turn_idx.extend([i] * len(turn_events))

            total_events = len(events)
            accepted_idx = deliver(events, event_turn_idx, base_url, api_token) if events else set()

            # FIX A + FIX B combined commit decision: advance the checkpoint only
            # through the longest PREFIX of this run's turns (starting at turn 0) that
            # were fully accepted upstream. A later turn's acceptance can't be used to
            # skip over an earlier turn's failure -- the byte offset is a single linear
            # cursor, so committing turn i's end_offset implicitly claims turns
            # 0..i-1 are also done. Turns at/after the first gap are left for the next
            # run to retry; deterministic trace/observation ids make that retry an
            # idempotent upsert, not a duplicate.
            for i in range(len(turns)):
                if i in accepted_idx:
                    committed = i + 1
                else:
                    break

            if committed < emitted:
                info(
                    f"only {committed}/{emitted} turn(s) fully accepted upstream this run; "
                    "checkpoint held back so the rest retry next time"
                )

            if committed > 0:
                ss.offset = turns[committed - 1].end_offset
                ss.turn_count += committed
            # else: leave ss.offset/turn_count untouched -- nothing new was durably
            # accepted, so the next run re-reads and re-attempts from the same point.

            write_session_state(state, key, ss)
            save_state(state)

        dur = time.time() - start
        info(
            f"Processed {committed}/{emitted} turns ({total_events} events) in "
            f"{dur:.2f}s (session={session_id})"
        )
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

if __name__ == "__main__":
    sys.exit(main())

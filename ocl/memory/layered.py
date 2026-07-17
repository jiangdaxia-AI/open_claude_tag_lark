"""Layered memory service — memU-style global/task/working memory layers.

Design (borrowed from memU, https://github.com/NevaMind-AI/memU):
- Memory is stored as **files** (one Markdown doc per topic) sliced into
  per-line **segments**, each segment individually embedded.
- Commit reconciles segments: only genuinely new lines get embedded,
  unchanged lines keep their vectors, removed lines are deleted — so
  re-committing a lightly edited file costs almost nothing.
- Retrieval embeds the query once, brute-force cosine over scoped
  segments (data volume is small — thousands of lines), returns ranked
  slices within a character budget. No LLM call, no summarization.

Three layers:
- **global**  — long-lived team knowledge (conventions, decisions, roles).
                Scoped to (channel_id, agent_id). Retrieved on every turn.
- **task**    — current big-task context (PRD progress, review results).
                Scoped to (channel_id, agent_id, session_id). Retrieved
                when the session has active tasks.
- **working** — recent messages from messages.db (no embedding needed).

The agent's LLM curation decides WHAT to write; this service handles
HOW it's stored and retrieved.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import struct
from pathlib import Path

from ocl.config import settings
from ocl.memory.embedder import embed_query, embed_texts

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,               -- 'global' | 'task'
    session_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'P2',
    created_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    updated_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    UNIQUE(channel_id, agent_id, scope, session_id, name)
);

CREATE TABLE IF NOT EXISTS memory_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
    line_text TEXT NOT NULL,
    embedding BLOB,                    -- struct-packed float32 array, NULL until embedded
    created_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    UNIQUE(file_id, line_text)
);

CREATE INDEX IF NOT EXISTS idx_segments_file ON memory_segments(file_id);
CREATE INDEX IF NOT EXISTS idx_files_scope
    ON memory_files(channel_id, agent_id, scope, session_id);
"""

_MAX_EMBED_BATCH = 32  # SiliconFlow accepts large batches; keep requests small


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _content_lines(content: str) -> list[str]:
    """Split content into segment lines. Headings and blank lines are skipped
    (they carry no standalone semantic value), matching memU's approach."""
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip common list markers so the embedding focuses on content
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        if line:
            lines.append(line)
    return lines


class LayeredMemoryService:
    """Global/task scoped memory with per-line embedding retrieval."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or (settings.data_dir / "workspaces" / "layered_memory.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Write path                                                          #
    # ------------------------------------------------------------------ #

    async def commit(
        self,
        channel_id: str,
        agent_id: str,
        name: str,
        content: str,
        *,
        scope: str = "global",
        session_id: str = "",
        description: str = "",
        priority: str = "P2",
    ) -> int:
        """Create or update a memory file and reconcile its segments.

        Returns the file id. Only new/changed lines are embedded.
        """
        async with self._lock:
            file_id = self._upsert_file(
                channel_id, agent_id, scope, session_id, name,
                description, content, priority,
            )
            await self._reconcile_segments(file_id, content)
            return file_id

    def _upsert_file(
        self,
        channel_id: str,
        agent_id: str,
        scope: str,
        session_id: str,
        name: str,
        description: str,
        content: str,
        priority: str,
    ) -> int:
        row = self._conn.execute(
            """SELECT id FROM memory_files
               WHERE channel_id=? AND agent_id=? AND scope=? AND session_id=? AND name=?""",
            (channel_id, agent_id, scope, session_id, name),
        ).fetchone()
        if row:
            self._conn.execute(
                """UPDATE memory_files
                   SET content=?, description=?, priority=?, updated_at=unixepoch('now','subsec')
                   WHERE id=?""",
                (content, description, priority, row["id"]),
            )
            self._conn.commit()
            return row["id"]
        cur = self._conn.execute(
            """INSERT INTO memory_files
               (channel_id, agent_id, scope, session_id, name, description, content, priority)
               VALUES (?,?,?,?,?,?,?,?)""",
            (channel_id, agent_id, scope, session_id, name, description, content, priority),
        )
        self._conn.commit()
        return cur.lastrowid

    async def _reconcile_segments(self, file_id: int, content: str) -> None:
        """Diff content lines against stored segments; embed only new lines."""
        new_lines = set(_content_lines(content))
        existing = self._conn.execute(
            "SELECT id, line_text FROM memory_segments WHERE file_id=?", (file_id,)
        ).fetchall()
        existing_map = {r["line_text"]: r["id"] for r in existing}

        # Delete removed lines
        to_delete = [sid for text, sid in existing_map.items() if text not in new_lines]
        if to_delete:
            self._conn.execute(
                f"DELETE FROM memory_segments WHERE id IN ({','.join('?' * len(to_delete))})",
                to_delete,
            )

        # Insert genuinely new lines (embedding=NULL first, embed after)
        to_add = [text for text in new_lines if text not in existing_map]
        for text in to_add:
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_segments (file_id, line_text) VALUES (?,?)",
                (file_id, text),
            )
        self._conn.commit()

        if to_add:
            await self._embed_pending(file_id)

    async def _embed_pending(self, file_id: int) -> None:
        """Embed segments that don't have vectors yet."""
        pending = self._conn.execute(
            "SELECT id, line_text FROM memory_segments WHERE file_id=? AND embedding IS NULL",
            (file_id,),
        ).fetchall()
        for i in range(0, len(pending), _MAX_EMBED_BATCH):
            batch = pending[i:i + _MAX_EMBED_BATCH]
            vectors = await embed_texts([r["line_text"] for r in batch])
            if vectors is None:
                logger.warning(
                    "Embedding failed for file_id=%d — %d segments left unembedded",
                    file_id, len(batch),
                )
                return
            for row, vec in zip(batch, vectors):
                self._conn.execute(
                    "UPDATE memory_segments SET embedding=? WHERE id=?",
                    (_pack_vector(vec), row["id"]),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Read path                                                           #
    # ------------------------------------------------------------------ #

    async def retrieve(
        self,
        channel_id: str,
        agent_id: str,
        query: str,
        *,
        session_id: str = "",
        top_k: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        """Retrieve relevant memory slices for injection into system prompt.

        Searches global-scope segments (always) plus task-scope segments
        for the given session (if provided). Returns a formatted block,
        or empty string when nothing relevant / on any failure.
        """
        if not settings.memory_layered_enabled:
            return ""
        top_k = top_k or settings.memory_retrieve_top_k
        max_chars = max_chars or settings.memory_inject_max_chars

        try:
            query_vec = await embed_query(query)
            if query_vec is None:
                return ""
            return await asyncio.to_thread(
                self._rank_and_format,
                channel_id, agent_id, session_id, query_vec, top_k, max_chars,
            )
        except Exception:
            logger.exception("Layered memory retrieve failed (channel=%s agent=%s)",
                             channel_id, agent_id)
            return ""

    def _rank_and_format(
        self,
        channel_id: str,
        agent_id: str,
        session_id: str,
        query_vec: list[float],
        top_k: int,
        max_chars: int,
    ) -> str:
        # Load segments from global scope + current task scope
        rows = self._conn.execute(
            """SELECT s.line_text, s.embedding, f.scope, f.name, f.priority
               FROM memory_segments s
               JOIN memory_files f ON f.id = s.file_id
               WHERE f.channel_id=? AND f.agent_id=? AND s.embedding IS NOT NULL
                 AND (f.scope='global' OR (f.scope='task' AND f.session_id=?))""",
            (channel_id, agent_id, session_id),
        ).fetchall()
        if not rows:
            return ""

        scored = []
        for r in rows:
            vec = _unpack_vector(r["embedding"])
            score = _cosine(query_vec, vec)
            # Global P1 knowledge gets a small boost so core facts rank higher
            if r["scope"] == "global" and r["priority"] == "P1":
                score += 0.05
            scored.append((score, r["line_text"], r["scope"], r["name"]))

        scored.sort(key=lambda x: x[0], reverse=True)

        lines: list[str] = []
        total = 0
        for score, text, scope, name in scored[:top_k]:
            # Relevance floor calibrated for bge-m3 Chinese: related pairs
            # score ~0.53-0.58, unrelated ~0.36-0.42. 0.45 separates cleanly.
            if score < 0.45:
                break
            entry = f"[{scope}/{name}] {text}"
            if total + len(entry) > max_chars:
                break
            lines.append(entry)
            total += len(entry) + 1

        if not lines:
            return ""
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Maintenance                                                         #
    # ------------------------------------------------------------------ #

    async def list_files(
        self, channel_id: str, agent_id: str | None = None, scope: str | None = None
    ) -> list[dict]:
        """List memory files (for debugging / admin UI)."""
        sql = "SELECT id, channel_id, agent_id, scope, session_id, name, priority, updated_at FROM memory_files WHERE channel_id=?"
        params: list = [channel_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    async def delete_file(self, file_id: int) -> None:
        """Delete a memory file and its segments."""
        async with self._lock:
            self._conn.execute("DELETE FROM memory_files WHERE id=?", (file_id,))
            self._conn.commit()

    async def expire_old(self) -> int:
        """Delete files past their priority's expiry window. Returns count."""
        days_map = {
            "P1": settings.memory_expiry_days_p1,
            "P2": settings.memory_expiry_days_p2,
            "P3": settings.memory_expiry_days_p3,
        }
        total = 0
        async with self._lock:
            for prio, days in days_map.items():
                cur = self._conn.execute(
                    f"""DELETE FROM memory_files
                        WHERE priority=? AND updated_at < unixepoch('now','subsec') - ? * 86400""",
                    (prio, days),
                )
                total += cur.rowcount
            self._conn.commit()
        if total:
            logger.info("Layered memory: expired %d old files", total)
        return total


_service: LayeredMemoryService | None = None


def get_layered_memory() -> LayeredMemoryService:
    global _service
    if _service is None:
        _service = LayeredMemoryService()
    return _service

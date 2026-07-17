"""Web admin dashboard for multi-agent task board and agent management.

FastAPI single-page app:
  - /              → dashboard (task board, agent status, memory preview)
  - /api/channels              → list channels
  - /api/channels/{id}/agents  → list agents and lifecycle status
  - /api/channels/{id}/tasks   → list/create tasks
  - /api/channels/{id}/memories/{agent_id} → view per-agent memory

Run: pip install fastapi uvicorn
Then set WEB_ADMIN_ENABLED=true in .env
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

from ocl.agents.config import load_agents
from ocl.agents.lifecycle import get_lifecycle
from ocl.agents.task_store import task_create, task_get, task_list, task_update
from ocl.config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Open Claude Tag Lark Admin")


def start_admin_server(host: str = "0.0.0.0", port: int = 8765, bg_loop=None) -> None:
    """Start the web admin server in a background thread."""
    import threading
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="ocl-web-admin", daemon=True)
    t.start()
    logger.info("Web admin server started on %s:%d", host, port)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/channels")
async def list_channels() -> list[dict[str, Any]]:
    if not settings.channels_dir.exists():
        return []
    channels = []
    for d in sorted(settings.channels_dir.iterdir()):
        if d.is_dir() and d.name != "agents":
            channels.append({"id": d.name, "name": d.name})
    return channels


@app.get("/api/channels/{channel_id}/agents")
async def list_agents(channel_id: str) -> list[dict[str, Any]]:
    registry = load_agents(channel_id)
    result = []
    for cfg in registry.iter_enabled():
        lc = get_lifecycle(channel_id, cfg.agent_id)
        result.append({
            "id": cfg.agent_id,
            "display_name": cfg.display_name,
            "description": cfg.description,
            "state": "active" if lc.is_active else "sleeping",
            "idle_timeout": lc.idle_timeout_seconds,
        })
    return result


@app.get("/api/channels/{channel_id}/tasks")
async def get_tasks(channel_id: str, status: str | None = None):
    return await task_list(channel_id, status=status)


@app.post("/api/channels/{channel_id}/tasks")
async def create_task(
    channel_id: str,
    title: str = Form(...),
    description: str = Form(""),
    assignee: str = Form(""),
    priority: str = Form("P2"),
):
    try:
        result = await task_create(
            channel_id=channel_id,
            creator="web-admin",
            title=title,
            description=description,
            assignee=assignee,
            priority=priority,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/channels/{channel_id}/tasks/{task_id}")
async def get_task(channel_id: str, task_id: int):
    result = await task_get(channel_id, task_id)
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@app.post("/api/channels/{channel_id}/tasks/{task_id}/status")
async def update_task_status(channel_id: str, task_id: int, status: str = Form(...)):
    result = await task_update(channel_id, task_id, status)
    if not result:
        raise HTTPException(status_code=404, detail="Task not found or invalid status")
    return result


@app.get("/api/channels/{channel_id}/memories/{agent_id}")
async def get_memory(channel_id: str, agent_id: str):
    if agent_id == "default":
        path = settings.channels_dir / channel_id / "MEMORY.md"
    else:
        path = settings.channels_dir / channel_id / "agents" / agent_id / "MEMORY.md"
    if path.exists():
        return {"agent_id": agent_id, "memory": path.read_text(encoding="utf-8")}
    return {"agent_id": agent_id, "memory": ""}


@app.get("/api/health")
async def health():
    from ocl.doctor import run_doctor
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_doctor()
    return {"output": buf.getvalue()}


@app.get("/api/ledger")
async def list_ledger(channel_id: str | None = None, agent_id: str | None = None, status: str | None = None, limit: int = 50):
    from ocl.agents.ledger import list_entries
    return await list_entries(channel_id=channel_id, agent_id=agent_id, status=status, limit=limit)


@app.get("/api/ledger/{entry_id}")
async def get_ledger_entry(entry_id: int):
    from ocl.agents.ledger import get_entry
    result = await get_entry(entry_id)
    if not result:
        raise HTTPException(status_code=404, detail="Ledger entry not found")
    return result


@app.get("/api/token-usage")
async def token_usage_stats(channel_id: str | None = None, days: int = 7):
    """Aggregate token usage stats (observation only, no enforcement)."""
    from ocl.agents.ledger import _get_ledger_db
    import time
    since = time.time() - days * 86400
    db = await _get_ledger_db()
    try:
        # Per-agent breakdown
        if channel_id:
            cursor = await db.execute(
                """SELECT agent_id,
                          SUM(prompt_tokens) as prompt_tokens,
                          SUM(completion_tokens) as completion_tokens,
                          SUM(total_tokens) as total_tokens,
                          COUNT(*) as call_count
                   FROM ledger WHERE channel_id = ? AND started_at >= ?
                   GROUP BY agent_id ORDER BY total_tokens DESC""",
                (channel_id, since),
            )
        else:
            cursor = await db.execute(
                """SELECT agent_id,
                          SUM(prompt_tokens) as prompt_tokens,
                          SUM(completion_tokens) as completion_tokens,
                          SUM(total_tokens) as total_tokens,
                          COUNT(*) as call_count
                   FROM ledger WHERE started_at >= ?
                   GROUP BY agent_id ORDER BY total_tokens DESC""",
                (since,),
            )
        rows = [dict(r) for r in await cursor.fetchall()]
        total = sum(r["total_tokens"] or 0 for r in rows)
        return {"days": days, "total_tokens": total, "by_agent": rows}
    finally:
        await db.close()


@app.post("/api/channels/{channel_id}/agents/{agent_id}/cancel")
async def cancel_agent_run(channel_id: str, agent_id: str):
    from ocl.agents.cancel import cancel_agent
    count = cancel_agent(channel_id, agent_id)
    return {"cancelled": count}


@app.get("/api/channels/{channel_id}/agents/{agent_id}/workspace")
async def list_workspace_files(channel_id: str, agent_id: str):
    from ocl.agents.config import load_agents
    registry = load_agents(channel_id)
    cfg = registry.get(agent_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Agent not found")
    ws = cfg.workspace_dir
    if not ws.exists():
        return {"files": []}
    files = []
    for f in sorted(ws.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime})
    return {"files": files}


@app.get("/api/channels/{channel_id}/agents/{agent_id}/workspace/{filename}")
async def get_workspace_file(channel_id: str, agent_id: str, filename: str):
    from ocl.agents.config import load_agents
    registry = load_agents(channel_id)
    cfg = registry.get(agent_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Agent not found")
    file_path = cfg.workspace_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return {"filename": filename, "content": file_path.read_text(encoding="utf-8")}


@app.put("/api/channels/{channel_id}/agents/{agent_id}/scopes")
async def update_agent_scopes(channel_id: str, agent_id: str, scopes: str = Form("")):
    """Update agent scopes (comma-separated). Empty = all granted."""
    from ocl.agents.config import load_agents
    registry = load_agents(channel_id)
    cfg = registry.get(agent_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Agent not found")
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes else None
    cfg.scopes = scope_list
    return {"agent_id": agent_id, "scopes": scope_list or "all"}


_FRONTEND_FILE = Path(__file__).parent / "web_admin_frontend.html"

_DASHBOARD_HTML = ""
if _FRONTEND_FILE.exists():
    _DASHBOARD_HTML = _FRONTEND_FILE.read_text(encoding="utf-8")
else:
    _DASHBOARD_HTML = """\
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Open Claude Tag Lark Admin</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f0f2f5; }
        .header { background: #1f2329; color: white; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { margin: 0; font-size: 1.4rem; }
        .tabs { display: flex; gap: 0; background: white; border-bottom: 2px solid #e5e7eb; padding: 0 2rem; }
        .tab { padding: 0.75rem 1.5rem; cursor: pointer; border-bottom: 3px solid transparent; font-weight: 500; color: #6b7280; }
        .tab.active { color: #6366f1; border-bottom-color: #6366f1; }
        .content { padding: 2rem; max-width: 1400px; margin: 0 auto; }
        .grid { display: grid; grid-template-columns: 280px 1fr; gap: 1.5rem; }
        .card { background: white; border-radius: 8px; padding: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 1.5rem; }
        .card h2 { margin-top: 0; font-size: 1.1rem; color: #1f2329; }
        .status-active { color: #10b981; font-weight: 600; }
        .status-sleeping { color: #6b7280; font-weight: 600; }
        .status-running { color: #f59e0b; font-weight: 600; }
        .status-completed { color: #10b981; }
        .status-failed { color: #ef4444; }
        .status-cancelled { color: #6b7280; }
        .task { border-bottom: 1px solid #eee; padding: 0.75rem 0; }
        .task:last-child { border-bottom: none; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; background: #e5e7eb; margin-left: 0.5rem; }
        .badge-p1 { background: #fee2e2; color: #ef4444; }
        .badge-p2 { background: #fef3c7; color: #f59e0b; }
        .badge-p3 { background: #d1fae5; color: #10b981; }
        button { cursor: pointer; padding: 4px 12px; border: none; border-radius: 4px; background: #6366f1; color: white; font-size: 0.85rem; }
        button:hover { background: #5558e0; }
        button.danger { background: #ef4444; }
        button.danger:hover { background: #dc2626; }
        ul { list-style: none; padding: 0; margin: 0; }
        li { padding: 0.5rem 0.5rem; cursor: pointer; border-radius: 4px; }
        li:hover { background: #f0f2f5; }
        li.selected { background: #ede9fe; color: #6366f1; font-weight: 600; }
        pre { background: #1f2329; color: #e5e7eb; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; max-height: 500px; }
        .ledger-row { border-bottom: 1px solid #eee; padding: 0.5rem 0; cursor: pointer; }
        .ledger-row:hover { background: #f9fafb; }
        .file-item { padding: 0.5rem; border-bottom: 1px solid #eee; }
        .file-item a { color: #6366f1; text-decoration: none; }
        .file-item a:hover { text-decoration: underline; }
        .hidden { display: none; }
        .scopes-input { width: 100%; padding: 4px 8px; border: 1px solid #d1d5db; border-radius: 4px; font-size: 0.85rem; }
        .stat { display: inline-block; margin-right: 1.5rem; }
        .stat-value { font-size: 1.5rem; font-weight: 700; color: #6366f1; }
        .stat-label { font-size: 0.8rem; color: #6b7280; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🦐 Open Claude Tag Lark 管理后台</h1>
        <button onclick="runDoctor()">健康检查</button>
    </div>
    <div class="tabs">
        <div class="tab active" onclick="showTab('dashboard', this)">概览</div>
        <div class="tab" onclick="showTab('ledger', this)">执行账本</div>
        <div class="tab" onclick="showTab('workspace', this)">工作目录</div>
        <div class="tab" onclick="showTab('health', this)">系统状态</div>
    </div>
    <div class="content">
        <!-- Dashboard Tab -->
        <div id="tab-dashboard">
            <div class="grid">
                <div class="card">
                    <h2>频道列表</h2>
                    <ul id="channels"></ul>
                </div>
                <div>
                    <div class="card">
                        <h2>Agent 状态</h2>
                        <div id="agents"></div>
                    </div>
                    <div class="card">
                        <h2>任务看板</h2>
                        <div id="tasks"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Ledger Tab -->
        <div id="tab-ledger" class="hidden">
            <div class="card">
                <h2>Agent 执行账本</h2>
                <div style="margin-bottom:1rem">
                    <button onclick="loadLedger()">刷新</button>
                    <select id="ledger-status" onchange="loadLedger()">
                        <option value="">全部状态</option>
                        <option value="running">运行中</option>
                        <option value="completed">已完成</option>
                        <option value="failed">失败</option>
                        <option value="cancelled">已取消</option>
                    </select>
                </div>
                <div id="ledger-list"></div>
                <pre id="ledger-detail" class="hidden"></pre>
            </div>
        </div>

        <!-- Workspace Tab -->
        <div id="tab-workspace" class="hidden">
            <div class="grid">
                <div class="card">
                    <h2>频道</h2>
                    <ul id="ws-channels"></ul>
                </div>
                <div>
                    <div class="card">
                        <h2>Agent</h2>
                        <div id="ws-agents"></div>
                    </div>
                    <div class="card">
                        <h2>工作目录文件</h2>
                        <div id="ws-files"></div>
                        <pre id="ws-file-content" class="hidden" style="margin-top:1rem"></pre>
                    </div>
                </div>
            </div>
        </div>

        <!-- Health Tab -->
        <div id="tab-health" class="hidden">
            <div class="card">
                <h2>系统健康检查</h2>
                <button onclick="runDoctor()">运行检查</button>
                <pre id="health-output" style="margin-top:1rem">点击"运行检查"开始...</pre>
            </div>
        </div>
    </div>

    <script>
        let currentChannel = null;

        function showTab(name, el) {
            document.querySelectorAll('[id^="tab-"]').forEach(t => t.classList.add('hidden'));
            document.getElementById('tab-' + name).classList.remove('hidden');
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            el.classList.add('active');
            if (name === 'ledger') loadLedger();
            if (name === 'workspace') loadWsChannels();
        }

        // Dashboard
        async function loadChannels() {
            const res = await fetch('/api/channels');
            const channels = await res.json();
            document.getElementById('channels').innerHTML = channels.map(c =>
                `<li onclick="loadChannel('${c.id}')" id="ch-${c.id}">${c.id.slice(0,20)}...</li>`
            ).join('') || '<li>No channels</li>';
            if (channels.length) loadChannel(channels[0].id);
        }
        async function loadChannel(id) {
            currentChannel = id;
            document.querySelectorAll('#channels li').forEach(li => li.classList.remove('selected'));
            const el = document.getElementById('ch-' + id);
            if (el) el.classList.add('selected');
            const [agents, tasks] = await Promise.all([
                fetch(`/api/channels/${id}/agents`).then(r => r.json()),
                fetch(`/api/channels/${id}/tasks`).then(r => r.json()),
            ]);
            document.getElementById('agents').innerHTML = agents.map(a => `
                <div style="padding:0.5rem 0;border-bottom:1px solid #eee">
                    <strong>${a.display_name}</strong> <span class="status-${a.state}">${a.state}</span>
                    <span class="badge">${a.idle_timeout}s</span>
                    <button class="danger" style="float:right" onclick="cancelAgent('${id}','${a.id}')">取消</button>
                </div>`).join('') || '<p>No agents</p>';
            document.getElementById('tasks').innerHTML = tasks.map(t =>
                `<div class="task"><strong>#${t.id} ${t.title}</strong><span class="badge badge-${t.priority}">${t.priority}</span><span class="badge">${t.status}</span><br>@${t.assignee || 'unassigned'}</div>`
            ).join('') || '<p>No tasks</p>';
        }
        async function cancelAgent(ch, ag) {
            if (!confirm('取消 ' + ag + ' 的运行？')) return;
            const res = await fetch(`/api/channels/${ch}/agents/${ag}/cancel`, {method:'POST'});
            const data = await res.json();
            alert('已取消 ' + data.cancelled + ' 个运行');
        }

        // Ledger
        async function loadLedger() {
            const status = document.getElementById('ledger-status').value;
            let url = '/api/ledger?limit=50';
            if (currentChannel) url += '&channel_id=' + currentChannel;
            if (status) url += '&status=' + status;
            const res = await fetch(url);
            const entries = await res.json();
            document.getElementById('ledger-list').innerHTML = entries.map(e => `
                <div class="ledger-row" onclick="showLedgerDetail(${e.id})">
                    <strong>#${e.id}</strong> ${e.agent_id} <span class="status-${e.status}">${e.status}</span>
                    <span class="badge">${e.duration_ms || 0}ms</span>
                    <span class="badge">${(JSON.parse(e.tool_calls||'[]')).length} tools</span>
                    <br><small style="color:#6b7280">${(e.trigger_message||'').slice(0,80)}</small>
                </div>`).join('') || '<p>No entries</p>';
        }
        async function showLedgerDetail(id) {
            const res = await fetch(`/api/ledger/${id}`);
            const e = await res.json();
            const pre = document.getElementById('ledger-detail');
            pre.classList.remove('hidden');
            pre.textContent = JSON.stringify(e, null, 2);
        }

        // Workspace
        async function loadWsChannels() {
            const res = await fetch('/api/channels');
            const channels = await res.json();
            document.getElementById('ws-channels').innerHTML = channels.map(c =>
                `<li onclick="loadWsAgents('${c.id}')" id="wsch-${c.id}">${c.id.slice(0,20)}...</li>`
            ).join('') || '<li>No channels</li>';
            if (channels.length) loadWsAgents(channels[0].id);
        }
        async function loadWsAgents(id) {
            document.querySelectorAll('#ws-channels li').forEach(li => li.classList.remove('selected'));
            const el = document.getElementById('wsch-' + id);
            if (el) el.classList.add('selected');
            const res = await fetch(`/api/channels/${id}/agents`);
            const agents = await res.json();
            document.getElementById('ws-agents').innerHTML = agents.map(a =>
                `<div style="padding:0.5rem 0"><a href="#" onclick="loadWsFiles('${id}','${a.id}');return false">${a.display_name} (${a.id})</a></div>`
            ).join('');
        }
        async function loadWsFiles(ch, ag) {
            const res = await fetch(`/api/channels/${ch}/agents/${ag}/workspace`);
            const data = await res.json();
            const fc = document.getElementById('ws-file-content');
            fc.classList.add('hidden');
            document.getElementById('ws-files').innerHTML = (data.files||[]).map(f =>
                `<div class="file-item"><a href="#" onclick="loadFileContent('${ch}','${ag}','${f.name}');return false">${f.name}</a> <span class="badge">${(f.size/1024).toFixed(1)}KB</span></div>`
            ).join('') || '<p>No files</p>';
        }
        async function loadFileContent(ch, ag, name) {
            const res = await fetch(`/api/channels/${ch}/agents/${ag}/workspace/${name}`);
            const data = await res.json();
            const fc = document.getElementById('ws-file-content');
            fc.classList.remove('hidden');
            fc.textContent = data.content;
        }

        // Health
        async function runDoctor() {
            document.getElementById('health-output').textContent = '检查中...';
            const res = await fetch('/api/health');
            const data = await res.json();
            document.getElementById('health-output').textContent = data.output;
            showTab('health', document.querySelectorAll('.tab')[3]);
        }

        loadChannels();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)

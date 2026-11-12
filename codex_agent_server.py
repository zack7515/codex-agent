"""Codex Agent：用網頁操作 Codex coding agent（openai-codex Python SDK），接自架模型伺服器。

架構：
    瀏覽器（codex_agent.html） --HTTP/SSE--> 本程式（FastAPI） --openai-codex SDK--> codex app-server --Responses API--> Strata 等模型伺服器

安裝：
    pip install openai-codex fastapi uvicorn httpx

啟動：
    python codex_agent_server.py            # 開啟 http://127.0.0.1:8765（頁面是 codex_agent.html）
    python codex_agent_server.py --port 9000

注意：Codex 執行的指令與檔案操作都發生在「執行本程式的這台電腦」上，
      預設只綁定 127.0.0.1，請勿直接對外開放。

v2 變更（相對於原版）：
    - 每個 SSE 事件加上 t_ms（距送出 turn 的毫秒數），turn 結束時送出 agent/turnSummary
    - 每個 turn 的時間軸寫入 .agent/logs/YYYY-MM-DD.jsonl（中間產物），需要時才解析
    - 等待事件時每 15 秒送 SSE 心跳註解，避免長 prefill 期間連線被中途斷掉
    - 瀏覽器斷線時（預設）自動 interrupt turn，不讓 Codex 在背後繼續佔用模型伺服器
    - 修正 finally 區塊內 yield 的問題（產生器被關閉時不可再 yield）
    - 一次只允許一個進行中的 turn（Strata 是單一請求設計，同時送只會排隊）
    - /api/ping?deep=true 實際打一次 /responses，確認 Codex 真正會用到的端點
    - sandbox / approval / effort 參數不合法時回 400 並列出可用值，而非 500
    - 用 lifespan 取代已不建議使用的 on_event("shutdown")，connect/close 加鎖
    - approval_mode="ask_user"：要核准的指令／檔案變更、模型的 request_user_input 提問
      會以 agent/serverRequest 事件推到網頁，使用者回答後 POST /api/requests/{id} 回給 Codex

debug：頁面不再提供 log 查詢，直接查 .agent/logs/*.jsonl，例如
    grep -h error .agent/logs/*.jsonl | tail
    tail -n 5 .agent/logs/$(date +%F).jsonl | python -m json.tool --json-lines
"""

import argparse
import asyncio
import concurrent.futures
import contextlib
import enum
import json
import os
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import openai_codex._approval_mode as _sdk_approval
import openai_codex.api as _sdk_api
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox, SkillInput, TextInput
from openai_codex.generated.v2_all import (ApprovalsReviewer, AskForApproval, AskForApprovalValue,
                                           SkillsListResponse)

try:  # ReasoningEffort 在不同版本的匯出位置可能不同
    from openai_codex.generated.v2_all import ReasoningEffort as _Effort
except Exception:  # noqa: BLE001
    _Effort = None

HERE = Path(__file__).resolve().parent
# 中間產物一律先寫進 .agent，只有確定的內容才放根目錄
LOG_DIR = HERE / ".agent" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PROVIDER_ID = "agent"
KEY_ENV = "CODEX_AGENT_API_KEY"
HEARTBEAT_S = 15          # 多久沒有事件就送一次 SSE 心跳
LOG_PAYLOADS = os.environ.get("CODEX_AGENT_LOG_PAYLOADS") == "1"  # 設為 1 才把完整 payload 寫進 log
ASK_USER = "ask_user"     # 自訂的 approval 模式：需要核准時問網頁上的使用者
ASK_TIMEOUT_S = 600       # 使用者多久沒回答就自動拒絕（避免 Codex 永遠卡住）

# SDK 的 ApprovalMode 只有 deny_all / auto_review，沒有「交給使用者」。
# ponytail: 補一個 ask_user 對應到 on-request + reviewer=user；改的是 SDK 私有函式，升級 SDK 後要確認還在
_orig_approval_settings = _sdk_approval._approval_mode_settings


def _approval_settings(mode: Any):
    if mode == ASK_USER:
        return AskForApproval(root=AskForApprovalValue.on_request), ApprovalsReviewer.user
    return _orig_approval_settings(mode)


_sdk_approval._approval_mode_settings = _sdk_api._approval_mode_settings = _approval_settings

# 會推到網頁讓使用者決定的請求；其他（tool/call、登入 token 等）沿用 SDK 預設處理
ASK_METHODS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}   # 值是逾時、中斷或斷線時的預設回答（一律拒絕）


# --------------------------------------------------------------------------- 狀態


class LabState:
    def __init__(self) -> None:
        self.codex: AsyncCodex | None = None
        self.settings: dict[str, Any] = {}
        self.threads: dict[str, Any] = {}   # thread_id -> AsyncThread
        self.turns: dict[str, Any] = {}     # turn_id -> AsyncTurnHandle
        self.lock = asyncio.Lock()          # 保護 connect / close，避免重複按「啟動」造成競態
        self.turn_lock = asyncio.Lock()     # 檢查「有沒有進行中的 turn」與建立 turn 要一起做，避免連按兩次都通過
        self.http: httpx.AsyncClient | None = None
        self.conn: "ConnectionIn | None" = None   # 目前連線用的設定（含真正的 API key，不對外回傳）
        self.last_summary: dict[str, Any] | None = None
        self.bg_tasks: set[asyncio.Task] = set()  # 背景 interrupt 任務，保留參照避免被 GC
        self.queues: dict[str, asyncio.Queue] = {}  # turn_id -> 該 turn 的 SSE 佇列（server request 要推進去）
        self.pending: dict[str, dict[str, Any]] = {}  # request id -> {method, params, future}

    def resolve_pending(self, turn_id: str | None = None) -> None:
        """用預設回答（拒絕）結束等待中的請求。中斷前一定要先做：
        SDK 在讀取執行緒裡同步等待回答，不先放掉，interrupt 的回應也讀不到。"""
        for req in list(self.pending.values()):
            if turn_id is None or req["params"].get("turnId") == turn_id:
                if not req["future"].done():
                    req["future"].set_result(ASK_METHODS[req["method"]])

    async def close(self) -> None:
        self.resolve_pending()
        for handle in list(self.turns.values()):
            with contextlib.suppress(Exception):
                await handle.interrupt()
        if self.codex is not None:
            with contextlib.suppress(Exception):
                await self.codex.close()
        self.codex = None
        self.threads.clear()
        self.turns.clear()


state = LabState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.http = httpx.AsyncClient()
    try:
        yield
    finally:
        async with state.lock:
            await state.close()
        await state.http.aclose()


app = FastAPI(title="Codex Agent", lifespan=lifespan)


# --------------------------------------------------------------------------- 核准 / 提問


def announce(rid: str) -> None:
    """（在 event loop 上執行）把等待中的請求推給對應 turn 的網頁。"""
    req = state.pending.get(rid)
    if req is None or req.get("announced"):
        return
    queue = state.queues.get(req["params"].get("turnId", "")) or next(iter(state.queues.values()), None)
    if queue is None:
        return   
    req["announced"] = True
    queue.put_nowait({"method": "agent/serverRequest",
                      "payload": {"requestId": rid, "method": req["method"], "params": req["params"]}})


def make_request_handler(loop: asyncio.AbstractEventLoop, fallback):
    """SDK 會在它的讀取執行緒裡「同步」呼叫這個函式，回傳值就是給 Codex 的回答。"""

    def handler(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if method not in ASK_METHODS:
            return fallback(method, params)
        rid = uuid.uuid4().hex[:12]
        fut: concurrent.futures.Future = concurrent.futures.Future()
        state.pending[rid] = {"method": method, "params": params or {}, "future": fut}
        loop.call_soon_threadsafe(announce, rid)
        try:
            return fut.result(timeout=ASK_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            return ASK_METHODS[method]
        finally:
            state.pending.pop(rid, None)

    return handler


def require_codex() -> AsyncCodex:
    if state.codex is None:
        raise HTTPException(400, "Codex 尚未啟動：請先在「1. 連線」按「啟動 Codex」")
    return state.codex


def require_thread(thread_id: str):
    thread = state.threads.get(thread_id)
    if thread is None:
        raise HTTPException(404, f"找不到 thread {thread_id}：請先建立或接續")
    return thread


# --------------------------------------------------------------------------- 工具函式


def toml_str(value: str) -> str:
    """JSON 字串語法與 TOML basic string 相容，用來安全地加上引號與跳脫。"""
    return json.dumps(value, ensure_ascii=False)


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def event_to_dict(event: Any) -> dict[str, Any]:
    method = getattr(event, "method", "unknown")
    payload = getattr(event, "payload", None)
    if payload is None or payload.__class__.__name__ == "UnknownNotification":
        payload = getattr(payload, "params", payload)
    return {"method": method, "payload": to_jsonable(payload)}


def parse_choice(cls: Any, value: str | None, field: str):
    """把字串轉成 SDK 的列舉；不合法時回 400 並列出可用值，而不是 500。"""
    if not value:
        return None
    try:
        return cls(value)
    except (ValueError, TypeError):
        allowed: list[str] = []
        with contextlib.suppress(Exception):
            allowed = [str(getattr(m, "value", m)) for m in cls]
        raise HTTPException(400, f"{field} 不合法：{value!r}；可用值：{allowed or '請查 SDK 文件'}")


def parse_effort(value: str | None):
    if not value:
        return None
    return parse_choice(_Effort, value, "effort") if _Effort is not None else value


def error_response(exc: Exception) -> JSONResponse:
    traceback.print_exc()
    return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- 計時記錄


class TurnRecorder:
    """記錄單一 turn 的事件時間軸，供研究分析（首個事件延遲、各類事件數、總時長）。"""

    def __init__(self, thread_id: str, turn_id: str, text: str, t_send: float) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.text = text
        self.t_send = t_send                       # 呼叫 thread.turn() 前的時間
        self.t_handle = time.perf_counter()        # 拿到 turn handle 的時間
        self.first_event_ms: float | None = None   # 第一個 Codex 事件（非 agent/*）
        self.first_delta_ms: float | None = None   # 第一個方法名含 delta 的事件（約略等於首 token）
        self.counts: dict[str, int] = {}
        self.timeline: list[dict[str, Any]] = []
        self.ended_by = "completed"

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.t_send) * 1000, 1)

    def stamp(self, item: dict[str, Any]) -> dict[str, Any]:
        t = self.elapsed_ms()
        method = item.get("method", "unknown")
        if not method.startswith("agent/"):
            self.counts[method] = self.counts.get(method, 0) + 1
            if self.first_event_ms is None:
                self.first_event_ms = t
            if self.first_delta_ms is None and "delta" in method.lower():
                self.first_delta_ms = t
            entry: dict[str, Any] = {"t_ms": t, "method": method}
            if LOG_PAYLOADS:
                entry["payload"] = item.get("payload")
            self.timeline.append(entry)
        return {**item, "t_ms": t}

    def summary(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "turnId": self.turn_id,
            "endedBy": self.ended_by,
            "handleMs": round((self.t_handle - self.t_send) * 1000, 1),
            "firstEventMs": self.first_event_ms,
            "firstDeltaMs": self.first_delta_ms,
            "totalMs": self.elapsed_ms(),
            "eventCounts": self.counts,
            "model": state.settings.get("model"),
        }

    def write(self) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "prompt": self.text[:500],
            "summary": self.summary(),
            "timeline": self.timeline,
        }
        path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
        with contextlib.suppress(Exception):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 請求格式


class ConnectionIn(BaseModel):
    base_url: str = Field(..., examples=["http://192.168.31.120:8080/v1"])
    api_key: str = ""
    model: str = "strata"
    context_window: int = 262144
    wire_api: str = "responses"
    stream_idle_timeout_ms: int = 600_000
    windows_unelevated: bool = sys.platform == "win32"


class ThreadIn(BaseModel):
    cwd: str = ""
    sandbox: str = "workspace-write"
    approval_mode: str = ASK_USER
    developer_instructions: str = ""
    ephemeral: bool = False


class SkillRef(BaseModel):
    name: str
    path: str


class TurnIn(BaseModel):
    text: str
    skills: list[SkillRef] = []   # 用 / 選單指定的 skills，明確交給 Codex，不靠模型自己判斷
    effort: str = ""
    sandbox: str = ""
    output_schema: dict[str, Any] | None = None


class TextIn(BaseModel):
    text: str


# --------------------------------------------------------------------------- 頁面


@app.get("/")
async def index() -> FileResponse:
    # no-store：改了 html 之後重新整理就會拿到新版，不會被瀏覽器快取住
    return FileResponse(HERE / "codex_agent.html", headers={"Cache-Control": "no-store"})



# --------------------------------------------------------------------------- 1. 連線


def auth_headers(conn: ConnectionIn) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key}"} if conn.api_key else {}


async def probe_models(conn: ConnectionIn, timeout: float) -> dict[str, Any]:
    """打一次模型伺服器的 /models：確認網路、port 與 API key。"""
    url = conn.base_url.rstrip("/") + "/models"
    try:
        t0 = time.perf_counter()
        r = await state.http.get(url, headers=auth_headers(conn), timeout=timeout)
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text[:2000]
        return {"ok": r.is_success, "status": r.status_code, "url": url,
                "ms": round((time.perf_counter() - t0) * 1000, 1), "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "url": url, "body": f"{type(exc).__name__}: {exc}"}


@app.post("/api/ping")
async def ping(conn: ConnectionIn, deep: bool = False):
    """不經過 Codex 直接測模型伺服器。

    預設只打 /models（確認網路與 API key）；deep=true 時再打一次 /responses，
    確認 Codex 實際使用的端點可用，並量出一次極短生成的延遲。
    """
    base = conn.base_url.rstrip("/")
    headers = auth_headers(conn)
    result: dict[str, Any] = {"models": await probe_models(conn, timeout=10)}

    if deep:
        url = base + "/responses"
        payload = {"model": conn.model, "input": "Reply with OK.", "max_output_tokens": 16, "stream": False}
        try:
            t0 = time.perf_counter()
            r = await state.http.post(url, headers=headers, json=payload, timeout=180)
            try:
                body = r.json()
            except ValueError:
                body = r.text[:2000]
            result["responses"] = {"ok": r.is_success, "status": r.status_code, "url": url,
                                   "ms": round((time.perf_counter() - t0) * 1000, 1), "body": body}
        except Exception as exc:  # noqa: BLE001
            result["responses"] = {"ok": False, "status": None, "url": url, "body": f"{type(exc).__name__}: {exc}"}

    result["ok"] = all(v.get("ok") for v in result.values() if isinstance(v, dict))
    return result


@app.post("/api/connect")
async def connect(conn: ConnectionIn):
    """啟動（或重新啟動）codex app-server，並指向自訂模型伺服器。"""
    overrides = [
        f"model_provider={toml_str(PROVIDER_ID)}",
        f"model={toml_str(conn.model)}",
        f"model_context_window={int(conn.context_window)}",
        "show_raw_agent_reasoning=true",
        "features.default_mode_request_user_input=true",   # 讓模型可以丟出選項請使用者選
        f"model_providers.{PROVIDER_ID}.name={toml_str('Codex Agent provider')}",
        f"model_providers.{PROVIDER_ID}.base_url={toml_str(conn.base_url.rstrip('/'))}",
        f"model_providers.{PROVIDER_ID}.wire_api={toml_str(conn.wire_api)}",
        f"model_providers.{PROVIDER_ID}.stream_idle_timeout_ms={int(conn.stream_idle_timeout_ms)}",
    ]
    env: dict[str, str] = {}
    if conn.api_key:
        # 只有真的有 key 才設定 env_key；空值會造成「Missing environment variable: ``」
        overrides.append(f"model_providers.{PROVIDER_ID}.env_key={toml_str(KEY_ENV)}")
        env[KEY_ENV] = conn.api_key
    if conn.windows_unelevated:
        overrides.append('windows.sandbox="unelevated"')

    async with state.lock:
        await state.close()
        codex = AsyncCodex(CodexConfig(config_overrides=tuple(overrides), env=env or None,
                                       client_name="codex_agent", client_title="Codex Agent"))
        # ponytail: AsyncCodex 沒開放 approval_handler 參數，只能設在內部的同步 client 上
        sync_client = codex._client._sync
        sync_client._approval_handler = make_request_handler(asyncio.get_running_loop(),
                                                             sync_client._default_approval_handler)
        try:
            await codex.__aenter__()
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await codex.close()
            return error_response(exc)
        state.codex = codex
        state.conn = conn
        state.settings = {**conn.model_dump(), "api_key": "***" if conn.api_key else ""}
        return {"ok": True, "metadata": to_jsonable(codex.metadata), "overrides": overrides}


@app.post("/api/disconnect")
async def disconnect():
    async with state.lock:
        await state.close()
    return {"ok": True}


@app.get("/api/health")
async def health():
    """右上角狀態燈用：Codex 有沒有啟動，以及模型伺服器實際連不連得到。
    app-server 啟動成功不代表模型伺服器活著（它要到送 turn 時才會去連）。"""
    if state.codex is None or state.conn is None:
        return {"connected": False, "model_ok": False}
    r = await probe_models(state.conn, timeout=5)
    detail = "" if r["ok"] else (f"HTTP {r['status']}" if r["status"] else str(r["body"])[:200])
    return {"connected": True, "model_ok": r["ok"], "url": r["url"], "detail": detail}


@app.get("/api/status")
async def status():
    return {
        "connected": state.codex is not None,
        "settings": state.settings,
        "threads": list(state.threads),
        "active_turns": list(state.turns),
        "last_turn_summary": state.last_summary,
    }


# --------------------------------------------------------------------------- 2. Thread


def thread_kwargs(t: ThreadIn) -> dict[str, Any]:
    return {
        "cwd": t.cwd or None,
        "sandbox": parse_choice(Sandbox, t.sandbox, "sandbox"),
        "approval_mode": ASK_USER if t.approval_mode == ASK_USER
                         else parse_choice(ApprovalMode, t.approval_mode, "approval_mode") or ApprovalMode.deny_all,
        "developer_instructions": t.developer_instructions or None,
        "model": state.settings.get("model"),
        "model_provider": PROVIDER_ID,
    }


@app.post("/api/threads")
async def thread_start(t: ThreadIn):
    codex = require_codex()
    kwargs = thread_kwargs(t)
    try:
        thread = await codex.thread_start(**kwargs, ephemeral=t.ephemeral or None)
        state.threads[thread.id] = thread
        return {"ok": True, "thread_id": thread.id}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


@app.post("/api/threads/{thread_id}/resume")
async def thread_resume(thread_id: str, t: ThreadIn):
    codex = require_codex()
    kwargs = thread_kwargs(t)
    try:
        thread = await codex.thread_resume(thread_id, **kwargs)
        state.threads[thread.id] = thread
        return {"ok": True, "thread_id": thread.id}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


@app.post("/api/threads/{thread_id}/compact")
async def thread_compact(thread_id: str):
    thread = require_thread(thread_id)
    try:
        return {"ok": True, "result": to_jsonable(await thread.compact())}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


@app.post("/api/threads/{thread_id}/name")
async def thread_name(thread_id: str, body: TextIn):
    """改標題：寫進 Codex 自己存的 name，失敗時頁面會退回本地標題。"""
    thread = require_thread(thread_id)
    try:
        return {"ok": True, "result": to_jsonable(await thread.set_name(body.text))}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


@app.post("/api/threads/{thread_id}/archive")
async def thread_archive(thread_id: str):
    codex = require_codex()
    try:
        return {"ok": True, "result": to_jsonable(await codex.thread_archive(thread_id))}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


def history_from_disk(thread_id: str) -> list[dict[str, Any]]:
    """讀 Codex 自己寫的紀錄檔（~/.codex/sessions）：app-server 沒 hydrate 歷史時用。"""
    root = Path(os.path.expanduser("~")) / ".codex" / "sessions"
    for path in sorted(root.glob(f"**/*{thread_id}*.jsonl")):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        msgs: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            text = "".join(c.get("text", "") for c in (payload.get("content") or []) if isinstance(c, dict)).strip()
            if not text:
                continue
            # 系統的注入（developer / environment_context）不列進來，不然歷史會滿滿都是系統內容
            if role == "developer" or text.startswith("<environment_context>"):
                continue
            msgs.append({"role": role, "text": text[:4000], "at": obj.get("timestamp")})
        return msgs
    return []


@app.get("/api/threads/{thread_id}/history")
async def thread_history(thread_id: str):
    """讀一輪對話的紀錄：先試 app-server，沒東西就讀 Codex 的紀錄檔。"""
    items: list[dict[str, Any]] = []
    source = "app_server"
    thread = state.threads.get(thread_id)
    if thread is not None:
        try:
            resp = await thread.read(include_turns=True)
            for turn in (getattr(resp, "thread", None).turns or []):
                for item in (turn.items or []):
                    items.append(to_jsonable(item))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    if not items:
        items = history_from_disk(thread_id)
        source = "codex_session_file" if items else "none"
    return {"ok": True, "thread_id": thread_id, "source": source, "items": items}


@app.get("/api/skills")
async def skills_list(cwd: str = "", reload: bool = False):
    """列出 Codex 載入的 skills（~/.codex/skills、專案裡的 skills 等），給網頁的 / 選單用。"""
    codex = require_codex()
    params: dict[str, Any] = {"cwds": [cwd] if cwd else [], "forceReload": reload}
    try:
        # ponytail: SDK 沒包裝 skills/list，借用內部 client 的通用 request；升級 SDK 後要確認還在
        resp = await codex._client.request("skills/list", params, response_model=SkillsListResponse)
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)
    skills, errors, seen = [], [], set()
    for entry in resp.data:
        errors += [to_jsonable(e) for e in entry.errors]
        for sk in entry.skills:
            if not sk.enabled or sk.name in seen:
                continue
            seen.add(sk.name)
            skills.append({"name": sk.name, "path": str(to_jsonable(sk.path)),
                           "description": sk.short_description or sk.description,
                           "scope": to_jsonable(sk.scope)})
    return {"ok": True, "skills": sorted(skills, key=lambda x: x["name"]), "errors": errors}


@app.get("/api/threads")
async def thread_list():
    codex = require_codex()
    try:
        return {"ok": True, "threads": to_jsonable(await codex.thread_list(limit=20))}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


# --------------------------------------------------------------------------- 3. Turn（串流）


async def _interrupt_quietly(handle: Any) -> None:
    with contextlib.suppress(Exception):
        await handle.interrupt()


@app.post("/api/threads/{thread_id}/turn")
async def turn_start(thread_id: str, body: TurnIn):
    thread = require_thread(thread_id)
    effort = parse_effort(body.effort)
    sandbox = parse_choice(Sandbox, body.sandbox, "sandbox")

    async with state.turn_lock:
        if state.turns:
            raise HTTPException(409, f"已有進行中的 turn：{list(state.turns)}。"
                                     "模型伺服器一次只處理一個請求，請等它結束或按「中斷」")
        t_send = time.perf_counter()
        try:
            items = [TextInput(body.text), *(SkillInput(name=k.name, path=k.path) for k in body.skills)]
            handle = await thread.turn(items if body.skills else body.text,
                                       effort=effort, sandbox=sandbox, output_schema=body.output_schema)
        except Exception as exc:  # noqa: BLE001
            return error_response(exc)
        state.turns[handle.id] = handle
    rec = TurnRecorder(thread_id, handle.id, body.text, t_send)

    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    state.queues[handle.id] = queue
    for rid in list(state.pending):   # turn 建好前就到的請求，在這裡補推
        announce(rid)

    async def pump() -> None:
        """在背景讀 Codex 事件放進佇列，讓前景能在等待期間送心跳。

        登記的清除與寫 log 放在這裡而不是 events()：就算瀏覽器在串流開始前就斷線
        （events() 根本沒跑），turn 結束後也一定會從 state.turns 移除，不會卡住之後的 turn。
        """
        try:
            async for event in handle.stream():
                await queue.put(event_to_dict(event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            rec.ended_by = "error"
            await queue.put({"method": "agent/error", "payload": {"message": f"{type(exc).__name__}: {exc}"}})
        finally:
            state.resolve_pending(handle.id)
            state.turns.pop(handle.id, None)
            state.queues.pop(handle.id, None)
            state.last_summary = rec.summary()
            rec.write()
            queue.put_nowait(done)

    task = asyncio.create_task(pump())
    state.bg_tasks.add(task)
    task.add_done_callback(state.bg_tasks.discard)

    async def events() -> AsyncIterator[str]:
        finished = False
        try:
            yield sse(rec.stamp({"method": "agent/turnHandle",
                                 "payload": {"turnId": handle.id, "threadId": thread_id}}))
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield f": keepalive {rec.elapsed_ms()}ms\n\n"   # SSE 註解行，前端會忽略
                    continue
                if item is done:
                    finished = True
                    break
                yield sse(rec.stamp(item))
            yield sse({"method": "agent/turnSummary", "payload": rec.summary()})
            yield sse({"method": "agent/streamClosed", "payload": {"turnId": handle.id}})
        finally:
            # 注意：這裡不能再 yield（產生器可能正在被關閉）
            if not finished:
                # 瀏覽器斷線：中止 turn，不讓 Codex 在背後繼續佔用模型伺服器
                rec.ended_by = "client_disconnected"
                state.resolve_pending(handle.id)
                bg = asyncio.create_task(_interrupt_quietly(handle))
                state.bg_tasks.add(bg)
                bg.add_done_callback(state.bg_tasks.discard)
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/turns/{turn_id}/steer")
async def turn_steer(turn_id: str, body: TextIn):
    handle = state.turns.get(turn_id)
    if handle is None:
        raise HTTPException(404, "這個 turn 已結束或不存在")
    try:
        return {"ok": True, "result": to_jsonable(await handle.steer(body.text))}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


@app.post("/api/turns/{turn_id}/interrupt")
async def turn_interrupt(turn_id: str):
    handle = state.turns.get(turn_id)
    if handle is None:
        raise HTTPException(404, "這個 turn 已結束或不存在")
    state.resolve_pending(turn_id)
    try:
        return {"ok": True, "result": to_jsonable(await handle.interrupt())}
    except Exception as exc:  # noqa: BLE001
        return error_response(exc)


class AnswerIn(BaseModel):
    result: dict[str, Any]   # 直接回給 Codex 的內容，格式依請求種類（decision / answers / permissions）


@app.post("/api/requests/{request_id}")
async def answer_request(request_id: str, body: AnswerIn):
    req = state.pending.get(request_id)
    if req is None or req["future"].done():
        raise HTTPException(404, "這個請求已經回答、逾時或已取消")
    req["future"].set_result(body.result)
    return {"ok": True}


# --------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex Agent：網頁版 Codex coding agent")
    ap.add_argument("--host", default="127.0.0.1", help="預設只允許本機連線")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost"):
        print("警告：Codex 能在這台電腦執行指令，對外開放前請自行加上驗證。", file=sys.stderr)
    print(f"Codex Agent: http://{a.host}:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()

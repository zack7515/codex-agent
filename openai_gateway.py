"""OpenAI 相容轉接層：讓現成的 chat UI 用 /v1/chat/completions 使用 Codex Agent。

只做格式轉換，實際工作交給 codex_agent_server.py 的 /api/*（不另外開 Codex，排隊與鎖共用）。

    對方的 chat UI ──/v1/chat/completions + token──▶ openai_gateway.py ──/api/*──▶ codex_agent_server.py

對話對應：OpenAI 格式每次都送完整歷史，Codex 則自己記對話。每回合結束後把
「完整歷史 + 這次回覆」的雜湊值對應到 Codex thread；下次送來的歷史（扣掉最後一句）對得上，
就沿用那個 thread 只送最後一句；對不上（新對話、使用者改了舊訊息、伺服器重啟）就開新 thread，
並把先前的對話當參考文字一起送。

環境變數：
    CODEX_GATEWAY_TOKEN    對方要帶的 Bearer token（對外開放時必填）
    CODEX_AGENT_URL        codex_agent_server.py 位置，預設 http://127.0.0.1:8765
    CODEX_GATEWAY_CWD      Codex 的工作資料夾，預設是 codex_agent_server.py 的啟動位置
    CODEX_GATEWAY_SANDBOX  read-only / workspace-write（預設）/ full-access
    LLM_BASE_URL、LLM_MODEL、LLM_API_KEY
                           有設的話，Codex 還沒啟動時 gateway 會自己連線；沒設就要先在網頁按連線
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

AGENT_URL = os.environ.get("CODEX_AGENT_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN", "")
MODEL_ID = "codex-agent"
THREAD_SETTINGS = {
    "cwd": os.environ.get("CODEX_GATEWAY_CWD", ""),
    "sandbox": os.environ.get("CODEX_GATEWAY_SANDBOX", "workspace-write"),
    "approval_mode": "auto_review",   # 對方的 UI 不會處理核准卡片，交給 Codex 自己審
}
LLM = {k: v for k, v in {"base_url": os.environ.get("LLM_BASE_URL"),
                         "model": os.environ.get("LLM_MODEL"),
                         "api_key": os.environ.get("LLM_API_KEY")}.items() if v}

# ponytail: 對應表只放記憶體、上限 1000 筆，gateway 重啟後舊對話會改開新 thread（附上歷史文字），要持久化再存檔
threads: OrderedDict[str, str] = OrderedDict()
agent = httpx.AsyncClient(base_url=AGENT_URL, timeout=httpx.Timeout(30, read=None))

app = FastAPI(title="Codex Agent OpenAI Gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def auth(request: Request) -> None:
    if TOKEN and not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
        raise HTTPException(401, "token 錯誤或沒帶")


def text_of(content: Any) -> str:
    if isinstance(content, list):   # 多段格式 [{"type":"text","text":...}]，圖片等其他段落忽略
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def history_key(msgs: list[dict]) -> str:
    norm = [[m["role"], text_of(m.get("content")).strip()] for m in msgs]
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode()).hexdigest()


def remember(key: str, thread_id: str) -> None:
    threads[key] = thread_id
    while len(threads) > 1000:
        threads.popitem(last=False)


def with_history(convo: list[dict], text: str) -> str:
    if not convo:
        return text
    past = "\n\n".join(f"[{m['role']}]\n{text_of(m.get('content'))}" for m in convo)
    return f"以下是先前的對話紀錄，供你參考：\n\n{past}\n\n---\n\n{text}"


async def new_thread(instructions: str) -> str:
    body = {**THREAD_SETTINGS, "developer_instructions": instructions}
    r = await agent.post("/api/threads", json=body)
    if r.status_code == 400 and LLM:   # Codex 還沒啟動
        c = await agent.post("/api/connect", json=LLM)
        if not c.json().get("ok"):
            raise HTTPException(503, f"Codex 啟動失敗：{c.json().get('error')}")
        r = await agent.post("/api/threads", json=body)
    if r.status_code == 400:
        raise HTTPException(503, "Codex 尚未啟動：請先在 Codex Agent 網頁連線，或設定 LLM_BASE_URL")
    data = r.json()
    if not data.get("ok"):
        raise HTTPException(502, f"開對話失敗：{data.get('error')}")
    return data["thread_id"]


async def open_turn(thread_id: str, text: str, effort: str) -> httpx.Response | None:
    """回傳串流中的回應；thread 不存在（例如伺服器重啟過）回 None。"""
    req = agent.build_request("POST", f"/api/threads/{thread_id}/turn", json={"text": text, "effort": effort})
    resp = await agent.send(req, stream=True)
    if resp.status_code == 200:
        return resp
    await resp.aread()
    await resp.aclose()
    if resp.status_code == 404:
        return None
    if resp.status_code == 409:
        raise HTTPException(429, "Codex 正在處理別的請求，請稍後再試")
    raise HTTPException(502, f"Codex Agent 回 {resp.status_code}：{resp.text[:300]}")


async def replies(resp: httpx.Response) -> AsyncIterator[str]:
    """把 Codex 事件轉成回覆文字；空字串代表心跳（Codex 在跑指令、還沒有文字）。"""
    seen: set = set()
    last = None

    def piece(item_id: Any, text: str) -> str:
        nonlocal last
        sep = "\n\n" if last is not None and item_id != last else ""   # 不同段回覆之間空一行
        last = item_id
        seen.add(item_id)
        return sep + text

    try:
        async for line in resp.aiter_lines():
            if line.startswith(":"):
                yield ""
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            method, p = ev.get("method"), ev.get("payload") or {}
            item = p.get("item") or {}
            if method == "item/agentMessage/delta":
                yield piece(p.get("itemId"), p.get("delta", ""))
            elif method == "item/completed" and item.get("type") == "agentMessage" and item.get("id") not in seen:
                yield piece(item.get("id"), item.get("text", ""))   # 模型沒串流時只會有完成事件
            elif method == "agent/error":
                yield f"\n\n[錯誤] {p.get('message')}"
            elif method == "turn/completed" and (p.get("turn") or {}).get("error"):
                yield f"\n\n[錯誤] {json.dumps(p['turn']['error'], ensure_ascii=False)}"
    finally:
        await resp.aclose()   # 對方斷線時關掉上游，codex_agent_server 會中止這個回合


@app.get("/v1/models", dependencies=[Depends(auth)])
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "codex-agent"}]}


@app.post("/v1/chat/completions", dependencies=[Depends(auth)])
async def chat(request: Request):
    body = await request.json()
    msgs = [m for m in body.get("messages", []) if m.get("role") in ("system", "developer", "user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "messages 最後一則要是 user")
    text = text_of(msgs[-1].get("content"))
    effort = body.get("reasoning_effort") or ""

    thread_id = threads.get(history_key(msgs[:-1]))
    resp = await open_turn(thread_id, text, effort) if thread_id else None
    if resp is None:
        instructions = "\n\n".join(text_of(m.get("content")) for m in msgs if m["role"] in ("system", "developer"))
        convo = [m for m in msgs[:-1] if m["role"] in ("user", "assistant")]
        thread_id = await new_thread(instructions)
        resp = await open_turn(thread_id, with_history(convo, text), effort)
        if resp is None:
            raise HTTPException(502, "剛開的對話找不到")

    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    base = {"id": cid, "created": created, "model": MODEL_ID}

    def done(reply: str) -> None:
        remember(history_key(msgs + [{"role": "assistant", "content": reply}]), thread_id)

    if not body.get("stream"):
        reply = "".join([t async for t in replies(resp)])
        done(reply)
        return {**base, "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def chunk(delta: dict, finish: str | None = None) -> str:
        data = {**base, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def stream() -> AsyncIterator[str]:
        parts: list[str] = []
        yield chunk({"role": "assistant", "content": ""})
        async for t in replies(resp):
            if not t:
                yield ": keepalive\n\n"
                continue
            parts.append(t)
            yield chunk({"content": t})
        done("".join(parts))
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex Agent 的 OpenAI 相容轉接層")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and not TOKEN:
        sys.exit("對外開放（--host 不是本機）一定要設 CODEX_GATEWAY_TOKEN，否則任何人都能在這台電腦執行指令")
    print(f"OpenAI gateway: http://{a.host}:{a.port}/v1  →  {AGENT_URL}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()

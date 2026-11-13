"""OpenAI 相容轉接層：讓現成的 chat UI 用 /v1/chat/completions 使用 Codex Agent。

只做格式轉換，實際工作交給 codex_agent_server.py 的 /api/*（不另外開 Codex）。

    對方的 chat UI ──/v1/chat/completions + token──▶ openai_gateway.py ──/api/*──▶ codex_agent_server.py

對話對應：OpenAI 格式每次都送完整歷史，Codex 則自己記對話。每回合結束後把
「完整歷史 + 這次回覆」的雜湊值對應到 Codex thread；下次送來的歷史（扣掉最後一句）對得上，
就沿用那個 thread 只送最後一句；對不上（新對話、使用者改了舊訊息、gateway 重啟）就開新 thread，
並把先前的對話當參考文字一起送。Codex 重啟過、後端不認得那個 thread 時，會先試著接續（resume）保留完整上下文。

排隊：後端一次只跑一個回合。請求依先來後到排隊，排到最前面、後端也有空才開回合；
等候期間串流請求會收到心跳。等超過 --queue-wait 秒（非串流最多 60 秒）才回 429。

核准／提問：對方的 UI 顯示不了卡片，Codex 丟出來的核准或提問一律立刻拒絕，不讓整個服務卡到逾時。

Skills：訊息只打 /skills 會列出可用的 skills（不經過模型）；訊息裡的 $名稱 會明確指定 Codex 使用該 skill。

設定（命令列參數優先，沒給才看環境變數）：
    --users     CODEX_GATEWAY_USERS    多人使用：名單檔（JSON，{"使用者": "token"}），每人用自己的 token，
                                       工作資料夾自動分到 CODEX_GATEWAY_CWD/<使用者>/，對話也互不相通
    --token     CODEX_GATEWAY_TOKEN    只有一組 token 時用這個（沒設 USERS 時才看）；對外開放時兩者至少設一個
    --agent-url CODEX_AGENT_URL        codex_agent_server.py 位置，預設 http://127.0.0.1:8765
    --cwd       CODEX_GATEWAY_CWD      Codex 的工作資料夾；多人模式是各人資料夾的上層，預設 ./workspaces
                                       單一 token 時預設是 codex_agent_server.py 的啟動位置
    --sandbox   CODEX_GATEWAY_SANDBOX  read-only / workspace-write（預設）/ full-access
    --queue-wait CODEX_GATEWAY_QUEUE_WAIT  忙碌時最多排隊幾秒，預設 300（非串流請求最多 60）
    --llm-url / --llm-model / --llm-key   LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
                                       有設的話，Codex 還沒啟動時 gateway 會自己連線；沒設就要先在網頁按連線
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

MODEL_ID = "codex-agent"
SANDBOXES = ("read-only", "workspace-write", "full-access")
NONSTREAM_WAIT_S = 60   # 非串流請求收不到心跳，排太久對方會先逾時，所以排隊上限比較短
POLL_S = 0.5            # 排隊時多久看一次輪到了沒
OPTIONS = {   # 命令列參數 → 對應的環境變數
    "--users": "CODEX_GATEWAY_USERS", "--token": "CODEX_GATEWAY_TOKEN", "--cwd": "CODEX_GATEWAY_CWD",
    "--sandbox": "CODEX_GATEWAY_SANDBOX", "--agent-url": "CODEX_AGENT_URL", "--queue-wait": "CODEX_GATEWAY_QUEUE_WAIT",
    "--llm-url": "LLM_BASE_URL", "--llm-model": "LLM_MODEL", "--llm-key": "LLM_API_KEY",
}
# 對方的 UI 顯示不了核准／提問卡片：收到就用這些回答立刻拒絕（內容跟後端逾時的預設回答一樣）
DECLINE = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}


def load_users() -> tuple[dict[str, str], Path | None]:
    """回傳 ({使用者: token}, 各人資料夾的上層)；單一 token 模式上層是 None（不分資料夾）。"""
    path = os.environ.get("CODEX_GATEWAY_USERS")
    if not path:
        token = os.environ.get("CODEX_GATEWAY_TOKEN", "")
        return ({"default": token} if token else {}), None
    users = json.loads(Path(path).read_text(encoding="utf-8"))
    for name, token in users.items():
        # 名稱會變成資料夾名，只允許安全字元，避免 ../ 之類跳出上層
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(token, str) or len(token) < 16:
            sys.exit(f"{path}：使用者 {name!r} 名稱只能用英數字 _ -，token 至少 16 字")
    if len(set(users.values())) != len(users):
        sys.exit(f"{path}：每個人的 token 要不一樣")
    root = Path(os.environ.get("CODEX_GATEWAY_CWD") or Path(__file__).with_name("workspaces"))
    return users, root.resolve()


def configure() -> None:
    """從環境變數讀設定；main() 會先把命令列參數寫進環境變數再呼叫一次。"""
    global AGENT_URL, THREAD_SETTINGS, USERS, WORKSPACE_ROOT, LLM, QUEUE_WAIT_S, agent
    AGENT_URL = os.environ.get("CODEX_AGENT_URL", "http://127.0.0.1:8765")
    THREAD_SETTINGS = {
        "cwd": os.environ.get("CODEX_GATEWAY_CWD", ""),
        "sandbox": os.environ.get("CODEX_GATEWAY_SANDBOX", "workspace-write"),
        "approval_mode": "auto_review",   # 對方的 UI 不會處理核准卡片，交給 Codex 自己審
    }
    if THREAD_SETTINGS["sandbox"] not in SANDBOXES:   # 啟動時就擋下來，不要等到每個請求都失敗
        sys.exit(f"sandbox 不合法：{THREAD_SETTINGS['sandbox']!r}；可用值：{'、'.join(SANDBOXES)}")
    QUEUE_WAIT_S = float(os.environ.get("CODEX_GATEWAY_QUEUE_WAIT") or 300)
    USERS, WORKSPACE_ROOT = load_users()
    LLM = {k: v for k, v in {"base_url": os.environ.get("LLM_BASE_URL"),
                             "model": os.environ.get("LLM_MODEL"),
                             "api_key": os.environ.get("LLM_API_KEY")}.items() if v}
    agent = httpx.AsyncClient(base_url=AGENT_URL, timeout=30)   # 只有回合串流不限讀取時間，見 open_turn


configure()

# ponytail: 對應表只放記憶體、上限 1000 筆，gateway 重啟後舊對話會改開新 thread（附上歷史文字），要持久化再存檔
threads: OrderedDict[str, str] = OrderedDict()
waiting: deque[object] = deque()   # 排隊中的請求；最前面的才能開回合，跑完（或放棄）才離開
connect_lock = asyncio.Lock()      # 同時進來的請求只啟動 Codex 一次


class Busy(Exception):
    """後端正在跑別的回合（例如網頁那邊），等一下再試。"""

app = FastAPI(title="Codex Agent OpenAI Gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def auth(request: Request) -> str:
    """回傳使用者名稱；沒設任何 token（只限本機）時是 default。"""
    if not USERS:
        return "default"
    got = request.headers.get("authorization", "")
    for name, token in USERS.items():
        if secrets.compare_digest(got, f"Bearer {token}"):
            return name
    raise HTTPException(401, "token 錯誤或沒帶")


def text_of(content: Any) -> str:
    if isinstance(content, list):   # 多段格式 [{"type":"text","text":...}]，圖片等其他段落忽略
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def history_key(user: str, msgs: list[dict]) -> str:
    norm = [user, *([m["role"], text_of(m.get("content")).strip()] for m in msgs)]   # 加上使用者，對話不會接到別人的
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


def user_cwd(user: str) -> str:
    if not WORKSPACE_ROOT:
        return THREAD_SETTINGS["cwd"]
    cwd = WORKSPACE_ROOT / user
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


async def call(method: str, url: str, **kw: Any) -> dict:
    """呼叫 codex_agent_server 的一般端點（回合串流在 open_turn）；連不到或回錯誤都轉成 502。"""
    try:
        r = await agent.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"連不到 Codex Agent 後端 {AGENT_URL}：{type(exc).__name__}") from None
    if r.status_code >= 400:
        raise HTTPException(502, f"Codex Agent 後端回 {r.status_code}：{r.text[:300]}")
    return r.json()


async def ensure_codex() -> None:
    """Codex 還沒啟動就用 --llm-url 的設定啟動。

    一定要先確認真的沒啟動：/api/connect 會重啟共用的 Codex，連網頁正在跑的回合也會被中止。
    加鎖後再確認一次，同時進來的請求只會啟動一次。
    """
    async with connect_lock:
        if (await call("GET", "/api/status"))["connected"]:
            return
        if not LLM:
            raise HTTPException(503, "Codex 尚未啟動：請先在 Codex Agent 網頁連線，或啟動 gateway 時給 --llm-url")
        await call("POST", "/api/connect", json=LLM, timeout=120)   # 第一次啟動 app-server 比較久


async def busy() -> bool:
    return bool((await call("GET", "/api/status"))["active_turns"])


async def list_skills(user: str) -> list[dict]:
    return (await call("GET", "/api/skills", params={"cwd": user_cwd(user)})).get("skills", [])


async def mentioned_skills(user: str, text: str) -> list[dict]:
    """訊息裡的 $名稱 對得到 Codex 載入的 skill，就明確指定使用（跟網頁 / 選單一樣）；對不到的當普通文字。"""
    names = set(re.findall(r"\$([\w:.-]+)", text))
    if not names:
        return []
    return [{"name": k["name"], "path": k["path"]} for k in await list_skills(user) if k["name"] in names]


def skills_help(skills: list[dict]) -> str:
    if not skills:
        return "目前沒有可用的 skills。"
    rows = "\n".join(f"- `${k['name']}`：{k.get('description') or ''}" for k in skills)
    return (f"可用的 skills（{len(skills)}）：\n\n{rows}\n\n"
            f"在訊息裡打 `$名稱` 就會指定使用，例如：`${skills[0]['name']} 幫我…`")


def thread_body(user: str, instructions: str) -> dict:
    return {**THREAD_SETTINGS, "cwd": user_cwd(user), "developer_instructions": instructions}


async def new_thread(user: str, instructions: str) -> str:
    return (await call("POST", "/api/threads", json=thread_body(user, instructions)))["thread_id"]


async def resume_thread(thread_id: str, user: str, instructions: str) -> bool:
    """Codex 重啟後後端不認得這個 thread，但 Codex 自己存的紀錄還在：接續回來可以保留完整上下文。"""
    try:
        r = await agent.post(f"/api/threads/{thread_id}/resume", json=thread_body(user, instructions))
    except httpx.HTTPError:
        return False
    return r.status_code == 200 and r.json().get("ok") is True


async def open_turn(thread_id: str, text: str, effort: str, skills: list[dict]) -> httpx.Response | None:
    """回傳串流中的回應；後端不認得這個 thread 回 None；後端正忙丟 Busy。"""
    req = agent.build_request("POST", f"/api/threads/{thread_id}/turn",
                              json={"text": text, "effort": effort, "skills": skills},
                              timeout=httpx.Timeout(30, read=None))   # 回合可能跑很久，後端會定時送心跳
    try:
        resp = await agent.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"連不到 Codex Agent 後端 {AGENT_URL}：{type(exc).__name__}") from None
    if resp.status_code == 200:
        return resp
    await resp.aread()
    await resp.aclose()
    if resp.status_code == 404:
        return None
    if resp.status_code == 409:
        raise Busy
    raise HTTPException(502, f"Codex Agent 後端回 {resp.status_code}：{resp.text[:300]}")


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
        elif method == "agent/serverRequest":
            # 沒人能回答的話，Codex 會整個卡住等到後端逾時（10 分鐘），其他人也都排不到
            with contextlib.suppress(httpx.HTTPError):
                await agent.post(f"/api/requests/{p.get('requestId')}",
                                 json={"result": DECLINE.get(p.get("method"), {})})
        elif method == "agent/error":
            yield f"\n\n[錯誤] {p.get('message')}"
        elif method == "turn/completed" and (p.get("turn") or {}).get("error"):
            yield f"\n\n[錯誤] {json.dumps(p['turn']['error'], ensure_ascii=False)}"


async def answer(user: str, msgs: list[dict], effort: str, wait_s: float) -> AsyncIterator[str]:
    """排隊 → 開回合 → 轉送回覆，結束後記下對話對應。yield 空字串代表心跳（排隊中，或 Codex 在跑指令）。

    排隊和開回合都在這個產生器裡面做：對方斷線時產生器被關閉，finally 一定會讓出佇列、關掉上游。
    """
    text = text_of(msgs[-1].get("content"))
    thread_id = threads.get(history_key(user, msgs[:-1]))
    parts: list[str] = []

    if text.strip() in ("/skills", "／skills"):   # 列出 skills：不送給模型，也不用排隊
        parts.append(skills_help(await list_skills(user)))
        yield parts[0]
    else:
        instructions = "\n\n".join(text_of(m.get("content")) for m in msgs if m["role"] in ("system", "developer"))
        convo = [m for m in msgs[:-1] if m["role"] in ("user", "assistant")]
        send_text, skills = text, None   # 沿用舊 thread 時只送最後一句

        async def attempt() -> httpx.Response:
            nonlocal thread_id, send_text, skills
            if await busy():   # 先看有沒有空：忙的時候不白開 thread，也不去碰可能正卡在等核准的 Codex
                raise Busy
            if skills is None:
                skills = await mentioned_skills(user, text)
            for _ in range(2):   # 第二圈：舊 thread 接不回來，改開新的
                if thread_id is None:
                    thread_id = await new_thread(user, instructions)
                    send_text = with_history(convo, text)   # 新 thread 沒有上下文，把先前的對話一起送
                resp = await open_turn(thread_id, send_text, effort, skills)
                if resp is None and await resume_thread(thread_id, user, instructions):
                    resp = await open_turn(thread_id, send_text, effort, skills)
                if resp is not None:
                    return resp
                thread_id = None
            raise HTTPException(502, "剛開的對話找不到")

        ticket = object()
        waiting.append(ticket)
        try:
            deadline, tick, resp = time.monotonic() + wait_s, 0, None
            while resp is None:
                if waiting[0] is ticket:   # 排到最前面才能試；前面的人跑完才會離開佇列
                    with contextlib.suppress(Busy):
                        resp = await attempt()
                        break
                if time.monotonic() >= deadline:
                    raise HTTPException(429, f"Codex 忙碌中，排隊超過 {wait_s:.0f} 秒還沒輪到，請稍後再試")
                if tick % 20 == 0:
                    yield ""   # 排隊時約每 10 秒送一次心跳，對方的連線才不會斷
                tick += 1
                await asyncio.sleep(POLL_S)
            try:
                async for t in replies(resp):
                    if t:
                        parts.append(t)
                    yield t
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"和 Codex Agent 後端的連線中斷：{type(exc).__name__}") from None
            finally:
                await resp.aclose()   # 對方斷線時關掉上游，後端會中止這個回合
        finally:
            waiting.remove(ticket)

    if thread_id:   # /skills 夾在對話中間也不會讓下一句對不上
        remember(history_key(user, msgs + [{"role": "assistant", "content": "".join(parts)}]), thread_id)


@app.get("/v1/models", dependencies=[Depends(auth)])
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "codex-agent"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request, user: str = Depends(auth)):
    body = await request.json()
    msgs = [m for m in body.get("messages", []) if m.get("role") in ("system", "developer", "user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "messages 最後一則要是 user")
    await ensure_codex()
    streaming = bool(body.get("stream"))
    source = answer(user, msgs, body.get("reasoning_effort") or "",
                    QUEUE_WAIT_S if streaming else min(QUEUE_WAIT_S, NONSTREAM_WAIT_S))
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()), "model": MODEL_ID}

    if not streaming:
        async def collect() -> str:
            return "".join([t async for t in source])

        task = asyncio.create_task(collect())
        try:
            while not (await asyncio.wait({task}, timeout=1))[0]:
                # 一般回應在對方斷線後不會自己停：不檢查的話回合會照跑，白白佔住唯一的名額
                if await request.is_disconnected():
                    raise HTTPException(499, "對方已斷線")
            reply = task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)   # 等它收尾：讓出佇列、關掉上游
        return {**base, "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def chunk(delta: dict, finish: str | None = None) -> str:
        data = {**base, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def stream() -> AsyncIterator[str]:
        yield chunk({"role": "assistant", "content": ""})
        async with contextlib.aclosing(source):   # 對方斷線時確實收尾：讓出佇列、關掉上游
            try:
                async for t in source:
                    yield chunk({"content": t}) if t else ": keepalive\n\n"
            except HTTPException as exc:   # 串流已經開始，改不了狀態碼，錯誤用文字告訴對方
                yield chunk({"content": f"[錯誤] {exc.detail}"})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex Agent 的 OpenAI 相容轉接層")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    for flag, env in OPTIONS.items():
        ap.add_argument(flag, help=f"沒給時用環境變數 {env}")
    a = ap.parse_args()
    for flag, env in OPTIONS.items():
        value = getattr(a, flag[2:].replace("-", "_"))
        if value is not None:
            os.environ[env] = value
    configure()
    if a.host not in ("127.0.0.1", "localhost") and not USERS:
        sys.exit("對外開放（--host 不是本機）一定要給 --users 或 --token，"
                 "否則任何人都能在這台電腦執行指令")
    if WORKSPACE_ROOT:
        print(f"多人模式：{', '.join(USERS)}，工作資料夾在 {WORKSPACE_ROOT}")
    print(f"OpenAI gateway: http://{a.host}:{a.port}/v1  →  {AGENT_URL}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()

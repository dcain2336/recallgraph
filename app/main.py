"""FastAPI entrypoint for RecallGraph.

Endpoints:
- POST /chat {tenant, message} -> {answer, trail, episode_id, facts_written, llm_used}
- POST /remember {tenant, text} -> {episode_id}
- POST /seed -> {status: "ok", ...} (runs seed.main() synchronously)
- GET /graph?tenant=...&limit=60 -> {nodes, edges}
- GET /health -> {falkordb: "ok"|"down", llm: "ok"|"down"}
- GET / -> serves the demo UI (ui/index.html)

Tenant ids are validated with memory.validate_tenant; invalid ids get HTTP 422.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

load_dotenv()

from app import agent, memory, seed
from app.cypher_gen import LLMClient

app = FastAPI(title="RecallGraph", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatIn(BaseModel):
    tenant: str
    message: str


class RememberIn(BaseModel):
    tenant: str
    text: str


def _validate_or_422(tenant: str) -> str:
    try:
        return memory.validate_tenant(tenant)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/chat")
def post_chat(body: ChatIn):
    tenant = _validate_or_422(body.tenant)
    result = agent.chat(tenant, body.message)
    return {
        "answer": result.get("answer"),
        "trail": result.get("trail"),
        "episode_id": result.get("episode_id"),
        "facts_written": result.get("facts_written"),
        "llm_used": result.get("llm_used"),
    }


@app.post("/remember")
def post_remember(body: RememberIn):
    tenant = _validate_or_422(body.tenant)
    episode_id = memory.add_episode(tenant, body.text)
    return {"episode_id": episode_id}


@app.post("/seed")
def post_seed():
    try:
        summary = seed.main()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="seed failed: %s" % (exc,))
    result = {"status": "ok"}
    if isinstance(summary, dict):
        result.update(summary)
    elif summary is not None:
        result["summary"] = str(summary)
    return result


@app.get("/graph")
def get_graph(tenant: str = Query(...), limit: int = Query(default=60)):
    _validate_or_422(tenant)
    limit = max(1, min(200, limit))
    return memory.subgraph(tenant, limit=limit)


@app.get("/health")
def get_health():
    falkor = "ok" if memory.ping() else "down"
    llm = "down"
    try:
        client = LLMClient()
        if client.configured() and client.ping():
            llm = "ok"
    except Exception:
        llm = "down"
    return {"falkordb": falkor, "llm": llm}


@app.get("/")
def index():
    ui_path = Path(__file__).resolve().parent.parent / "ui" / "index.html"
    if not ui_path.is_file():
        return PlainTextResponse(
            "RecallGraph UI file is absent: %s" % (ui_path,), status_code=500
        )
    return HTMLResponse(ui_path.read_text(encoding="utf-8"))

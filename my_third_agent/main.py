from fastapi import FastAPI
from pydantic import BaseModel
from datetime import date
import pathlib, yaml

# ── Load metadata once ───────────────────────────────────────
meta = yaml.safe_load(pathlib.Path(__file__).with_name("agent.yaml").read_text())
NAME    = meta.get("name", "UnnamedAgent")
VERSION = meta.get("version", "0.0.0")

app = FastAPI(title=NAME)

# ── Schemas ─────────────────────────────────────────────────
class InPayload(BaseModel):
    today: date

class OutPayload(BaseModel):
    message: str

# ── Routes ──────────────────────────────────────────────────
@app.post("/invoke", response_model=OutPayload, tags=["Agent Ops"])
async def invoke(p: InPayload):
    return {"message": f"Hello 3! You said today is {p.today}."}

@app.get("/health", tags=["Agent Ops"])
async def health():
    return {"status": "ok", "version": VERSION}

@app.get("/metadata", tags=["Agent Ops"])
async def metadata():
    return meta

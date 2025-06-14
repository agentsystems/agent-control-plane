from fastapi import FastAPI
from pydantic import BaseModel
from datetime import date

import pathlib, yaml

app = FastAPI(title="Hello-Agent")

class InPayload(BaseModel):
    today: date

class OutPayload(BaseModel):
    message: str

@app.post("/invoke", response_model=OutPayload)
async def invoke(p: InPayload):
    return {"message": f"Hello! You said today is {p.today}."}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/metadata")
async def metadata():
    """Serve agent.yaml so the gateway can show rich details."""
    path = pathlib.Path(__file__).with_name("agent.yaml")
    return yaml.safe_load(path.read_text())


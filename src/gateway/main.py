"""FastAPI app: POST /route is the one door. Also serves the JavaScript dashboard at /."""
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.gateway.logger import get_history, init_db, log_request
from src.gateway.router import route_request
from src.gateway.schemas import ModelSpec, ModelUpdate, RouteRequest, load_registry

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODELS = load_registry()  # in-memory copy: the judge panel edits THIS, not the file
app = FastAPI(title="NeuroRoute")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


@app.post("/route")
async def route(req: RouteRequest):
    resp = await route_request(req, MODELS)
    log_request(req.model_dump(), resp)
    return resp


@app.get("/api/models")
@app.get("/models")
def list_models():
    return MODELS


@app.patch("/api/models/{name}")
@app.patch("/models/{name}")
def update_model(name: str, update: ModelUpdate):
    for m in MODELS:
        if m["name"] == name:
            m.update(update.model_dump(exclude_none=True))
            return m
    raise HTTPException(404, f"unknown model {name}")


@app.post("/api/models/reset")
@app.post("/models/reset")
def reset_models():
    MODELS[:] = load_registry()
    return MODELS


@app.get("/api/history")
@app.get("/history")
def history(limit: int = 50):
    return get_history(limit)



# Keep this LAST: it serves src/dashboard/index.html at "/".
app.mount("/", StaticFiles(directory=os.path.join(SRC_DIR, "dashboard"), html=True), name="ui")

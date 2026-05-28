from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
from agent import run_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [AGENT] %(message)s")

app = FastAPI(title="생산 CAPA Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class OrderRequest(BaseModel):
    order_id:     str
    product_code: str
    quantity:     int
    deadline:     str
    priority:     int = 1

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/agent/capa")
def capa_agent(req: OrderRequest):
    """생산 CAPA 판단 — Trajectory 반환"""
    try:
        trajectory = run_agent(
            order_id=req.order_id,
            product_code=req.product_code,
            quantity=req.quantity,
            deadline=req.deadline,
            priority=req.priority
        )
        return {"status": "ok", "trajectory": trajectory}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import json
import os
import uuid
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List
from pathlib import Path

import asyncio

import aiomysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.graph import capa_graph
from agent.state import CapaAgentState
from agent.tools import MES_API_URL
from agent.nodes import LLM_BASE_MODEL, LLM_1B_MODEL, call_lora_full_trajectory

app = FastAPI(title="Production CAPA Agent", version="1.0.0")

_STATIC = Path(__file__).parent.parent / "static"
_TRAJ_DIR = Path(__file__).parent.parent / "trajectories"
_TRAJ_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=_STATIC), name="static")

_pool: aiomysql.Pool | None = None

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "mes_user")
DB_PASS = os.getenv("DB_PASS", "mes_pass")
DB_NAME = os.getenv("DB_NAME", "mes_db")


@app.on_event("startup")
async def startup():
    global _pool
    _pool = await aiomysql.create_pool(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, db=DB_NAME,
        autocommit=True, minsize=1, maxsize=5,
    )


@app.on_event("shutdown")
async def shutdown():
    if _pool:
        _pool.close()
        await _pool.wait_closed()


class CapaRequest(BaseModel):
    product_code: str
    required_quantity: int
    deadline: str
    order_id: str = ""
    model_mode: str = "original"  # "original" | "lora"


class SaveRequest(BaseModel):
    trajectory_id: str
    product_code: str
    required_quantity: int
    deadline: str
    judgment: bool
    recommended_machine: Optional[str]
    selection_reason: str
    full_state: dict


class ScheduleAddRequest(BaseModel):
    trajectory_id: str
    product_code: str
    required_quantity: int
    deadline: str
    judgment: bool
    recommended_machine: Optional[str]
    selection_reason: str
    full_state: dict
    capa_summary: List[dict]
    multi_machine_plan: Optional[dict] = None


class CapaResponse(BaseModel):
    trajectory_id: str
    judgment: bool
    recommended_machine: Optional[str]
    selection_reason: str
    capa_summary: list
    alternative_scenarios: object = None
    multi_machine_plan: Optional[dict] = None
    full_state: dict
    llm_trajectory: Optional[dict] = None


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _build_trajectory(request: CapaRequest, result: dict, created_at: datetime, completed_at: datetime) -> dict:
    step_traces = result.get("step_traces", [])
    duration_ms = int((completed_at - created_at).total_seconds() * 1000)

    sequential = {t["step_id"]: t for t in step_traces if t["step_id"] != "step-003"}
    parallel = [t for t in step_traces if t["step_id"] == "step-003"]

    # step-003: group by machine_id, compute wall-clock span
    step_003_state = None
    if parallel:
        instances = {
            t["machine_id"]: {
                **{k: v for k, v in t.items() if k not in ("step_id", "machine_id")},
                "input": {k: v for k, v in t["input"].items() if k != "scheduled_periods"},
            }
            for t in parallel
        }
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        starts = [datetime.strptime(t["started_at"], fmt) for t in parallel]
        ends = [datetime.strptime(t["completed_at"], fmt) for t in parallel]
        wall_ms = int((max(ends) - min(starts)).total_seconds() * 1000)
        step_003_state = {
            "type": "tool_call",
            "tool": "get_machine_capa",
            "langgraph_send": True,
            "wall_clock_ms": wall_ms,
            "instances": instances,
        }

    def _strip_step_id(trace: dict) -> dict:
        return {k: v for k, v in trace.items() if k not in ("step_id", "machine_id")}

    # ── step_defs: 실제 실행된 trace 기반으로 동적 구성 ─────────────────
    FIXED_STEP_DEFS = {
        "step-001": {
            "step_id": "step-001",
            "step_name": "MES 생산계획 및 사출기별 가동 일정 조회",
            "type": "tool_call",
            "tool": "get_mes_production_schedule",
            "langgraph_node": "mes_schedule_node",
            "api_endpoint": f"{MES_API_URL}/schedule",
        },
        "step-002": {
            "step_id": "step-002",
            "step_name": "금형 위치·사용횟수·교체시간 조회",
            "type": "tool_call",
            "tool": "get_mold_status",
            "langgraph_node": "mold_status_node",
            "api_endpoint": f"{MES_API_URL}/mold",
        },
        "step-003": {
            "step_id": "step-003",
            "step_name": "사출기별 가용 CAPA 계산",
            "type": "tool_call",
            "tool": "get_machine_capa",
            "langgraph_node": "calculate_capa_node",
            "langgraph_send": True,
            "api_endpoint": f"{MES_API_URL}/capa",
            "parallel_instances": [t["machine_id"] for t in parallel],
        },
        "step-004": {
            "step_id": "step-004",
            "step_name": "가용 CAPA 기반 생산 가능 여부 추론 및 추천",
            "type": "llm_reasoning",
            "langgraph_node": "reasoning_node",
            "model": LLM_1B_MODEL if request.model_mode == "lora" else LLM_BASE_MODEL,
        },
        "step-004a": {
            "step_id": "step-004a",
            "step_name": "최적 설비 조합 탐색",
            "type": "python_computation",
            "langgraph_node": "reasoning_node",
            "algorithm": "greedy_descending_capacity",
        },
        "step-004b": {
            "step_id": "step-004b",
            "step_name": "다중 설비 조합 추천 이유 생성",
            "type": "llm_reasoning",
            "langgraph_node": "reasoning_node",
            "model": LLM_1B_MODEL if request.model_mode == "lora" else LLM_BASE_MODEL,
        },
        "step-005": {
            "step_id": "step-005",
            "step_name": "결과 정규화 및 오케스트레이터 반환",
            "type": "transform",
            "langgraph_node": "result_node",
        },
    }

    # 실행된 step 순서대로 step_defs 조립
    ordered_ids = ["step-001", "step-002", "step-003", "step-004a", "step-004b", "step-004", "step-005"]
    step_defs = [
        FIXED_STEP_DEFS[sid]
        for sid in ordered_ids
        if sid in sequential or sid == "step-003"
    ]

    return {
        "trajectory_id": result["trajectory_id"],
        "agent_id": result["agent_id"],
        "order_id": result["order_id"],
        "created_at": _fmt_dt(created_at),
        "completed_at": _fmt_dt(completed_at),
        "duration_ms": duration_ms,

        "goal": {
            "description": "요청 수량을 납기 내에 생산할 수 있는지 사출기별 가용 능력 판단",
            "input_from_orchestrator": {
                "product_code": request.product_code,
                "required_quantity": request.required_quantity,
                "deadline": request.deadline,
            },
            "success_criteria": "사출기별 가용 CAPA 요약 + 생산 가능 여부 판정 + 추천 설비 반환",
        },

        "plan": {
            "strategy": "sequential_with_parallel",
            "steps_expected": [s["step_name"] for s in step_defs],
        },

        "actions": {
            "strategy": "sequential_with_parallel",
            "parallel_groups": [t["machine_id"] for t in parallel],
            "steps": step_defs,
        },

        "state": {
            "langgraph_node": result["agent_id"],
            "langgraph_state": result.get("langgraph_state", "completed"),
            "retry_count": result.get("retry_count", 0),
            "human_in_loop": result.get("human_in_loop", False),
            "next_node": "orchestrator",
            "step_001": _strip_step_id(sequential["step-001"]) if "step-001" in sequential else None,
            "step_002": _strip_step_id(sequential["step-002"]) if "step-002" in sequential else None,
            "step_003": step_003_state,
            **{
                f"step_{sid.replace('-', '_').replace('step_', '')}":
                    _strip_step_id(sequential[sid])
                for sid in ["step-004a", "step-004b", "step-004", "step-005"]
                if sid in sequential
            },
        },

        "result": {
            "judgment": result["judgment"],
            "recommended_machine": result["recommended_machine"],
            "multi_machine_plan": result.get("multi_machine_plan"),
            "selection_reason": result["selection_reason"],
            "capa_summary": result["capa_summary"],
            "alternative_scenarios": result.get("alternative_scenarios"),
        },

        "recovery": {
            "triggered": bool(result.get("error")),
            "strategy": None,
            "fallback_used": None,
        },
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/models")
async def get_models():
    return {"base": LLM_BASE_MODEL, "lora": LLM_1B_MODEL}


@app.post("/api/v1/capa", response_model=CapaResponse)
async def analyze_capa(request: CapaRequest):
    trajectory_id = f"traj-capa-{uuid.uuid4().hex[:8]}"

    initial_state: CapaAgentState = {
        "trajectory_id": trajectory_id,
        "agent_id": "production_capa_agent",
        "order_id": request.order_id or f"ORD-{uuid.uuid4().hex[:8]}",
        "product_code": request.product_code,
        "required_quantity": request.required_quantity,
        "deadline": request.deadline,
        "today": datetime.now(timezone.utc).date().isoformat(),
        "model_mode": request.model_mode,
        "mes_schedule": None,
        "mold_status": None,
        "capa_results": [],
        "reasoning": None,
        "judgment": None,
        "recommended_machine": None,
        "selection_reason": None,
        "capa_summary": None,
        "alternative_scenarios": None,
        "retry_count": 0,
        "human_in_loop": False,
        "error": None,
        "langgraph_state": "started",
        "step_traces": [],
        "multi_machine_plan": None,
    }

    created_at = datetime.now(timezone.utc)

    if request.model_mode == "evaluate":
        # pipeline과 LLM 병렬 실행
        lora_task = asyncio.to_thread(
            call_lora_full_trajectory,
            request.product_code,
            request.required_quantity,
            request.deadline,
        )
        try:
            result, llm_trajectory = await asyncio.gather(
                capa_graph.ainvoke(initial_state),
                asyncio.wait_for(lora_task, timeout=80),
            )
            print(f"[LoRA] turns={llm_trajectory.get('turns')} parse={llm_trajectory.get('parse_success')} ms={llm_trajectory.get('duration_ms')} tokens={llm_trajectory.get('tokens')}", flush=True)
        except asyncio.TimeoutError:
            result = await capa_graph.ainvoke(initial_state)
            llm_trajectory = {"error": "timeout", "parse_success": False, "raw_output": "", "parsed": {}}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    else:
        try:
            result = await capa_graph.ainvoke(initial_state)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        llm_trajectory = None

    completed_at = datetime.now(timezone.utc)
    trajectory = _build_trajectory(request, result, created_at, completed_at)

    return CapaResponse(
        trajectory_id=trajectory_id,
        judgment=result["judgment"],
        recommended_machine=result["recommended_machine"],
        selection_reason=result["selection_reason"],
        capa_summary=result["capa_summary"],
        alternative_scenarios=result["alternative_scenarios"],
        multi_machine_plan=result.get("multi_machine_plan"),
        full_state=trajectory,
        llm_trajectory=llm_trajectory,
    )


@app.post("/api/v1/trajectories/save")
async def save_trajectory(req: SaveRequest):
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO trajectories
                   (trajectory_id, product_code, required_quantity, deadline,
                    judgment, recommended_machine, selection_reason, full_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (req.trajectory_id, req.product_code, req.required_quantity, req.deadline,
                 req.judgment, req.recommended_machine, req.selection_reason,
                 json.dumps(req.full_state, ensure_ascii=False)),
            )
    return {"saved": True, "trajectory_id": req.trajectory_id}


@app.post("/api/v1/schedule/add")
async def add_to_schedule(req: ScheduleAddRequest):
    today = date.today()
    days_map = {m["machine_id"]: (m.get("days_needed") or 1) for m in req.capa_summary}

    if req.recommended_machine:
        targets = [(req.recommended_machine, days_map.get(req.recommended_machine, 1))]
        rec_machine = req.recommended_machine
    elif req.multi_machine_plan:
        targets = [
            (m["machine_id"], m.get("days_needed") or 1)
            for m in req.multi_machine_plan.get("machines", [])
        ]
        recs = req.multi_machine_plan.get("recommended_machines") or []
        rec_machine = recs[0] if recs else None
    else:
        raise HTTPException(status_code=400, detail="추천 설비 없음")

    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 이번 제품의 target mold 조회
            await cur.execute("SELECT mold_id FROM molds WHERE product_code = %s", (req.product_code,))
            mold_row = await cur.fetchone()
            to_mold = mold_row[0] if mold_row else None

            for machine_id, days_needed in targets:
                await cur.execute(
                    "SELECT id FROM schedules WHERE machine_id=%s",
                    (machine_id,),
                )
                row = await cur.fetchone()
                if not row:
                    continue

                # 현재 설비에 장착된 금형 = 이번 생산의 from_mold
                await cur.execute("SELECT current_mold FROM machines WHERE machine_id = %s", (machine_id,))
                machine_row = await cur.fetchone()
                from_mold = machine_row[0] if machine_row else None

                # 해당 설비의 마지막 스케줄 종료일 조회 → 그 다음 날부터 시작
                await cur.execute(
                    "SELECT MAX(period_to) FROM scheduled_periods WHERE schedule_id = %s",
                    (row[0],),
                )
                last_row = await cur.fetchone()
                last_end = last_row[0] if last_row and last_row[0] else None
                if last_end and last_end >= today:
                    period_from = last_end + timedelta(days=1)
                else:
                    period_from = today

                period_to = (period_from + timedelta(days=max(0, days_needed - 1))).isoformat()
                await cur.execute(
                    "INSERT INTO scheduled_periods (schedule_id, period_from, period_to, from_mold, to_mold) VALUES (%s, %s, %s, %s, %s)",
                    (row[0], period_from.isoformat(), period_to, from_mold, to_mold),
                )

                # 생산 완료 후 설비에 남는 금형으로 업데이트
                if to_mold:
                    await cur.execute(
                        "UPDATE machines SET current_mold = %s WHERE machine_id = %s",
                        (to_mold, machine_id),
                    )

            await cur.execute(
                """INSERT IGNORE INTO trajectories
                   (trajectory_id, product_code, required_quantity, deadline,
                    judgment, recommended_machine, selection_reason, full_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (req.trajectory_id, req.product_code, req.required_quantity, req.deadline,
                 req.judgment, rec_machine, req.selection_reason,
                 json.dumps(req.full_state, ensure_ascii=False)),
            )

    traj_path = _TRAJ_DIR / f"{req.trajectory_id}.json"
    traj_path.write_text(
        json.dumps(req.full_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {"success": True, "scheduled": [t[0] for t in targets], "trajectory_id": req.trajectory_id}


@app.get("/api/v1/schedule")
async def get_schedule(product_code: Optional[str] = None):
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            sql = """
                SELECT mpu.product_code, s.machine_id, s.daily_working_hours,
                       mpu.uph, s.current_product_code, sp.period_from, sp.period_to
                FROM machine_product_uph mpu
                JOIN schedules s ON s.machine_id = mpu.machine_id
                JOIN molds m ON m.product_code = mpu.product_code
                LEFT JOIN scheduled_periods sp
                    ON sp.schedule_id = s.id
                    AND (sp.to_mold = m.mold_id
                         OR (sp.to_mold IS NULL AND s.current_product_code = mpu.product_code))
                {where}
                ORDER BY mpu.product_code, s.machine_id, sp.period_from
            """
            if product_code:
                await cur.execute(sql.format(where="WHERE mpu.product_code = %s"), (product_code,))
            else:
                await cur.execute(sql.format(where=""))
            rows = await cur.fetchall()

    return [
        {
            "product_code": r["product_code"],
            "machine_id": r["machine_id"],
            "daily_working_hours": r["daily_working_hours"],
            "uph": r["uph"],
            "current_product_code": r["current_product_code"],
            "period_from": r["period_from"].isoformat() if r["period_from"] else None,
            "period_to": r["period_to"].isoformat() if r["period_to"] else None,
        }
        for r in rows
    ]

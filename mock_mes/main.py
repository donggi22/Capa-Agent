import math
import os
from datetime import datetime
from typing import List

import aiomysql
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock MES API", version="1.0.0")

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
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        db=DB_NAME,
        autocommit=True,
        minsize=2,
        maxsize=10,
    )


@app.on_event("shutdown")
async def shutdown():
    if _pool:
        _pool.close()
        await _pool.wait_closed()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/schedule")
async def get_schedule(product_code: str, deadline: str):
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT s.id, s.machine_id, s.daily_working_hours, mpu.uph,
                       s.current_product_code,
                       sp.period_from, sp.period_to
                FROM schedules s
                JOIN machine_product_uph mpu
                    ON mpu.machine_id = s.machine_id AND mpu.product_code = %s
                LEFT JOIN scheduled_periods sp ON sp.schedule_id = s.id
                ORDER BY s.machine_id
                """,
                (product_code,),
            )
            rows = await cur.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"product_code '{product_code}' not found")

    machines: dict = {}
    for r in rows:
        mid = r["machine_id"]
        if mid not in machines:
            machines[mid] = {
                "machine_id": mid,
                "daily_working_hours": r["daily_working_hours"],
                "uph": r["uph"],
                "current_product_code": r["current_product_code"],
                "scheduled_periods": [],
            }
        if r["period_from"]:
            machines[mid]["scheduled_periods"].append({
                "from": r["period_from"].isoformat(),
                "to": r["period_to"].isoformat(),
            })

    return list(machines.values())


@app.get("/mold")
async def get_mold(product_code: str):
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM molds WHERE product_code = %s",
                (product_code,),
            )
            mold = await cur.fetchone()

            if not mold:
                raise HTTPException(status_code=404, detail=f"product_code '{product_code}' not found")

            target_mold = mold["mold_id"]

            # 기계별 현재 금형 + from→to 교체시간 조회 (금형 조합 기준, 기계 무관)
            await cur.execute(
                """
                SELECT
                    mac.machine_id,
                    mac.current_mold,
                    COALESCE(mct.time_min, 120) AS time_min
                FROM machines mac
                LEFT JOIN mold_change_times mct
                    ON  mct.from_mold = mac.current_mold
                    AND mct.to_mold   = %s
                ORDER BY mac.machine_id
                """,
                (target_mold,),
            )
            change_rows = await cur.fetchall()

    return {
        "mold_id": target_mold,
        "usage_count": mold["usage_count"],
        "max_usage_count": mold["max_usage_count"],
        "cavity_count": mold["cavity_count"],
        "yield_rate": float(mold["yield_rate"]),
        "change_time_by_machine": [
            {
                "machine_id": r["machine_id"],
                "current_mold": r["current_mold"],
                "time_min": r["time_min"],
            }
            for r in change_rows
        ],
    }


class CapaRequest(BaseModel):
    machine_id: str
    daily_working_hours: int
    uph: int
    scheduled_periods: List[dict]
    current_mold: str
    target_mold_id: str
    change_time_min: int
    usage_count: int
    max_usage_count: int
    cavity_count: int
    yield_rate: float = 1.0
    deadline: str
    today: str
    required_quantity: int


@app.post("/capa")
async def calculate_capa(req: CapaRequest):
    today_dt = datetime.strptime(req.today, "%Y-%m-%d")
    deadline_dt = datetime.strptime(req.deadline, "%Y-%m-%d")

    # 수율 보정: 실제 생산해야 할 총 수량 (불량 포함)
    adjusted_quantity = math.ceil(req.required_quantity / req.yield_rate)

    shots_needed = math.ceil(adjusted_quantity / req.cavity_count)
    usage_exhausted = (req.usage_count + shots_needed) > req.max_usage_count
    mold_type_change = req.current_mold != req.target_mold_id
    mold_replacement_required = mold_type_change or usage_exhausted

    # 교체 불필요(동일 금형 + 수명 여유)면 0, 그 외엔 DB 조회 시간 그대로 사용
    effective_change_time = req.change_time_min if mold_replacement_required else 0

    last_scheduled_dt = today_dt
    for period in req.scheduled_periods:
        end_dt = datetime.strptime(period.get("to", req.today), "%Y-%m-%d")
        if end_dt > last_scheduled_dt:
            last_scheduled_dt = end_dt

    available_days = max(0, (deadline_dt - last_scheduled_dt).days)
    daily_capacity = req.uph * req.daily_working_hours
    mold_change_hours = effective_change_time / 60
    first_day_hours = max(0.0, req.daily_working_hours - mold_change_hours)
    first_day_capacity = int(req.uph * first_day_hours)

    if available_days <= 0:
        available_capacity = 0
    elif available_days == 1:
        available_capacity = first_day_capacity
    else:
        available_capacity = first_day_capacity + (available_days - 1) * daily_capacity

    sufficient = available_capacity >= adjusted_quantity

    # 필요 생산일수: 실제로 adjusted_quantity 를 채우는 데 걸리는 최소 일수
    if daily_capacity <= 0 or available_days <= 0:
        days_needed = None
    elif first_day_capacity >= adjusted_quantity:
        days_needed = 1
    else:
        remaining = adjusted_quantity - first_day_capacity
        days_needed = 1 + math.ceil(remaining / daily_capacity)

    if mold_type_change:
        replacement_reason = "type_change"
    elif usage_exhausted:
        replacement_reason = "wear_limit"
    else:
        replacement_reason = None

    return {
        "machine_id": req.machine_id,
        "actual_available_days": available_days,
        "days_needed": days_needed,
        "daily_capacity": daily_capacity,
        "first_day_capacity": first_day_capacity,
        "available_capacity": available_capacity,
        "adjusted_quantity": adjusted_quantity,
        "sufficient": sufficient,
        "mold_replacement_required": mold_replacement_required,
        "replacement_reason": replacement_reason,
        "change_time_min": effective_change_time,
        "calculation_trace": {
            "yield_rate": req.yield_rate,
            "adjusted_quantity": adjusted_quantity,
            "shots_needed": shots_needed,
            "projected_usage": req.usage_count + shots_needed,
            "usage_exhausted": usage_exhausted,
            "mold_type_change": mold_type_change,
            "change_time_hours": round(mold_change_hours, 3),
            "first_day_hours": round(first_day_hours, 3),
            "gross_daily_capacity": daily_capacity,
            "available_days": available_days,
        },
    }

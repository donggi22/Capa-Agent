from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pymysql
import os
from datetime import datetime, timedelta
from dbutils.pooled_db import PooledDB

app = FastAPI(title="MES Mock API Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool = PooledDB(
    creator=pymysql,
    maxconnections=5,
    mincached=1,
    host=os.getenv("DB_HOST", "mariadb"),
    port=int(os.getenv("DB_PORT", 3306)),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", "capa1234"),
    database=os.getenv("DB_NAME", "capa_db"),
    cursorclass=pymysql.cursors.DictCursor
)

def get_conn():
    return _pool.connection()

def _blocked_dates() -> list[str]:
    """이번 주 토·일 반환 (유지보수 휴무일 mock)"""
    today = datetime.now().date()
    days_to_sat = (5 - today.weekday()) % 7 or 7
    sat = today + timedelta(days=days_to_sat)
    return [str(sat), str(sat + timedelta(days=1))]

def parse_scenario(order_id: str) -> str:
    """order_id 앞자리로 시나리오 타입 판별"""
    if order_id.startswith("ORD-A"):
        return "A"
    elif order_id.startswith("ORD-B"):
        return "B"
    elif order_id.startswith("ORD-C"):
        return "C"
    else:
        return "ERROR"

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.get("/mes/machines")
def get_machines():
    """사출기 기본 정보 전체 조회"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM machines")
            return {"machines": cur.fetchall()}
    finally:
        conn.close()

@app.get("/mes/capacity")
def get_capacity(order_id: str):
    """사출기별 가용 CAPA 조회"""
    scenario = parse_scenario(order_id)
    if scenario == "ERROR":
        raise HTTPException(status_code=500, detail="알 수 없는 수주 패턴 — recovery 필요")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.machine_id, s.current_load, s.available_days, m.daily_cap, m.tons, m.cycle_sec
                FROM schedules s
                JOIN machines m ON s.machine_id = m.machine_id
                WHERE s.scenario_type = %s
            """, (scenario,))
            rows = cur.fetchall()
            result = {}
            for r in rows:
                result[r["machine_id"]] = {
                    "daily_cap": r["daily_cap"],
                    "current_load": r["current_load"],
                    "available_days": r["available_days"],
                    "tons": r["tons"],
                    "cycle_sec": r["cycle_sec"],
                    "available_cap": int(r["daily_cap"] * (1 - r["current_load"]) * r["available_days"])
                }
            return {"scenario_type": scenario, "capacity": result}
    finally:
        conn.close()

@app.get("/mes/schedule")
def get_schedule(order_id: str):
    """생산 일정 및 가동 가능 일수 조회"""
    scenario = parse_scenario(order_id)
    if scenario == "ERROR":
        raise HTTPException(status_code=500, detail="알 수 없는 수주 패턴 — recovery 필요")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MIN(available_days) AS available_days, MAX(updated_at) AS updated_at
                FROM schedules
                WHERE scenario_type = %s
            """, (scenario,))
            row = cur.fetchone()
            return {
                "scenario_type": scenario,
                "available_days": row["available_days"] if row else 10,
                "blocked_dates": _blocked_dates(),
                "updated_at": str(row["updated_at"]) if row else None
            }
    finally:
        conn.close()

@app.get("/mes/mold")
def get_mold(order_id: str, product_code: str):
    """금형 위치·사용횟수·교체시간 조회"""
    scenario = parse_scenario(order_id)
    if scenario == "ERROR":
        raise HTTPException(status_code=500, detail="알 수 없는 수주 패턴 — recovery 필요")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT mold_id, machine_id, usage_count, max_usage, setup_hours, status,
                       ROUND(usage_count / max_usage * 100, 1) as usage_pct
                FROM molds
                WHERE product_code = %s AND scenario_type = %s
            """, (product_code, scenario))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"금형 정보 없음: {product_code}")
            return {"mold": row}
    finally:
        conn.close()

@app.get("/mes/competing-orders")
def get_competing_orders(order_id: str):
    """경합 수주 조회 (시나리오 C 전용)"""
    scenario = parse_scenario(order_id)
    if scenario != "C":
        return {"competing_orders": []}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, quantity, CAST(deadline AS CHAR) as deadline, priority
                FROM competing_orders
                WHERE scenario_type = 'C'
                ORDER BY priority
            """)
            return {"competing_orders": cur.fetchall()}
    finally:
        conn.close()

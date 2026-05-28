import pymysql
import os
import json
import logging
from datetime import datetime
from dbutils.pooled_db import PooledDB

logger = logging.getLogger(__name__)

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

def save_trajectory(trajectory: dict, traj_id: int = None) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if traj_id is None:
                cur.execute("""
                    INSERT INTO trajectories (order_id, scenario_type, goal, plan, action, state, result, recovery, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    trajectory["goal"]["order_id"],
                    trajectory["goal"]["scenario_type"],
                    json.dumps(trajectory.get("goal"), ensure_ascii=False),
                    json.dumps(trajectory.get("plan"), ensure_ascii=False),
                    json.dumps(trajectory.get("action", []), ensure_ascii=False),
                    json.dumps(trajectory.get("state"), ensure_ascii=False),
                    json.dumps(trajectory.get("result"), ensure_ascii=False),
                    json.dumps(trajectory.get("recovery"), ensure_ascii=False),
                    datetime.now()
                ))
                conn.commit()
                return cur.lastrowid
            else:
                cur.execute("""
                    UPDATE trajectories
                    SET plan = %s, action = %s, state = %s, result = %s, recovery = %s, completed_at = %s
                    WHERE id = %s
                """, (
                    json.dumps(trajectory.get("plan"), ensure_ascii=False),
                    json.dumps(trajectory.get("action", []), ensure_ascii=False),
                    json.dumps(trajectory.get("state"), ensure_ascii=False),
                    json.dumps(trajectory.get("result"), ensure_ascii=False),
                    json.dumps(trajectory.get("recovery"), ensure_ascii=False),
                    datetime.now(),
                    traj_id
                ))
                conn.commit()
                return traj_id
    except Exception as e:
        logger.error(f"Trajectory DB 저장 실패: {e}")
        raise
    finally:
        conn.close()

def get_recent_avg_cap() -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.machine_id, m.daily_cap,
                       AVG(s.current_load) as avg_load
                FROM machines m
                LEFT JOIN schedules s ON m.machine_id = s.machine_id
                GROUP BY m.machine_id, m.daily_cap
            """)
            rows = cur.fetchall()
            total_avg = sum(int(r["daily_cap"] * (1 - (r["avg_load"] or 0.5))) for r in rows)
            return {
                "avg_daily_output": total_avg,
                "data_source": "최근 schedules 테이블 평균 (간이 추정)",
                "confidence": "낮음 — 참고용으로만 활용 권장"
            }
    finally:
        conn.close()

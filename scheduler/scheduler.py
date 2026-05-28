import pymysql
import os
import random
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SCHEDULER] %(message)s")

def get_conn():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "mariadb"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "capa1234"),
        database=os.getenv("DB_NAME", "capa_db"),
        cursorclass=pymysql.cursors.DictCursor
    )

# 시나리오별 current_load 범위 정의
LOAD_RANGES = {
    "A": (0.20, 0.55),   # CAPA 여유 구간
    "B": (0.75, 0.95),   # CAPA 부족 구간
    "C": (0.40, 0.65),   # 경합 구간
}

def update_loads():
    logging.info("current_load 업데이트 시작")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, scenario_type FROM schedules")
            rows = cur.fetchall()
            for row in rows:
                lo, hi = LOAD_RANGES.get(row["scenario_type"], (0.3, 0.7))
                new_load = round(random.uniform(lo, hi), 2)
                cur.execute(
                    "UPDATE schedules SET current_load = %s, updated_at = %s WHERE id = %s",
                    (new_load, datetime.now(), row["id"])
                )
        conn.commit()
        logging.info(f"업데이트 완료 — {len(rows)}개 행")
    except Exception as e:
        logging.error(f"업데이트 실패: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    logging.info("Scheduler 시작")
    update_loads()  # 시작 시 1회 즉시 실행

    scheduler = BlockingScheduler()
    scheduler.add_job(update_loads, "interval", hours=1)
    scheduler.start()

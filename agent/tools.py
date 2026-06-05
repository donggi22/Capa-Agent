import time
import httpx
import os
from typing import List

MES_API_URL = os.getenv("MES_API_URL", "http://localhost:8001")


async def get_mes_production_schedule(product_code: str, deadline: str) -> tuple[int, List[dict], int]:
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{MES_API_URL}/schedule",
            params={"product_code": product_code, "deadline": deadline},
        )
        status_code = resp.status_code
        resp.raise_for_status()
        data = resp.json()
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return status_code, data, duration_ms


async def get_mold_status(product_code: str) -> tuple[int, dict, int]:
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{MES_API_URL}/mold",
            params={"product_code": product_code},
        )
        status_code = resp.status_code
        resp.raise_for_status()
        data = resp.json()
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return status_code, data, duration_ms


async def calculate_machine_capa(
    machine_id: str,
    daily_working_hours: int,
    uph: int,
    scheduled_periods: list,
    current_mold: str,
    target_mold_id: str,
    change_time_min: int,
    usage_count: int,
    max_usage_count: int,
    cavity_count: int,
    yield_rate: float,
    deadline: str,
    today: str,
    required_quantity: int,
) -> tuple[int, dict, int]:
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        payload = {
            "machine_id": machine_id,
            "daily_working_hours": daily_working_hours,
            "uph": uph,
            "scheduled_periods": scheduled_periods,
            "current_mold": current_mold,
            "target_mold_id": target_mold_id,
            "change_time_min": change_time_min,
            "usage_count": usage_count,
            "max_usage_count": max_usage_count,
            "cavity_count": cavity_count,
            "yield_rate": yield_rate,
            "deadline": deadline,
            "today": today,
            "required_quantity": required_quantity,
        }
        resp = await client.post(f"{MES_API_URL}/capa", json=payload)
        status_code = resp.status_code
        resp.raise_for_status()
        data = resp.json()
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return status_code, data, duration_ms

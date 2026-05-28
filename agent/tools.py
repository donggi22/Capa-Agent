import httpx
import os
import time
import logging

MES_API_URL = os.getenv("MES_API_URL", "http://mes-api:8001")

logger = logging.getLogger(__name__)

# ── OpenAI 호환 tool 정의 (EXAONE에 전달) ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_capacity",
            "description": "사출기별 가용 CAPA를 조회합니다. 현재 부하율과 일 최대 생산량을 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "수주 ID (예: ORD-A-0001)"}
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_schedule",
            "description": "생산 일정 및 가동 가능 일수를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "수주 ID"}
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_mold_info",
            "description": "금형의 현재 장착 위치, 누적 사용횟수, 교체 소요시간, 상태를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id":     {"type": "string", "description": "수주 ID"},
                    "product_code": {"type": "string", "description": "제품코드 (예: P-320-BLK)"}
                },
                "required": ["order_id", "product_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_competing_orders",
            "description": "동일 기간 경합 수주를 조회합니다. 시나리오 C일 때 사용합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "수주 ID"}
                },
                "required": ["order_id"]
            }
        }
    }
]

def call_tool(tool_name: str, parameters: dict) -> dict:
    """Tool 이름에 따라 MES API HTTP 호출 후 결과 반환"""
    start = time.time()
    try:
        if tool_name == "get_capacity":
            resp = httpx.get(f"{MES_API_URL}/mes/capacity", params=parameters, timeout=10)
        elif tool_name == "get_schedule":
            resp = httpx.get(f"{MES_API_URL}/mes/schedule", params=parameters, timeout=10)
        elif tool_name == "get_mold_info":
            resp = httpx.get(f"{MES_API_URL}/mes/mold", params=parameters, timeout=10)
        elif tool_name == "get_competing_orders":
            resp = httpx.get(f"{MES_API_URL}/mes/competing-orders", params=parameters, timeout=10)
        else:
            return {"status": "error", "error_message": f"알 수 없는 tool: {tool_name}", "latency_ms": 0}

        latency = int((time.time() - start) * 1000)

        if resp.status_code == 500:
            return {"status": "error", "error_message": resp.json().get("detail", "MES 서버 오류"), "latency_ms": latency}

        return {"status": "success", "data": resp.json(), "latency_ms": latency}

    except httpx.TimeoutException:
        latency = int((time.time() - start) * 1000)
        return {"status": "timeout", "error_message": "MES API 응답 없음 (timeout)", "latency_ms": latency}
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        return {"status": "error", "error_message": str(e), "latency_ms": latency}

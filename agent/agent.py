import json
import re
import logging
import os
from openai import OpenAI
from schema import CapaTrajectory
from tools import call_tool
from db import save_trajectory, get_recent_avg_cap

logger = logging.getLogger(__name__)

llm = OpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:8080/v1"),
    api_key="EMPTY"
)
MODEL = os.getenv("LLM_MODEL", "exaone")

MAX_STEPS = 10

SYSTEM_PROMPT = """당신은 사출성형 공장의 생산 CAPA 판단 AI 에이전트입니다.
Tool을 호출하여 수집한 데이터를 직접 분석하고, 납기 내 생산 가능 여부를 스스로 판단하세요.

판단 기준:
- 가용 CAPA 합계와 요청 수량을 비교하여 생산 가능 여부를 결정하세요.
- CAPA가 부족하면 야간작업, 대체 사출기 투입, 분할납기 중 실행 가능한 대안을 생성하세요.
- 금형 수명이 90% 이상이거나 교체 필요 상태이면 셋업 지연을 리스크로 포함하세요.
- 경합 수주가 있으면 우선순위와 납기를 비교하여 생산 순서를 판단하세요.

반드시 한국어로 답변하세요."""

def parse_scenario(order_id: str) -> str:
    if order_id.startswith("ORD-A"):
        return "A"
    elif order_id.startswith("ORD-B"):
        return "B"
    elif order_id.startswith("ORD-C"):
        return "C"
    return "ERROR"

def _parse_json_response(text: str) -> dict:
    """LLM 응답에서 JSON 추출 — 마크다운 코드블록 처리 포함"""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    return json.loads(text.strip())

def run_agent(order_id: str, product_code: str, quantity: int, deadline: str, priority: int = 1) -> dict:
    scenario = parse_scenario(order_id)

    trajectory: CapaTrajectory = {
        "goal": {
            "order_id": order_id,
            "product_code": product_code,
            "quantity": quantity,
            "deadline": deadline,
            "scenario_type": scenario,
            "priority": priority
        },
        "plan": {
            "strategy": "",
            "tool_sequence": [],
            "replanned": False,
            "replan_reason": None
        },
        "action": [],
        "state": {
            "available_capa": None,
            "required_capa": quantity,
            "capa_gap": None,
            "feasible": None,
            "bottleneck": None,
            "competing_orders": None,
            "material_shortage": None,
            "mold_setup_hours": None
        },
        "result": {
            "feasible": None,
            "summary": "",
            "alternatives": None
        },
        "recovery": None
    }

    traj_id = save_trajectory(trajectory)

    if scenario == "ERROR":
        return _handle_recovery(trajectory, traj_id, "get_capacity", "tool_error", order_id)

    try:
        # ── ② plan ──
        tool_seq = ["get_capacity", "get_mold_info", "get_schedule", "get_competing_orders"]
        trajectory["plan"]["strategy"] = "LLM 오케스트레이터: 전체 Tool 순차 호출 후 최종 판단"
        trajectory["plan"]["tool_sequence"] = tool_seq

        # ── ③ action: 순서 고정, 직접 호출 (LLM 툴 선택 배제) ──
        for step, tool_name in enumerate(tool_seq):
            params = {"order_id": order_id}
            if tool_name == "get_mold_info":
                params["product_code"] = product_code

            call_result = call_tool(tool_name, params)

            trajectory["action"].append({
                "step": step,
                "tool_name": tool_name,
                "parameters": params,
                "raw_response": call_result.get("data"),
                "parsed_result": call_result.get("data"),
                "status": call_result["status"],
                "error_message": call_result.get("error_message"),
                "latency_ms": call_result.get("latency_ms", 0)
            })

            if call_result["status"] in ("error", "timeout"):
                return _handle_recovery(trajectory, traj_id, tool_name, call_result["status"], order_id)

            _update_state(trajectory, tool_name, call_result["data"], quantity)

        # ── ④ result 생성 (LLM) ──
        state = trajectory["state"]
        avail = state.get("available_capa") or {}
        total_avail = sum(avail.values())
        required = state.get("required_capa", 0)
        competing = state.get("competing_orders") or []
        total_competing = sum(o.get("quantity", 0) for o in competing)
        # state["capa_gap"]은 경합 수주 차감까지 완료된 최종 값
        gap = state.get("capa_gap") if state.get("capa_gap") is not None else (total_avail - required)
        mold_hours = state.get("mold_setup_hours") or 0
        bottleneck = state.get("bottleneck") or "없음"

        avail_text = " ".join(f"{k}은 {v}개" for k, v in avail.items())
        gap_text = f"CAPA가 {abs(gap)}개 여유입니다" if gap >= 0 else f"CAPA가 {abs(gap)}개 부족합니다"
        competing_text = " ".join(
            f"수주번호 {o['order_id']} 수량 {o['quantity']}개 납기 {o['deadline']} 우선순위 {o['priority']}순위"
            for o in competing
        ) if competing else "경합 수주가 없습니다"
        competing_summary = f"경합 수주 총 {total_competing:,}개 차감 후 " if competing else ""

        # feasible은 수치로 확정 — LLM에게 맡기지 않음
        feasible = gap >= 0

        feasible_text = "생산 가능합니다" if feasible else "생산 불가능합니다"
        need_alternatives = not feasible or bool(competing)

        result_prompt = f"""
수주번호 {trajectory['goal']['order_id']}의 분석 결과입니다.
요청 수량은 {required}개입니다.
{avail_text}이며 합계는 {total_avail}개입니다.
경합 수주: {competing_text}.
{competing_summary}{gap_text}.
금형 셋업 시간은 {mold_hours}시간입니다.
병목 사항은 {bottleneck}입니다.
납기는 {trajectory['goal']['deadline']}입니다.
판정: {feasible_text}.

위 내용을 바탕으로:
1. 판단 근거를 포함한 한국어 summary를 2~3문장으로 작성하세요.
2. {'생산이 불가능하거나 경합 수주가 있으므로 alternatives 리스트를 최소 2개 생성하세요 (risk_notes 포함).' if need_alternatives else '생산 가능하고 경합이 없으므로 alternatives는 null로 하세요.'}

JSON 형식으로만 응답하세요:
{{"summary": "...", "alternatives": null 또는 [...]}}
"""
        result_resp = llm.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": result_prompt}
            ],
            max_tokens=400
        )

        try:
            llm_out = _parse_json_response(result_resp.choices[0].message.content)
            trajectory["result"] = {
                "feasible": feasible,
                "summary": llm_out.get("summary", ""),
                "alternatives": llm_out.get("alternatives")
            }
        except Exception:
            trajectory["result"] = {
                "feasible": feasible,
                "summary": result_resp.choices[0].message.content,
                "alternatives": None
            }

        return trajectory

    except Exception as e:
        logger.error(f"run_agent 실패: {e}")
        trajectory["result"] = {
            "feasible": None,
            "summary": f"에이전트 실행 중 오류 발생: {e}",
            "alternatives": None
        }
        return trajectory

    finally:
        save_trajectory(trajectory, traj_id)

def _update_state(trajectory: dict, tool_name: str, data: dict, quantity: int):
    state = trajectory["state"]

    if tool_name == "get_capacity" and data:
        cap = data.get("capacity", {})
        total_available = sum(v.get("available_cap", 0) for v in cap.values())
        state["available_capa"] = {k: v.get("available_cap", 0) for k, v in cap.items()}
        state["required_capa"] = quantity
        state["capa_gap"] = total_available - quantity
        state["feasible"] = state["capa_gap"] >= 0
        if state["capa_gap"] < 0:
            state["bottleneck"] = f"CAPA {abs(state['capa_gap']):,}EA 부족"

    elif tool_name == "get_mold_info" and data:
        mold = data.get("mold", {})
        state["mold_setup_hours"] = mold.get("setup_hours", 0)
        usage_pct = mold.get("usage_pct", 0)
        if mold.get("status") != "ok" or usage_pct >= 90:
            note = f"금형 {mold.get('mold_id')} 수명 {usage_pct}% — 교체 필요"
            state["bottleneck"] = (state.get("bottleneck") or "") + f" / {note}" if state.get("bottleneck") else note

    elif tool_name == "get_competing_orders" and data:
        competing = data.get("competing_orders", [])
        state["competing_orders"] = competing
        if competing and state.get("capa_gap") is not None:
            total_competing = sum(o.get("quantity", 0) for o in competing)
            state["capa_gap"] -= total_competing
            state["feasible"] = state["capa_gap"] >= 0
            if state["capa_gap"] < 0:
                note = f"경합 수주 총 {total_competing:,}EA 추가 수요로 CAPA 부족"
                state["bottleneck"] = f"{state.get('bottleneck')} / {note}" if state.get("bottleneck") else note

def _handle_recovery(trajectory: dict, traj_id: int, failed_action: str, error_type: str, order_id: str) -> dict:
    fallback = get_recent_avg_cap()
    required = trajectory["state"].get("required_capa", 0)
    estimated_days = max(1, required // fallback["avg_daily_output"]) if fallback["avg_daily_output"] > 0 else 99

    trajectory["recovery"] = {
        "triggered": True,
        "failed_action": failed_action,
        "error_type": error_type,
        "fallback_used": "최근 schedules 테이블 기반 일평균 생산량 간이 추정",
        "fallback_data": {**fallback, "estimated_days_needed": estimated_days},
        "replan_triggered": False,
        "recovery_note": "MES 조회 실패. 현재 결과는 참고용이며 단독 의사결정 불가"
    }
    trajectory["result"] = {
        "feasible": None,
        "summary": f"MES 조회 실패로 정확한 판단이 어렵습니다. 간이 추정 기준 약 {estimated_days}일 소요 예상입니다. 신뢰도: 낮음",
        "alternatives": None
    }
    trajectory["plan"]["replanned"] = True
    trajectory["plan"]["replan_reason"] = f"{failed_action} {error_type}"

    save_trajectory(trajectory, traj_id)
    return trajectory

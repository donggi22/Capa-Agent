import logging
import json
import re
import os
from typing import TypedDict, Optional, Annotated
import operator

from langgraph.graph import StateGraph, END
from openai import OpenAI

from schema import CapaTrajectory
from tools import call_tool, TOOLS
from db import save_trajectory, get_recent_avg_cap

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:8080/v1")
LLM_MODEL    = os.getenv("LLM_MODEL", "exaone")

_llm = OpenAI(base_url=LLM_BASE_URL, api_key="dummy")

VALID_TOOLS = {t["function"]["name"] for t in TOOLS}


# ── LangGraph State ──────────────────────────────────────────────────────────

class AgentState(TypedDict):
    order_id:        str
    product_code:    str
    quantity:        int
    deadline:        str
    priority:        int
    goal:            dict
    plan:            dict
    action:          Annotated[list, operator.add]
    state:           dict
    result:          dict
    recovery:        Optional[dict]
    traj_id:         Optional[int]
    tools_remaining: list
    current_error:   Optional[str]
    failed_tool:     Optional[str]


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def _call_llm(messages: list) -> str:
    resp = _llm.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        max_tokens=1500,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_json(text: str) -> dict:
    clean = re.sub(r'```(?:json)?\s*', '', text)
    clean = re.sub(r'```', '', clean).strip()
    depth, start = 0, None
    for i, ch in enumerate(clean):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(clean[start:i + 1])
                except Exception:
                    pass
                start = None
    return {}


def _parse_scenario(order_id: str) -> str:
    if order_id.startswith("ORD-A"):
        return "A"
    if order_id.startswith("ORD-B"):
        return "B"
    if order_id.startswith("ORD-C"):
        return "C"
    return "ERROR"


def _update_state(state: dict, tool_name: str, data: dict, quantity: int) -> dict:
    """raw 데이터만 저장 — 계산/판단은 LLM이 수행"""
    if tool_name == "get_capacity" and data:
        state["capacity_raw"]  = data.get("capacity", {})
        state["required_capa"] = quantity

    elif tool_name == "get_mold_info" and data:
        state["mold_raw"] = data.get("mold", {})

    elif tool_name == "get_schedule" and data:
        state["schedule_raw"] = data

    elif tool_name == "get_competing_orders" and data:
        state["competing_orders"] = data.get("competing_orders", [])

    return state


# ── 노드 1: 초기화 ───────────────────────────────────────────────────────────

def initialize(s: AgentState) -> dict:
    scenario = _parse_scenario(s["order_id"])
    goal = {
        "order_id":      s["order_id"],
        "product_code":  s["product_code"],
        "quantity":      s["quantity"],
        "deadline":      s["deadline"],
        "scenario_type": scenario,
        "priority":      s["priority"],
    }
    state = {
        "required_capa":     s["quantity"],
        "capacity_raw":      None,
        "mold_raw":          None,
        "schedule_raw":      None,
        "competing_orders":  None,
        "available_capa":    None,
        "capa_gap":          None,
        "feasible":          None,
        "bottleneck":        None,
        "mold_setup_hours":  None,
        "material_shortage": None,
    }
    traj: CapaTrajectory = {
        "goal":     goal,
        "plan":     {"strategy": "", "tool_sequence": [], "replanned": False, "replan_reason": None},
        "action":   [],
        "state":    state,
        "result":   {"feasible": None, "summary": "", "alternatives": None},
        "recovery": None,
    }
    traj_id = save_trajectory(traj)
    return {
        "goal":            goal,
        "plan":            traj["plan"],
        "state":           state,
        "result":          traj["result"],
        "recovery":        None,
        "traj_id":         traj_id,
        "tools_remaining": [],
        "current_error":   None,
        "failed_tool":     None,
    }


# ── 노드 2: LLM Plan ─────────────────────────────────────────────────────────

def plan(s: AgentState) -> dict:
    if s["goal"]["scenario_type"] == "ERROR":
        return {"current_error": "invalid_order_id", "failed_tool": "plan", "tools_remaining": []}

    tool_names = "\n".join(f"- {t['function']['name']}: {t['function']['description']}" for t in TOOLS)
    prompt = f"""당신은 사출성형 공장의 생산 CAPA 판단 AI 에이전트입니다.
수주 정보를 분석하고 필요한 Tool과 호출 순서를 결정하세요.

수주 정보:
- 수주 ID: {s["order_id"]}
- 제품코드: {s["product_code"]}
- 요청수량: {s["quantity"]:,}개
- 납기: {s["deadline"]}
- 우선순위: {s["priority"]}
- 시나리오: {s["goal"]["scenario_type"]}

사용 가능한 Tool:
{tool_names}

반드시 JSON으로만 응답하세요:
{{"strategy": "전략 설명 (1-2문장)", "tool_sequence": ["tool1", "tool2", ...]}}"""

    text = _call_llm([{"role": "user", "content": prompt}])
    parsed = _parse_json(text)

    tool_seq = [t.lower() for t in parsed.get("tool_sequence", []) if t.lower() in VALID_TOOLS]
    if not tool_seq:
        tool_seq = ["get_capacity", "get_mold_info", "get_schedule", "get_competing_orders"]
        parsed["strategy"] = "전체 Tool 순차 호출 (LLM 파싱 실패 fallback)"

    logger.info(f"[plan] strategy={parsed.get('strategy')} seq={tool_seq}")

    return {
        "plan": {
            "strategy":      parsed.get("strategy", ""),
            "tool_sequence": tool_seq,
            "replanned":     False,
            "replan_reason": None,
        },
        "tools_remaining": tool_seq,
        "current_error":   None,
        "failed_tool":     None,
    }


# ── 노드 3: Tool 호출 ────────────────────────────────────────────────────────

def call_next_tool(s: AgentState) -> dict:
    tools_remaining = list(s["tools_remaining"])
    tool_name = tools_remaining.pop(0)

    params = {"order_id": s["order_id"]}
    if tool_name == "get_mold_info":
        params["product_code"] = s["product_code"]

    result = call_tool(tool_name, params)
    logger.info(f"[tool] {tool_name} → {result['status']} ({result.get('latency_ms')}ms)")

    action_entry = {
        "step":          len(s["action"]),
        "tool_name":     tool_name,
        "parameters":    params,
        "raw_response":  result.get("data"),
        "parsed_result": result.get("data"),
        "status":        result["status"],
        "error_message": result.get("error_message"),
        "latency_ms":    result.get("latency_ms", 0),
    }

    if result["status"] in ("error", "timeout"):
        return {
            "action":          [action_entry],
            "tools_remaining": tools_remaining,
            "current_error":   result["status"],
            "failed_tool":     tool_name,
        }

    updated_state = _update_state(dict(s["state"]), tool_name, result["data"], s["quantity"])
    return {
        "action":          [action_entry],
        "state":           updated_state,
        "tools_remaining": tools_remaining,
    }


# ── 노드 4: LLM Result 생성 ──────────────────────────────────────────────────

def generate_result(s: AgentState) -> dict:
    state     = s["state"]
    required  = state.get("required_capa", 0)
    cap_raw   = state.get("capacity_raw") or {}
    mold_raw  = state.get("mold_raw") or {}
    sched_raw = state.get("schedule_raw") or {}
    competing = state.get("competing_orders") or []

    mold = mold_raw or {}

    prompt = f"""당신은 사출성형 공장의 생산 CAPA 판단 전문가입니다.
아래 MES 원시 데이터를 직접 계산하고 분석하여 납기 내 생산 가능 여부를 종합 판단하세요.

[수주 정보]
- 수주 ID: {s["order_id"]}
- 요청수량: {required:,}개
- 납기: {s["deadline"]}
- 우선순위: {s["priority"]}

[사출기별 현황]
{json.dumps(cap_raw, ensure_ascii=False, indent=2)}
※ 사출기별 가용수량 = daily_cap × (1 - current_load) × available_days

[금형 정보]
{json.dumps(mold, ensure_ascii=False, indent=2)}

[생산 일정]
{json.dumps(sched_raw, ensure_ascii=False, indent=2)}

[경합 수주 — 이 수주들도 동일 CAPA를 사용합니다]
{json.dumps(competing, ensure_ascii=False, indent=2)}

계산 순서:
1. 사출기별 가용수량 계산 (daily_cap × (1 - current_load) × available_days)
2. 총 가용 CAPA = 사출기별 합산
3. CAPA 갭 = 총 가용 - 요청수량 - 경합수주 총량
4. 금형 수명·셋업·경합 우선순위 등 맥락 종합 판단

아래 JSON으로만 응답하세요:
{{
  "available_capa": {{"사출기ID": 가용수량, ...}},
  "total_avail": 총_가용수량,
  "total_competing": 경합_수주_총량,
  "capa_gap": CAPA_갭,
  "bottleneck": "주요 병목 원인 (없으면 null)",
  "feasible": true 또는 false,
  "summary": "계산 근거와 맥락을 포함한 종합 판단 (3-4문장)",
  "alternatives": [
    {{
      "scenario_id": "ALT-01",
      "description": "대안 설명",
      "feasible": true 또는 false,
      "lead_time_days": 숫자,
      "risk_notes": "위험 사항"
    }}
  ]
}}

생산 가능하고 경합도 없으면 alternatives는 null로 설정하세요."""

    text   = _call_llm([{"role": "user", "content": prompt}])
    parsed = _parse_json(text)

    raw_feasible = parsed.get("feasible")
    if isinstance(raw_feasible, str):
        raw_feasible = raw_feasible.strip().lower() == "true"

    machine_capa    = parsed.get("available_capa") or {}
    total_avail     = parsed.get("total_avail") or 0
    total_competing = parsed.get("total_competing") or 0
    capa_gap        = parsed.get("capa_gap")

    updated_state = dict(state)
    updated_state["available_capa"]   = machine_capa
    updated_state["capa_gap"]         = capa_gap
    updated_state["feasible"]         = raw_feasible
    updated_state["bottleneck"]       = parsed.get("bottleneck")
    updated_state["mold_setup_hours"] = mold.get("setup_hours")

    result = {
        "feasible":        raw_feasible,
        "summary":         parsed.get("summary", "LLM 결과 파싱 실패"),
        "alternatives":    parsed.get("alternatives"),
        "machine_capa":    machine_capa,
        "total_avail":     total_avail,
        "total_competing": total_competing,
    }

    traj: CapaTrajectory = {
        "goal":     s["goal"],
        "plan":     s["plan"],
        "action":   s["action"],
        "state":    updated_state,
        "result":   result,
        "recovery": s.get("recovery"),
    }
    save_trajectory(traj, s["traj_id"])
    return {"result": result, "state": updated_state}


# ── 노드 5: Recovery ─────────────────────────────────────────────────────────

def recovery(s: AgentState) -> dict:
    fallback      = get_recent_avg_cap()
    required      = s["state"].get("required_capa", 0)
    daily_out     = fallback.get("avg_daily_output", 0)
    estimated_days = max(1, required // daily_out) if daily_out > 0 else 99

    recovery_data = {
        "triggered":        True,
        "failed_action":    s.get("failed_tool", "unknown"),
        "error_type":       s.get("current_error", "unknown"),
        "fallback_used":    "최근 schedules 테이블 기반 일평균 생산량 간이 추정",
        "fallback_data":    {**fallback, "estimated_days_needed": estimated_days},
        "replan_triggered": False,
        "recovery_note":    "MES 조회 실패. 현재 결과는 참고용이며 단독 의사결정 불가",
    }
    result = {
        "feasible":     None,
        "summary":      f"MES 조회 실패로 정확한 판단이 어렵습니다. 간이 추정 기준 약 {estimated_days}일 소요 예상입니다. 신뢰도: 낮음",
        "alternatives": None,
    }
    plan_data = {
        **s["plan"],
        "replanned":     True,
        "replan_reason": f"{s.get('failed_tool')} {s.get('current_error')}",
    }

    traj: CapaTrajectory = {
        "goal":     s["goal"],
        "plan":     plan_data,
        "action":   s["action"],
        "state":    s["state"],
        "result":   result,
        "recovery": recovery_data,
    }
    save_trajectory(traj, s["traj_id"])
    return {"plan": plan_data, "result": result, "recovery": recovery_data}


# ── 조건부 엣지 ──────────────────────────────────────────────────────────────

def _after_plan(s: AgentState) -> str:
    return "recovery" if s.get("current_error") else "call_next_tool"


def _after_tool(s: AgentState) -> str:
    if s.get("current_error"):
        return "recovery"
    if s["tools_remaining"]:
        return "call_next_tool"
    return "generate_result"


# ── 그래프 빌드 ──────────────────────────────────────────────────────────────

def _build_graph():
    g = StateGraph(AgentState)

    g.add_node("initialize",      initialize)
    g.add_node("plan",            plan)
    g.add_node("call_next_tool",  call_next_tool)
    g.add_node("generate_result", generate_result)
    g.add_node("recovery",        recovery)

    g.set_entry_point("initialize")
    g.add_edge("initialize", "plan")
    g.add_conditional_edges("plan", _after_plan, {
        "call_next_tool": "call_next_tool",
        "recovery":       "recovery",
    })
    g.add_conditional_edges("call_next_tool", _after_tool, {
        "call_next_tool":  "call_next_tool",
        "generate_result": "generate_result",
        "recovery":        "recovery",
    })
    g.add_edge("generate_result", END)
    g.add_edge("recovery",        END)

    return g.compile()


_graph = _build_graph()


# ── 공개 인터페이스 ───────────────────────────────────────────────────────────

def run_agent(order_id: str, product_code: str, quantity: int, deadline: str, priority: int = 1) -> dict:
    initial: AgentState = {
        "order_id":        order_id,
        "product_code":    product_code,
        "quantity":        quantity,
        "deadline":        deadline,
        "priority":        priority,
        "goal":            {},
        "plan":            {},
        "action":          [],
        "state":           {},
        "result":          {},
        "recovery":        None,
        "traj_id":         None,
        "tools_remaining": [],
        "current_error":   None,
        "failed_tool":     None,
    }

    final: AgentState = _graph.invoke(initial)

    return {
        "goal":     final["goal"],
        "plan":     final["plan"],
        "action":   final["action"],
        "state":    final["state"],
        "result":   final["result"],
        "recovery": final["recovery"],
    }

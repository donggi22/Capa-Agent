import json
import os
import time
from datetime import datetime, timezone

from openai import OpenAI

from .state import CapaAgentState
from .tools import get_mes_production_schedule, get_mold_status, calculate_machine_capa, MES_API_URL

llm_client = OpenAI(
    base_url=os.getenv("LLM_API_URL", "http://localhost:8080/v1"),
    api_key="none",
)
# LLM_MODEL = os.getenv("LLM_MODEL", "EXAONE-3.5-7.8B-Instruct-AWQ")
LLM_MODEL = os.getenv("LLM_MODEL", "EXAONE-3.5-7.8B-Instruct")
LLM_MODEL = os.getenv("LLM_MODEL", "EXAONE-4.0-1.2B")


def _now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


async def mes_schedule_node(state: CapaAgentState) -> dict:
    started_at = _now()
    t0 = time.perf_counter()
    status_code, schedule, _ = await get_mes_production_schedule(state["product_code"], state["deadline"])
    duration_ms = int((time.perf_counter() - t0) * 1000)

    trace = {
        "step_id": "step-001",
        "step_name": "MES 생산계획 및 사출기별 가동 일정 조회",
        "type": "tool_call",
        "tool": "get_mes_production_schedule",
        "langgraph_node": "mes_schedule_node",
        "started_at": started_at,
        "completed_at": _now(),
        "duration_ms": duration_ms,
        "http_method": "GET",
        "api_endpoint": f"{MES_API_URL}/schedule",
        "status_code": status_code,
        "input": {"product_code": state["product_code"], "deadline": state["deadline"]},
        "output": schedule,
        "error": None,
    }
    return {
        "mes_schedule": schedule,
        "step_traces": [trace],
        "langgraph_state": "step_001_completed",
    }


async def mold_status_node(state: CapaAgentState) -> dict:
    started_at = _now()
    t0 = time.perf_counter()
    status_code, mold, _ = await get_mold_status(state["product_code"])
    duration_ms = int((time.perf_counter() - t0) * 1000)

    trace = {
        "step_id": "step-002",
        "step_name": "금형 위치·사용횟수·교체시간 조회",
        "type": "tool_call",
        "tool": "get_mold_status",
        "langgraph_node": "mold_status_node",
        "started_at": started_at,
        "completed_at": _now(),
        "duration_ms": duration_ms,
        "http_method": "GET",
        "api_endpoint": f"{MES_API_URL}/mold",
        "status_code": status_code,
        "input": {"product_code": state["product_code"]},
        "output": mold,
        "error": None,
    }
    return {
        "mold_status": mold,
        "step_traces": [trace],
        "langgraph_state": "step_002_completed",
    }


async def calculate_capa_node(state: dict) -> dict:
    """Called in parallel for each machine via LangGraph Send API."""
    started_at = _now()
    t0 = time.perf_counter()

    payload = {
        "machine_id": state["machine_id"],
        "daily_working_hours": state["daily_working_hours"],
        "uph": state["uph"],
        "scheduled_periods": state["scheduled_periods"],
        "current_mold": state["current_mold"],
        "target_mold_id": state["target_mold_id"],
        "change_time_min": state["change_time_min"],
        "usage_count": state["usage_count"],
        "max_usage_count": state["max_usage_count"],
        "cavity_count": state["cavity_count"],
        "yield_rate": state["yield_rate"],
        "deadline": state["deadline"],
        "today": state["today"],
        "required_quantity": state["required_quantity"],
    }

    status_code, result, _ = await calculate_machine_capa(**payload)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    trace = {
        "step_id": "step-003",
        "machine_id": state["machine_id"],
        "step_name": f"사출기별 가용 CAPA 계산 - {state['machine_id']}",
        "type": "tool_call",
        "tool": "calculate_machine_capa",
        "langgraph_node": "calculate_capa_node",
        "langgraph_send": True,
        "started_at": started_at,
        "completed_at": _now(),
        "duration_ms": duration_ms,
        "http_method": "POST",
        "api_endpoint": f"{MES_API_URL}/capa",
        "status_code": status_code,
        "input": payload,
        "output": result,
        "error": None,
    }
    return {"capa_results": [result], "step_traces": [trace]}


def _find_combination(capa_results: list, adjusted_quantity: int) -> tuple:
    """
    전체 기계 불가 시 greedy 조합 선정.
    available_capacity 내림차순으로 합산 >= adjusted_quantity 될 때까지 선택.
    Returns (allocated_machines, feasible).
    """
    candidates = sorted(
        [r for r in capa_results if r["available_capacity"] > 0],
        key=lambda r: -r["available_capacity"],
    )

    selected = []
    cumulative = 0
    for machine in candidates:
        selected.append(machine)
        cumulative += machine["available_capacity"]
        if cumulative >= adjusted_quantity:
            break

    if cumulative < adjusted_quantity:
        return None, False

    # 보유 CAPA 비례로 수량 배분
    total_cap = sum(m["available_capacity"] for m in selected)
    allocated = []
    remaining = adjusted_quantity
    for i, m in enumerate(selected):
        if i == len(selected) - 1:
            alloc = remaining
        else:
            alloc = round(adjusted_quantity * m["available_capacity"] / total_cap)
            remaining -= alloc
        allocated.append({**m, "allocated_quantity": alloc})

    return allocated, True


def _parse_llm_json(raw: str) -> dict:
    cleaned = raw.strip()
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def _build_selection_reason(best: dict, ranked: list, adjusted_quantity: int, top_cap: int) -> str:
    second = ranked[1] if len(ranked) > 1 else None

    if best["sufficient"]:
        lead = (
            f"{best['machine_id']}은 납기 내 최대 CAPA {best['available_capacity']:,}개로 "
            f"필요 수량 {adjusted_quantity:,}개를 납기 내 단독 충족할 수 있습니다."
        )
    else:
        lead = (
            f"{best['machine_id']}은 납기 내 최대 CAPA {best['available_capacity']:,}개로 "
            f"필요 수량 {adjusted_quantity:,}개에 미달하지만, 후보 중 가장 높은 CAPA입니다."
        )

    if second:
        diff = best["available_capacity"] - second["available_capacity"]
        in_top_tier = top_cap > 0 and (second["available_capacity"] / top_cap) >= 0.9
        if diff >= 0:
            compare = f"2순위 {second['machine_id']}({second['available_capacity']:,}개) 대비 {diff:,}개 우위입니다."
        elif in_top_tier:
            compare = (
                f"2순위 {second['machine_id']}({second['available_capacity']:,}개)와 CAPA 차이가 "
                f"{abs(diff):,}개({round(abs(diff)/top_cap*100, 1)}%)로 미미하여 "
                f"교체시간이 짧은 {best['machine_id']}을 우선 선정하였습니다."
            )
        else:
            compare = f"2순위 {second['machine_id']}({second['available_capacity']:,}개) 대비 CAPA는 {abs(diff):,}개 낮지만 교체시간 기준으로 우선 선정되었습니다."
    else:
        compare = ""

    if best.get("replacement_reason") == "wear_limit":
        risk = "다만 이번 수주 중 금형 수명이 소진될 예정이므로 생산 중 교체가 필요합니다."
    elif best.get("replacement_reason") == "type_change":
        risk = f"금형 교체({best['change_time_min']}분)가 필요하여 첫날 가동 시간이 줄어듭니다."
    else:
        risk = "금형 교체 없이 즉시 투입 가능합니다."

    days = best.get("days_needed")
    schedule = f"예상 생산 완료까지 {days}일이 소요됩니다." if days else ""

    return " ".join(filter(None, [lead, compare, risk, schedule]))


def _build_multi_selection_reason(combination: list, adjusted_quantity: int) -> str:
    machine_ids = [m["machine_id"] for m in combination]
    total_cap = sum(m["available_capacity"] for m in combination)

    lead = (
        f"단일 설비로는 필요 수량 {adjusted_quantity:,}개를 납기 내 충족할 수 없어 "
        f"{len(combination)}개 설비 조합 생산으로 전환했습니다."
    )
    total = (
        f"{', '.join(machine_ids)}의 합산 납기 내 최대 CAPA {total_cap:,}개가 필요 수량을 충족합니다."
    )
    alloc_parts = ", ".join(
        f"{m['machine_id']} {m['allocated_quantity']:,}개" for m in combination
    )
    alloc = f"설비별 배분은 {alloc_parts}으로 계획됩니다."

    return " ".join([lead, total, alloc])


def _score_machine(r: dict, top_cap: int) -> int:
    """순수 available_capacity 비율(0~100). greedy 선택 순서와 일치."""
    if top_cap == 0:
        return 0
    return round(100 * r["available_capacity"] / top_cap)


def _multi_criteria_sort(capa_results: list) -> list:
    """
    다기준 정렬:
    1. sufficient 우선
    2. 동일 그룹 내 최고 CAPA 대비 10% 이내 → change_time 오름차순 우선
    3. 10% 초과 차이 → available_capacity 내림차순
    4. wear_limit 패널티
    5. days_needed 오름차순
    """
    sufficient = [r for r in capa_results if r["sufficient"]]
    insufficient = [r for r in capa_results if not r["sufficient"]]

    def _sort_group(machines):
        if not machines:
            return []
        top_cap = max(r["available_capacity"] for r in machines)

        def key(r):
            cap = r["available_capacity"]
            in_top_tier = top_cap > 0 and (cap / top_cap) >= 0.9
            return (
                0 if in_top_tier else 1,
                r["change_time_min"] if in_top_tier else -cap,
                1 if r.get("replacement_reason") == "wear_limit" else 0,
                r.get("days_needed") or 9999,
                -cap,
            )
        return sorted(machines, key=key)

    return _sort_group(sufficient) + _sort_group(insufficient)


def _metric_context(ranked: list) -> str:
    """LLM 프롬프트에 주입할 사전 계산 지표 순위 문자열."""
    cap_top3 = sorted(ranked, key=lambda r: -r["available_capacity"])[:3]
    cap_str = " > ".join(f"{r['machine_id']}({r['available_capacity']:,})" for r in cap_top3)

    need_change = [r for r in ranked if r["mold_replacement_required"]]
    if need_change:
        ct_sorted = sorted(need_change, key=lambda r: r["change_time_min"])[:3]
        ct_str = " < ".join(f"{r['machine_id']}({r['change_time_min']}분)" for r in ct_sorted)
    else:
        ct_str = "전체 설비 교체 불필요"

    wear = [r["machine_id"] for r in ranked if r.get("replacement_reason") == "wear_limit"]
    wear_str = f"{', '.join(wear)} (동일 금형이나 사용횟수 초과)" if wear else "없음"

    with_days = [r for r in ranked if r.get("days_needed")]
    if with_days:
        days_sorted = sorted(with_days, key=lambda r: r["days_needed"])[:3]
        days_str = " < ".join(f"{r['machine_id']}({r['days_needed']}일)" for r in days_sorted)
    else:
        days_str = "N/A"

    return (
        f"[사전 계산된 지표 순위 — 반드시 이 수치만 인용하세요]\n"
        f"CAPA 용량 상위 3: {cap_str}\n"
        f"교체시간 최단 순 (교체 필요 설비만): {ct_str}\n"
        f"금형 수명 초과 교환 필요: {wear_str}\n"
        f"필요 생산일수 최단 순: {days_str}"
    )


def reasoning_node(state: CapaAgentState) -> dict:
    capa_results = state["capa_results"]
    required_quantity = state["required_quantity"]
    adjusted_quantity = capa_results[0].get("adjusted_quantity", required_quantity) if capa_results else required_quantity

    ranked = _multi_criteria_sort(capa_results)
    best = ranked[0]
    all_insufficient = not best["sufficient"]
    metric_ctx = _metric_context(ranked)

    top_cap = max(r["available_capacity"] for r in capa_results) if capa_results else 1

    candidate_filter = [
        {
            "machine_id": r["machine_id"],
            "available_capacity": r["available_capacity"],
            "filter_reason": "available_capacity == 0",
        }
        for r in capa_results if r["available_capacity"] == 0
    ]

    candidate_rank = [
        {
            "rank": i + 1,
            "machine_id": r["machine_id"],
            "available_capacity": r["available_capacity"],
            "sufficient": r["sufficient"],
            "days_needed": r.get("days_needed"),
            "change_time_min": r["change_time_min"],
            "replacement_reason": r.get("replacement_reason"),
            "score": _score_machine(r, top_cap),
        }
        for i, r in enumerate(ranked)
    ]

    decision_policy = {
        "primary": "sufficient=True 우선",
        "secondary": "available_capacity 상위 10% 내 → change_time_min ASC",
        "tertiary": "available_capacity DESC",
        "penalty": "wear_limit -5점, mold_replacement -5점",
    }

    if all_insufficient:
        # ── step-004a: 조합 탐색 (Python greedy) ──────────────────────────
        combo_started_at = _now()
        t0_combo = time.perf_counter()
        combination, feasible = _find_combination(capa_results, adjusted_quantity)
        combo_duration_ms = int((time.perf_counter() - t0_combo) * 1000)

        combo_capacity = sum(m["available_capacity"] for m in combination) if feasible else 0
        judgment_basis = {
            "all_sufficient": False,
            "combination_found": feasible,
            "combination_capacity": combo_capacity,
            "adjusted_quantity": adjusted_quantity,
            "judgment": feasible,
            "reason": (
                f"모든 단일 설비 sufficient=False → greedy 조합 탐색 → "
                f"합산 CAPA({combo_capacity:,}) >= adjusted_quantity({adjusted_quantity:,}) → judgment=True"
                if feasible else
                f"모든 단일 설비 sufficient=False → greedy 조합 탐색 → "
                f"합산 CAPA({sum(r['available_capacity'] for r in capa_results):,}) < adjusted_quantity({adjusted_quantity:,}) → judgment=False"
            ),
        }

        trace_004a = {
            "step_id": "step-004a",
            "step_name": "최적 설비 조합 탐색",
            "type": "python_computation",
            "langgraph_node": "reasoning_node",
            "algorithm": "greedy_descending_capacity",
            "started_at": combo_started_at,
            "completed_at": _now(),
            "duration_ms": combo_duration_ms,
            "input": {
                "adjusted_quantity": adjusted_quantity,
                "candidate_count": len(ranked),
            },
            "output": {
                "feasible": feasible,
                "selected_machines": [m["machine_id"] for m in combination] if feasible else [],
                "cumulative_capacity": combo_capacity,
                "allocation": combination if feasible else [],
                "judgment_basis": judgment_basis,
            },
            "error": None,
        }

        if not feasible:
            reasoning = {
                "mode": "infeasible",
                "recommended_machine": None,
                "recommended_machines": None,
                "multi_machine_plan": None,
                "selection_reason": (
                    f"전체 설비의 합산 납기 내 최대 CAPA({sum(r['available_capacity'] for r in capa_results):,})가 "
                    f"필요 수량({adjusted_quantity:,})에 미달하여 납기 내 생산이 불가합니다."
                ),
                "candidate_filter": candidate_filter,
                "candidate_rank": candidate_rank,
                "final_decision": None,
                "decision_policy": decision_policy,
            }
            return {
                "reasoning": reasoning,
                "step_traces": [trace_004a],
                "langgraph_state": "step_004_completed",
            }

        # ── step-004b: 조합 이유 LLM 생성 ────────────────────────────────
        machine_ids = [m["machine_id"] for m in combination]
        prompt = f"""당신은 제조 생산 설비 추천 전문가입니다.
시스템이 다음과 같이 판단했습니다: judgment=True (납기 내 생산 가능).
{judgment_basis['reason']}

아래 수치는 시스템이 계산한 정확한 값입니다. 수치를 변경하거나 재계산하지 마세요.

필요 수량(수율 보정 전): {required_quantity:,}
필요 수량(수율 보정 후): {adjusted_quantity:,}
선정된 설비 조합: {machine_ids}
합산 available_capacity: {sum(m['available_capacity'] for m in combination):,}

설비별 배분 계획:
{json.dumps(combination, ensure_ascii=False, indent=2)}

납기 내 생산이 가능한 상황에서, 이 다중 설비 조합의 운영 리스크와 고려사항을 3~4문장으로 서술하세요.
수치는 위에 명시된 값만 그대로 인용하고 계산하지 마세요.
- 분산 생산 시 조율 난이도
- 각 설비의 금형 상태 및 교체 여부
- 생산 중 발생 가능한 변수

반드시 다음 JSON 형식으로만 응답하세요 (다른 설명 없이):
{{
  "recommended_machines": {json.dumps(machine_ids, ensure_ascii=False)},
  "llm_narrative": "납기 달성 가능 전제 하에 운영 리스크 서술 3~4문장"
}}"""

        llm_started_at = _now()
        t0_llm = time.perf_counter()
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=384,
            temperature=0.1,
        )
        llm_duration_ms = int((time.perf_counter() - t0_llm) * 1000)

        llm_out = _parse_llm_json(response.choices[0].message.content)
        tokens = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        } if response.usage else None

        reasoning = {
            "mode": "multi_machine",
            "recommended_machine": None,
            "recommended_machines": llm_out["recommended_machines"],
            "multi_machine_plan": combination,
            "selection_reason": _build_multi_selection_reason(combination, adjusted_quantity),
            "llm_narrative": llm_out.get("llm_narrative"),
            "llm_narrative_training_use": False,
            "candidate_filter": candidate_filter,
            "candidate_rank": candidate_rank,
            "final_decision": {
                "recommended_machines": llm_out["recommended_machines"],
                "reason": "greedy_descending_capacity — 최소 설비 수로 adjusted_quantity 충족",
            },
            "decision_policy": decision_policy,
            "judgment_basis": judgment_basis,
        }

        trace_004b = {
            "step_id": "step-004b",
            "step_name": "다중 설비 조합 운영 리스크 분석",
            "type": "llm_reasoning",
            "langgraph_node": "reasoning_node",
            "started_at": llm_started_at,
            "completed_at": _now(),
            "duration_ms": llm_duration_ms,
            "model": LLM_MODEL,
            "temperature": 0.1,
            "input": {
                "machine_ids": machine_ids,
                "adjusted_quantity": adjusted_quantity,
            },
            "output": {"llm_narrative": llm_out.get("llm_narrative")},
            "tokens": tokens,
            "error": None,
        }

        return {
            "reasoning": reasoning,
            "step_traces": [trace_004a, trace_004b],
            "langgraph_state": "step_004_completed",
        }

    else:
        # ── step-004: 단일 기계 LLM 추천 ─────────────────────────────────
        second_best = ranked[1] if len(ranked) > 1 else None
        candidate_rank_ctx = json.dumps(candidate_rank, ensure_ascii=False, indent=2)
        judgment_single = best["sufficient"]
        prompt = f"""당신은 제조 생산 설비 추천 전문가입니다.
시스템이 다음과 같이 판단했습니다: judgment={judgment_single} ({'납기 내 생산 가능' if judgment_single else '납기 내 생산 불가'}).
추천 설비: {best["machine_id"]} / sufficient={best["sufficient"]} / available_capacity={best["available_capacity"]:,} / adjusted_quantity={adjusted_quantity:,}

아래는 시스템이 이미 계산·정렬한 사출기별 CAPA 결과입니다. 수치는 정확하므로 재계산하지 마세요.

필요 수량(수율 보정 전): {required_quantity:,}
필요 수량(수율 보정 후): {adjusted_quantity:,}

[설비별 점수 순위 — 반드시 이 수치만 인용하세요]
{candidate_rank_ctx}

{metric_ctx}

위 판단(judgment={judgment_single})을 전제로, 이 설비 선정의 운영 리스크와 고려사항을 3~4문장으로 서술하세요.
수치는 위에 명시된 값만 그대로 인용하고 계산하지 마세요.
- 금형 상태(wear_limit·type_change 해당 여부)와 그로 인한 리스크
- 납기 달성 가능성 및 여유도
- 차순위 설비 대비 선택 우위 또는 주의사항

반드시 다음 JSON 형식으로만 응답하세요 (다른 설명 없이):
{{
  "recommended_machine": "{best["machine_id"]}",
  "llm_narrative": "judgment={judgment_single} 전제 하에 운영 리스크 서술 3~4문장"
}}"""

        llm_started_at = _now()
        t0_llm = time.perf_counter()
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.1,
        )
        llm_duration_ms = int((time.perf_counter() - t0_llm) * 1000)

        llm_out = _parse_llm_json(response.choices[0].message.content)
        tokens = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        } if response.usage else None

        reasoning = {
            "mode": "single_machine",
            "recommended_machine": llm_out["recommended_machine"],
            "recommended_machines": None,
            "multi_machine_plan": None,
            "selection_reason": _build_selection_reason(best, ranked, adjusted_quantity, top_cap),
            "llm_narrative": llm_out.get("llm_narrative"),
            "llm_narrative_training_use": False,
            "candidate_filter": candidate_filter,
            "candidate_rank": candidate_rank,
            "final_decision": {
                "recommended_machine": best["machine_id"],
                "score": _score_machine(best, top_cap),
                "sufficient": best["sufficient"],
                "key_factor": (
                    "highest_sufficient_capacity" if best["sufficient"]
                    else "highest_available_capacity"
                ),
            },
            "decision_policy": decision_policy,
        }

        trace_004 = {
            "step_id": "step-004",
            "step_name": "가용 CAPA 기반 생산 가능 여부 추론 및 추천",
            "type": "llm_reasoning",
            "langgraph_node": "reasoning_node",
            "started_at": llm_started_at,
            "completed_at": _now(),
            "duration_ms": llm_duration_ms,
            "model": LLM_MODEL,
            "temperature": 0.1,
            "input": {
                "required_quantity": required_quantity,
                "adjusted_quantity": adjusted_quantity,
                "recommended_machine": best["machine_id"],
            },
            "tokens": tokens,
            "reasoning": reasoning,
            "error": None,
        }

        return {
            "reasoning": reasoning,
            "step_traces": [trace_004],
            "langgraph_state": "step_004_completed",
        }


def result_node(state: CapaAgentState) -> dict:
    reasoning = state["reasoning"]
    capa_results = state["capa_results"]
    mode = reasoning.get("mode", "single_machine")

    started_at = _now()
    t0 = time.perf_counter()

    capa_summary = [
        {
            "machine_id": r["machine_id"],
            "actual_available_days": r["actual_available_days"],
            "days_needed": r.get("days_needed"),
            "daily_capacity": r["daily_capacity"],
            "available_capacity": r["available_capacity"],
            "sufficient": r["sufficient"],
            "mold_replacement_required": r["mold_replacement_required"],
            "replacement_reason": r.get("replacement_reason"),
            "change_time_min": r["change_time_min"],
        }
        for r in sorted(capa_results, key=lambda x: x["machine_id"])
    ]

    if mode == "single_machine":
        recommended = reasoning["recommended_machine"]
        judgment = any(r["sufficient"] for r in capa_results if r["machine_id"] == recommended)
        multi_machine_plan = None
        alternative_scenarios = None

    elif mode == "multi_machine":
        recommended = None
        judgment = True
        plan_machines = reasoning["multi_machine_plan"]
        total_cap = sum(m["available_capacity"] for m in plan_machines)
        total_alloc = sum(m["allocated_quantity"] for m in plan_machines)
        multi_machine_plan = {
            "machines": plan_machines,
            "recommended_machines": reasoning["recommended_machines"],
            "total_capacity": total_cap,
            "total_allocated": total_alloc,
            "judgment_basis": reasoning.get("judgment_basis"),
        }
        adj_qty_multi = plan_machines[0].get("adjusted_quantity") if plan_machines else reasoning.get("adjusted_quantity", 0)
        best_single = max(capa_results, key=lambda r: r["available_capacity"])
        single_shortfall = (adj_qty_multi or 0) - best_single["available_capacity"]

        alt_scenarios = []

        # Alt 1: 최선 단일 설비 (insufficient) + 수량 축소 시나리오
        alt_scenarios.append({
            "type": "best_single_machine",
            "feasible": False,
            "machine_id": best_single["machine_id"],
            "available_capacity": best_single["available_capacity"],
            "shortfall": max(0, single_shortfall),
            "note": (
                f"{best_single['machine_id']} 단독 생산 시 {best_single['available_capacity']:,}개 가능. "
                f"필요 수량 대비 {max(0, single_shortfall):,}개 부족 — 수량 축소 또는 납기 연장 필요."
            ),
        })

        # Alt 2: 조합에서 최약 설비 제거 (N-1 조합 가능 여부)
        if len(plan_machines) > 2:
            reduced = plan_machines[:-1]
            reduced_cap = sum(m["available_capacity"] for m in reduced)
            reduced_shortfall = (adj_qty_multi or 0) - reduced_cap
            if reduced_shortfall <= 0:
                alt_scenarios.append({
                    "type": "reduced_combination",
                    "feasible": True,
                    "machines": [{"machine_id": m["machine_id"]} for m in reduced],
                    "total_capacity": reduced_cap,
                    "note": (
                        f"{plan_machines[-1]['machine_id']} 제외 {len(reduced)}개 설비로도 충족 가능 — "
                        f"합산 CAPA {reduced_cap:,}개 ≥ adjusted_quantity."
                    ),
                })
            else:
                alt_scenarios.append({
                    "type": "reduced_combination",
                    "feasible": False,
                    "machines": [{"machine_id": m["machine_id"]} for m in reduced],
                    "total_capacity": reduced_cap,
                    "shortfall": reduced_shortfall,
                    "note": (
                        f"{plan_machines[-1]['machine_id']} 제외 시 합산 CAPA {reduced_cap:,}개로 {reduced_shortfall:,}개 부족 — "
                        f"{plan_machines[-1]['machine_id']} 필수."
                    ),
                })

        alternative_scenarios = alt_scenarios

    else:  # infeasible
        recommended = None
        judgment = False
        multi_machine_plan = {
            "judgment_basis": reasoning.get("judgment_basis"),
        }
        all_cap = sum(r["available_capacity"] for r in capa_results)
        best_single = max(capa_results, key=lambda r: r["available_capacity"])
        adj_qty = capa_results[0].get("adjusted_quantity", 0) if capa_results else 0
        alternative_scenarios = [
            {
                "type": "quantity_reduction",
                "feasible": all_cap > 0,
                "max_feasible_quantity": all_cap,
                "note": (
                    f"전체 설비 분산 시 최대 {all_cap:,}개까지 현재 납기 내 생산 가능."
                    f"필요 수량({adj_qty:,})을 {all_cap:,} 이하로 축소 시 달성 가능."
                ),
            },
            {
                "type": "deadline_extension",
                "feasible": True,
                "recommended_machine": best_single["machine_id"],
                "best_machine_capacity": best_single["available_capacity"],
                "note": (
                    f"납기 연장 시 {best_single['machine_id']}(available_capacity {best_single['available_capacity']:,}) 기준 단독 또는 조합 생산 검토 가능. "
                    f"현재 납기 기준 전체 설비 CAPA 합산 {all_cap:,}개로 부족."
                ),
            },
        ]

    duration_ms = int((time.perf_counter() - t0) * 1000)

    trace = {
        "step_id": "step-005",
        "step_name": "결과 정규화 및 오케스트레이터 반환",
        "type": "transform",
        "langgraph_node": "result_node",
        "started_at": started_at,
        "completed_at": _now(),
        "duration_ms": duration_ms,
        "input": {
            "mode": mode,
            "recommended_machine": reasoning.get("recommended_machine"),
            "recommended_machines": reasoning.get("recommended_machines"),
            "selection_reason": reasoning.get("selection_reason"),
            "judgment_basis": reasoning.get("judgment_basis"),
            "candidate_rank": reasoning.get("candidate_rank"),
            "llm_narrative_training_use": reasoning.get("llm_narrative_training_use"),
        },
        "output": {
            "judgment": judgment,
            "mode": mode,
            "recommended_machine": recommended,
            "multi_machine_plan": multi_machine_plan,
            "alternative_scenarios": alternative_scenarios,
            "capa_summary": capa_summary,
        },
        "error": None,
    }

    return {
        "judgment": judgment,
        "recommended_machine": recommended,
        "selection_reason": reasoning["selection_reason"],
        "capa_summary": capa_summary,
        "alternative_scenarios": alternative_scenarios,
        "multi_machine_plan": multi_machine_plan,
        "step_traces": [trace],
        "langgraph_state": "completed",
    }

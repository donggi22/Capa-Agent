from typing import TypedDict, Literal, Optional

class GoalSchema(TypedDict):
    order_id:      str
    product_code:  str
    quantity:      int
    deadline:      str
    scenario_type: Literal["A", "B", "C", "ERROR"]
    priority:      int

class PlanSchema(TypedDict):
    strategy:        str
    tool_sequence:   list
    replanned:       bool
    replan_reason:   Optional[str]

class ActionSchema(TypedDict):
    step:          int
    tool_name:     str
    parameters:    dict
    raw_response:  Optional[dict]
    parsed_result: Optional[dict]
    status:        Literal["success", "error", "timeout"]
    error_message: Optional[str]
    latency_ms:    int

class StateSchema(TypedDict):
    required_capa:     Optional[int]
    # Tool 호출 원시 데이터 (LLM이 직접 분석)
    capacity_raw:      Optional[dict]
    mold_raw:          Optional[dict]
    schedule_raw:      Optional[dict]
    competing_orders:  Optional[list]
    # LLM 분석 결과 (generate_result 이후 채워짐)
    available_capa:    Optional[dict]
    capa_gap:          Optional[int]
    feasible:          Optional[bool]
    bottleneck:        Optional[str]
    mold_setup_hours:  Optional[float]
    material_shortage: Optional[bool]

class AlternativeScenario(TypedDict):
    scenario_id:    str
    description:    str
    feasible:       bool
    lead_time_days: int
    risk_notes:     str
    cost_impact:    Optional[str]

class ResultSchema(TypedDict):
    feasible:      Optional[bool]
    summary:       str
    alternatives:  Optional[list]
    machine_capa:  Optional[dict]   # 사출기별 가용 CAPA
    total_avail:   Optional[int]    # 총 가용 CAPA
    total_competing: Optional[int]  # 경합 수주 총량

class RecoverySchema(TypedDict):
    triggered:        bool
    failed_action:    str
    error_type:       Literal["db_timeout", "invalid_response", "tool_error", "unknown"]
    fallback_used:    str
    fallback_data:    dict
    replan_triggered: bool
    recovery_note:    str

class CapaTrajectory(TypedDict):
    goal:     GoalSchema
    plan:     PlanSchema
    action:   list          # ActionSchema 누적
    state:    StateSchema
    result:   ResultSchema
    recovery: Optional[RecoverySchema]

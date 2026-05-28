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
    available_capa:    Optional[dict]
    required_capa:     Optional[int]
    capa_gap:          Optional[int]
    feasible:          Optional[bool]
    bottleneck:        Optional[str]
    competing_orders:  Optional[list]
    # 확장 예약 필드 (멀티 에이전트 전환 시 타 에이전트가 채움)
    material_shortage: Optional[bool]
    mold_setup_hours:  Optional[float]

class AlternativeScenario(TypedDict):
    scenario_id:    str
    description:    str
    feasible:       bool
    lead_time_days: int
    risk_notes:     str
    cost_impact:    Optional[str]

class ResultSchema(TypedDict):
    feasible:     Optional[bool]
    summary:      str
    alternatives: Optional[list]

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

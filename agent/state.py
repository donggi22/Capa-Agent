from __future__ import annotations
from typing import TypedDict, Optional, List, Annotated
import operator


class MachineSchedule(TypedDict):
    machine_id: str
    daily_working_hours: int
    uph: int
    current_product_code: str
    scheduled_periods: List[dict]


class MoldChangeTime(TypedDict):
    machine_id: str
    current_mold: str
    time_min: int


class MoldStatus(TypedDict):
    mold_id: str
    usage_count: int
    max_usage_count: int
    cavity_count: int
    change_time_by_machine: List[MoldChangeTime]


class CapaResult(TypedDict):
    machine_id: str
    actual_available_days: int
    daily_capacity: int
    first_day_capacity: int
    available_capacity: int
    sufficient: bool
    mold_replacement_required: bool
    change_time_min: int


class ReasoningOutput(TypedDict):
    all_sufficient: bool
    alternative_needed: bool
    recommended_machine: str
    selection_reason: str


class CapaAgentState(TypedDict):
    # Input from orchestrator
    trajectory_id: str
    agent_id: str
    order_id: str
    product_code: str
    required_quantity: int
    deadline: str
    today: str
    model_mode: str  # "original" | "lora"

    # Step results
    mes_schedule: Optional[List[MachineSchedule]]
    mold_status: Optional[MoldStatus]
    # Annotated with operator.add to accumulate parallel capa results
    capa_results: Annotated[List[CapaResult], operator.add]

    # LLM reasoning output
    reasoning: Optional[ReasoningOutput]

    # Final result fields
    judgment: Optional[bool]
    recommended_machine: Optional[str]
    selection_reason: Optional[str]
    capa_summary: Optional[List[dict]]
    alternative_scenarios: Optional[list]
    multi_machine_plan: Optional[dict]

    # Execution metadata
    retry_count: int
    human_in_loop: bool
    error: Optional[str]
    langgraph_state: str

    # Per-step execution traces — accumulated across all nodes (including parallel Send nodes)
    step_traces: Annotated[List[dict], operator.add]

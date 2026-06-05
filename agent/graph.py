from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from .state import CapaAgentState
from .nodes import (
    mes_schedule_node,
    mold_status_node,
    calculate_capa_node,
    reasoning_node,
    result_node,
)


def route_to_capa_calculation(state: CapaAgentState):
    """Fan-out: returns a Send per machine for parallel CAPA calculation (step-003-a/b/c)."""
    mes_schedule = state["mes_schedule"]
    mold_status = state["mold_status"]

    sends = []
    for machine in mes_schedule:
        machine_id = machine["machine_id"]
        mold_change = next(
            c for c in mold_status["change_time_by_machine"]
            if c["machine_id"] == machine_id
        )
        sends.append(
            Send(
                "calculate_capa_node",
                {
                    "machine_id": machine_id,
                    "daily_working_hours": machine["daily_working_hours"],
                    "uph": machine["uph"],
                    "scheduled_periods": machine["scheduled_periods"],
                    "current_mold": mold_change["current_mold"],
                    "target_mold_id": mold_status["mold_id"],
                    "change_time_min": mold_change["time_min"],
                    "usage_count": mold_status["usage_count"],
                    "max_usage_count": mold_status["max_usage_count"],
                    "cavity_count": mold_status["cavity_count"],
                    "yield_rate": mold_status["yield_rate"],
                    "deadline": state["deadline"],
                    "today": state["today"],
                    "required_quantity": state["required_quantity"],
                },
            )
        )
    return sends


def build_graph():
    g = StateGraph(CapaAgentState)

    g.add_node("mes_schedule_node", mes_schedule_node)
    g.add_node("mold_status_node", mold_status_node)
    g.add_node("calculate_capa_node", calculate_capa_node)
    g.add_node("reasoning_node", reasoning_node)
    g.add_node("result_node", result_node)

    # Sequential edges
    g.add_edge(START, "mes_schedule_node")
    g.add_edge("mes_schedule_node", "mold_status_node")

    # Parallel fan-out via Send; after all complete → reasoning_node
    g.add_conditional_edges("mold_status_node", route_to_capa_calculation)
    g.add_edge("calculate_capa_node", "reasoning_node")

    g.add_edge("reasoning_node", "result_node")
    g.add_edge("result_node", END)

    return g.compile()


capa_graph = build_graph()

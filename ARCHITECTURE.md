# CAPA Agent 시스템 구조

## 전체 컴포넌트

```
사용자
  │  HTTP
  ▼
[UI]  :80
  │  POST /agent/capa
  ▼
[Agent]  :8000
  ├─ [LLM Server (llama-cpp)]  :8080  ← 같은 컨테이너, 내부 전용
  │     EXAONE-3.5-2.4B GGUF
  │
  ├─ GET /mes/*  →  [MES API]  :8001  →  [MariaDB]  :3306 (내부)
  │
  └─ trajectory 저장  →  [MariaDB]  :3306 (내부)

[Scheduler]  →  [MariaDB]  :3306 (내부)
  매 1시간, current_load 랜덤 업데이트

호스트(PC)에서 DB 직접 접근:  localhost:3307  →  MariaDB:3306
```

---

## 컨테이너별 역할

| 컨테이너 | 이미지/빌드 | 포트 | 역할 |
|---|---|---|---|
| `capa-ui` | `./ui` | 80 | 정적 HTML 폼, Agent API 호출 |
| `capa-agent` | `./agent` | 8000 (외부), 8080 (내부) | FastAPI 진입점 + LLM 서버 동시 기동 |
| `capa-mes-api` | `./mes-api` | 8001 | MES Mock API (DB 조회 + CAPA 계산) |
| `capa-scheduler` | `./scheduler` | - | schedules 테이블 부하율 갱신 |
| `capa-mariadb` | `mariadb:11` | 3306 (내부) / 3307 (호스트) | 영속 데이터 |

> **포트 정리**: 컨테이너끼리는 3306 사용. 3307은 호스트에서 DBeaver 등으로 직접 접근할 때만 사용.

### Agent 컨테이너 기동 순서 (`start.sh`)

```
uvicorn llm_server:app --port 8080 &   # 1. LLM 서버 백그라운드 기동
  └─ /health 응답 대기
uvicorn main:app --port 8000           # 2. Agent API 포그라운드 기동
```

---

## Agent 내부 실행 흐름

```
POST /agent/capa
  │
  ▼
run_agent()
  │
  ├─ [1] Goal 설정
  │     order_id, product_code, quantity, deadline, priority
  │     parse_scenario() → A / B / C / ERROR
  │
  ├─ [2] Plan
  │     tool_sequence = [get_capacity, get_mold_info, get_schedule, get_competing_orders]
  │     save_trajectory() → DB INSERT (초기 상태)
  │
  ├─ [3] Action — 순서 고정, 코드에서 직접 호출 (LLM 툴 선택 없음)
  │     for tool_name in tool_seq:
  │       call_tool() → MES API HTTP GET
  │       _update_state() → trajectory["state"] 갱신
  │       trajectory["action"] 누적
  │
  ├─ [4] Result 생성
  │     feasible = (state["capa_gap"] >= 0)  ← 코드에서 확정
  │     LLM 호출 → summary + alternatives 텍스트만 생성
  │
  └─ finally: save_trajectory() → DB UPDATE
```

### 에러 경로

```
order_id가 ORD-A/B/C 아님
  └─ parse_scenario() == "ERROR"
       └─ _handle_recovery() 즉시 호출 (툴 루프 진입 안 함)

툴 실행 중 MES API 500 / timeout
  └─ call_result["status"] in ("error", "timeout")
       └─ _handle_recovery() 호출 → 남은 툴 전부 건너뜀

_handle_recovery()
  ├─ get_recent_avg_cap() → schedules 테이블 전체 평균으로 간이 추정
  ├─ trajectory["recovery"] 채움
  ├─ result.feasible = null, 신뢰도 = 낮음
  └─ plan.replanned = true
```

---

## 시나리오 분기

| order_id 패턴 | 시나리오 | current_load 범위 | 특징 |
|---|---|---|---|
| `ORD-A-*` | A | 0.20 ~ 0.55 | CAPA 여유 |
| `ORD-B-*` | B | 0.75 ~ 0.95 | CAPA 부족, 대안 생성 |
| `ORD-C-*` | C | 0.40 ~ 0.65 | 경합 수주 존재, 총합 차감 후 판단 |
| 그 외 | ERROR | - | 툴 호출 없이 recovery |

시나리오 C CAPA 계산 (초기 데이터 기준, load=0.50):
```
get_capacity  → INJ-01: 8200×0.50×10 = 41,000 / INJ-02: 6400×0.50×10 = 32,000
                가용 합계 = 73,000

get_competing_orders → ORD-C-0921: 30,000 + ORD-C-0930: 40,000 = 총 70,000 차감

실질 가용 CAPA = 73,000 - 70,000 = 3,000
→ 요청 수량 > 3,000이면 feasible = false
```

---

## DB 테이블

| 테이블 | 갱신 주체 | 용도 |
|---|---|---|
| `machines` | 고정 (init.sql) | 사출기 스펙 (daily_cap, tons, cycle_sec) |
| `schedules` | Scheduler (1시간) | 시나리오별 current_load, available_days |
| `molds` | 고정 (init.sql) | 금형 수명, 셋업 시간, 상태 |
| `competing_orders` | 고정 (init.sql) | 시나리오 C 경합 수주 목록 (order_id, quantity, deadline, priority) |
| `trajectories` | Agent | 실행 기록 전체 (goal~recovery JSON) |

### Trajectory 구조

```json
{
  "goal":     { "order_id", "product_code", "quantity", "deadline", "scenario_type", "priority" },
  "plan":     { "strategy", "tool_sequence", "replanned", "replan_reason" },
  "action":   [ { "step", "tool_name", "parameters", "raw_response", "status", "latency_ms" } ],
  "state":    { "available_capa", "required_capa", "capa_gap", "feasible", "bottleneck",
                "competing_orders", "mold_setup_hours", "material_shortage" },
  "result":   { "feasible", "summary", "alternatives" },
  "recovery": { "triggered", "failed_action", "fallback_data", "recovery_note" }
}
```

---

## 툴 목록

| 툴 | 엔드포인트 | 파라미터 | 반환 핵심값 |
|---|---|---|---|
| `get_capacity` | `GET /mes/capacity` | `order_id` | 사출기별 available_cap (daily_cap × (1-load) × days) |
| `get_mold_info` | `GET /mes/mold` | `order_id`, `product_code` | 금형 수명(usage_pct), 셋업 시간(setup_hours), 상태 |
| `get_schedule` | `GET /mes/schedule` | `order_id` | 가동 가능 일수, blocked_dates |
| `get_competing_orders` | `GET /mes/competing-orders` | `order_id` | 경합 수주 목록 (order_id, quantity, deadline, priority) |

호출 순서 고정: `get_capacity → get_mold_info → get_schedule → get_competing_orders`

`capa_gap` 계산 순서:
```
get_capacity       → capa_gap = 가용 CAPA 합계 - 요청 수량
get_competing_orders → capa_gap -= 경합 수주 총합 수량
result 생성        → feasible = (capa_gap >= 0)  ← LLM 아닌 코드에서 확정
```

---

## LLM 서버

- 모델: EXAONE-3.5-2.4B-Instruct Q4_K_M (GGUF)
- 런타임: llama-cpp-python
- OpenAI 호환 `/v1/chat/completions` 엔드포인트 자체 구현
- tool_choice 처리: 시스템 프롬프트에 tool 정의 주입 → 응답 JSON에서 `{"tool_call": {...}}` 파싱
- **툴 호출에는 사용하지 않음** — result 생성(summary, alternatives 텍스트)에만 사용
- feasible 판단은 코드에서 수치로 확정 후 LLM에 전달

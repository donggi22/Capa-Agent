# CAPA Agent — LangGraph + EXAONE + Docker

## 프로젝트 구조

```
capa_agent/
├── agent/
│   ├── state.py          ← CapaAgentState TypedDict (operator.add 병렬 accumulator)
│   ├── tools.py          ← MES API HTTP 호출 (httpx async)
│   ├── nodes.py          ← LangGraph 노드 5개 + Python 기반 기계 선정 로직
│   └── graph.py          ← LangGraph 그래프 + Send 팬아웃
├── api/
│   └── main.py           ← FastAPI: CAPA 분석 + trajectory 저장 엔드포인트
├── mock_mes/
│   └── main.py           ← Mock MES API (MariaDB 조회)
├── static/
│   └── index.html        ← 분석 UI (제품 선택, 결과 표시, trajectory 저장)
├── db/
│   ├── init.sql          ← 스키마 + 5개 제품 × 10개 기계 픽스처
│   └── add_machines.sql  ← MCH-04~10 추가 마이그레이션
├── Dockerfile            ← Agent 이미지 (aiomysql 포함)
├── Dockerfile.mes        ← Mock MES 이미지 (aiomysql 포함)
└── docker-compose.yaml
```

## 서비스 구성

| 서비스 | 이미지 | 포트 | 역할 |
|---|---|---|---|
| `db` | `mariadb:11` | 3306 | 제품/기계/금형 데이터 + trajectory 이력 |
| `mes-api` | `mes-api:latest` | 8001 | Mock MES API (DB 조회) |
| `agent` | `capa-agent:latest` | 8002 (host) → 8000 (container) | LangGraph Agent + FastAPI |
| `llm` | `vllm/vllm-openai:v0.9.2` | 8080 | EXAONE-3.5-7.8B-AWQ 서빙 |

기동 순서: `db` → `mes-api` → `agent` → `llm` (health check 의존)

## DB 스키마

```
schedules          제품×기계별 daily_working_hours / uph / 현재 작업
scheduled_periods  기계별 스케줄 기간 (from~to)
molds              제품별 금형 정보 (usage_count / max / cavity)
mold_change_times  제품×기계별 장착 금형 / 교체 소요시간
trajectories       저장된 분석 결과 + full LangGraph state (JSON)
```

## LangGraph 실행 흐름

```
START
  → mes_schedule_node    (MES API: 제품별 10개 기계 생산계획 조회)
  → mold_status_node     (MES API: 금형 상태 조회)
  → [Send × 10 machines] (병렬 CAPA 계산)
      calculate_capa_node (MCH-01)  ┐
      calculate_capa_node (MCH-02)  │
      ...                           ├─ operator.add로 결과 집계
      calculate_capa_node (MCH-10)  ┘
  → reasoning_node       (Python 우선순위 정렬 → EXAONE LLM 추천 이유 생성)
  → result_node          (데이터 기반 judgment 확정 + capa_summary 구성)
END
```

## 기계 선정 로직 (reasoning_node)

LLM에게 판단을 맡기지 않고 Python에서 정렬 후 LLM은 설명만 담당.

```
우선순위 정렬 (Python):
  ① sufficient=true 우선
  ② available_capacity 내림차순
  ③ 금형 교체 불필요 우선
  ④ change_time_min 오름차순
```

`judgment`(생산 가능 여부)는 추천 기계의 `sufficient` 값을 데이터에서 직접 읽음.

## 제품 코드

| 코드 | 형상 | 특징 |
|---|---|---|
| PROD-A01 | 삼각형 | 범용 — 기본 시나리오 |
| PROD-B01 | 정사각형 | 신금형(MOLD-03), 고UPH, MCH-03/04/09 여유 |
| PROD-C01 | 직사각형 | 저UPH, 금형 교체 오래 걸림 — CAPA 빠듯 |
| PROD-D01 | 원 | 고UPH, 납기 여유, MCH-04/07/10 즉시 가동 |
| PROD-E01 | 별표 | 금형 노후(14200/15000) — LLM 리스크 설명 |

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/` | UI (index.html) |
| `GET` | `/health` | 헬스체크 |
| `POST` | `/api/v1/capa` | CAPA 분석 실행 → full_state 포함 응답 |
| `POST` | `/api/v1/trajectories/save` | 분석 결과 DB 저장 |

## 실행 방법

> **사전 조건**: `nvidia-container-toolkit` 설치 필요

첫 실행 (이미지 빌드 포함):
```
docker compose up --build
```

코드 수정 후 재시작 불필요 (`--reload` 적용):
- `agent/`, `api/`, `static/` 수정 → agent 컨테이너 자동 리로드
- `mock_mes/` 수정 → mes-api 컨테이너 자동 리로드

UI 접속: http://localhost:8002

API 직접 호출:
```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/capa `
  -ContentType "application/json" `
  -Body '{"product_code":"PROD-A01","required_quantity":10000,"deadline":"2026-06-20"}'
```

## Trajectory 학습 데이터 수집 가이드

### 스키마 기준
traj-3 이후 버전이 canonical schema. 주요 필드:
- `reasoning.selection_reason` — rule-based 생성 (수치 정확 보장)
- `reasoning.llm_narrative` — LLM 생성, `llm_narrative_training_use: false` (검수 후 활용)
- `reasoning.judgment_basis` — multi_machine/infeasible 케이스 필수
- `step_003.instances.*.output.calculation_trace` — CoT 학습용 중간 계산식

### 시나리오별 수집 방법

| 시나리오 | 파라미터 예시 | judgment |
|---|---|---|
| 단일 설비 충분 (happy path) | `required_quantity: 5000, deadline: +20일` | true |
| 다중 설비 조합 | `required_quantity: 200000, deadline: +20일` | true |
| 완전 불가 (납기 초과) | `required_quantity: 9999999, deadline: +3일` | false |
| 금형 노후 리스크 | `product_code: PROD-E01` | true/false |
| 저UPH 빠듯 | `product_code: PROD-C01, required_quantity: 50000` | true/false |

### 불가 판정 케이스 예시 호출
```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/capa `
  -ContentType "application/json" `
  -Body '{"product_code":"PROD-C01","required_quantity":9999999,"deadline":"2026-06-10"}'
```

## 주요 설계 포인트

- **LLM 역할 최소화**: 기계 선정·판정은 Python 코드가 담당, LLM은 추천 이유 자연어 생성만 수행
- **병렬 실행**: LangGraph `Send` API로 10개 기계 CAPA를 동시 계산, `operator.add` reducer로 자동 집계
- **필요 생산일수**: `days_needed` = 실제 required_quantity를 충족하는 최소 일수 (납기일까지 전체 기간 아님)
- **Trajectory 저장**: UI에서 결과 확인 후 선택적으로 DB 저장, full LangGraph state를 JSON으로 보관
- **Hot Reload**: 소스 디렉토리를 볼륨 마운트 + uvicorn `--reload`로 재빌드 없이 개발
- **vLLM**: `--served-model-name` 으로 short name 등록, `--quantization=awq`, RTX 4070Ti 12GB 기준 `--gpu-memory-utilization=0.85`

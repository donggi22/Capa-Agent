# CAPA Agent — LangGraph + EXAONE + Docker

## 프로젝트 구조

```
capa_agent/
├── agent/
│   ├── state.py          ← CapaAgentState TypedDict (operator.add 병렬 accumulator)
│   ├── tools.py          ← MES API HTTP 호출 (httpx async)
│   ├── nodes.py          ← LangGraph 노드 5개 + LoRA ReAct 루프 (call_lora_full_trajectory)
│   └── graph.py          ← LangGraph 그래프 + Send 팬아웃
├── api/
│   └── main.py           ← FastAPI: CAPA 분석 / trajectory 저장 / 스케줄 추가 엔드포인트
├── mock_mes/
│   └── main.py           ← Mock MES API (MariaDB 조회)
├── static/
│   └── index.html        ← 분석 UI (제품 선택, 결과 표시, trajectory 저장)
├── db/
│   ├── init.sql          ← 스키마 + 3개 제품 × 3개 기계 픽스처
│   ├── add_machines.sql
│   ├── migrate_mold_change.sql
│   └── add_yield_rate.sql
├── Dockerfile            ← Agent 이미지
├── Dockerfile.mes        ← Mock MES 이미지
├── Dockerfile.llm-1.2b   ← vLLM + LoRA 이미지
└── docker-compose.yaml
```

## 서비스 구성

| 서비스 | 포트 (호스트→컨테이너) | 역할 |
|---|---|---|
| `db` | 3306 | MariaDB 11 — 제품/기계/금형 데이터 + trajectory 이력 |
| `llm-1.2b` | 8083→8080 | vLLM — EXAONE-4.0-1.2B (base) + LoRA v3 (finetuned) 서빙 |
| `mes-api` | 8001 | Mock MES API (DB 조회) |
| `agent` | 8002→8000 | LangGraph Agent + FastAPI |

기동 순서: `db` → `mes-api` → `llm-1.2b` → `agent` (healthcheck 의존)

## DB 스키마

```
machines           설비 기본 파라미터 (daily_working_hours)
machine_product_uph 설비×제품별 UPH
scheduled_periods  설비별 가동 스케줄 기간 (from~to)
molds              제품별 금형 정보 (usage_count / max / cavity / yield_rate)
mold_change_times  금형 조합별 교체 소요시간 (분)
trajectories       분석 결과 + full LangGraph state (JSON)
```

## 마스터 데이터

**설비 3대 (MCH-01 ~ MCH-03)**

| machine_id | daily_working_hours | current_mold |
|---|---|---|
| MCH-01 | 8h | MOLD-02 |
| MCH-02 | 16h | MOLD-01 |
| MCH-03 | 24h (3교대) | MOLD-03 |

**제품 3종**

| product_code | 금형 | usage / max | cavity | yield_rate | 특징 |
|---|---|---|---|---|---|
| PROD-A01 | MOLD-01 | 12400 / 15000 | 4 | 0.950 | 기본 시나리오, 금형 잔여수명 빠듯 |
| PROD-B01 | MOLD-02 | 2000 / 20000 | 8 | 0.970 | 신금형, 고UPH |
| PROD-C01 | MOLD-03 | 8500 / 12000 | 2 | 0.910 | 저cavity, 수율 낮음 — CAPA 빠듯 |

**설비×제품별 UPH**

| | PROD-A01 | PROD-B01 | PROD-C01 |
|---|---|---|---|
| MCH-01 | 267 | 320 | 150 |
| MCH-02 | 300 | 380 | 180 |
| MCH-03 | 300 | 420 | 200 |

## LangGraph 실행 흐름

```
START
  → mes_schedule_node    (step-001: MES API /schedule — 설비별 가동 일정 조회)
  → mold_status_node     (step-002: MES API /mold — 금형 상태·교체시간 조회)
  → [Send × 3 machines]  (route_to_capa_calculation — 팬아웃)
      get_machine_capa (MCH-01) ┐
      get_machine_capa (MCH-02) ├─ operator.add로 capa_results 자동 집계
      get_machine_capa (MCH-03) ┘
  → reasoning_node       (step-004: Python 우선순위 정렬 → LLM 추천 이유 생성)
  → result_node          (step-005: 최종 판정 확정 + capa_summary 구성)
END
```

## 기계 선정 로직 (reasoning_node)

판정과 기계 선정은 Python 코드가 담당하고, LLM은 추천 이유 자연어 생성만 수행.

```
우선순위 정렬 (Python):
  ① sufficient=True 우선
  ② available_capacity 내림차순
  ③ 금형 교체 불필요 우선
  ④ change_time_min 오름차순
```

`judgment`(생산 가능 여부)는 추천 기계의 `sufficient` 값을 데이터에서 직접 읽음.

## LLM 모델 모드

동일한 vLLM 서버에서 base 모델과 LoRA 어댑터를 함께 서빙.

| model_mode | 사용 모델 | 동작 |
|---|---|---|
| `original` (기본) | EXAONE-4.0-1.2B (base) | 상세 한국어 프롬프트 → 추천 이유 생성 |
| `lora` | finetuned (LoRA v3) | 간소화 포맷 프롬프트 → 추천 이유 생성 |
| `evaluate` | 파이프라인 + LoRA 병렬 | 파이프라인 실행과 동시에 LoRA ReAct 루프 실행 (asyncio.gather) |

**evaluate 모드의 LoRA ReAct 루프 (`call_lora_full_trajectory`)**

- 최대 8턴의 Action/Observation 루프
- 사용 가능한 Action: `get_mes_production_schedule`, `get_mold_status`, `get_machine_capa`
- `Final Answer:` 감지 시 루프 종료
- 타임아웃: 80초
- 결과는 응답의 `llm_trajectory` 필드로 반환

## Trajectory 스키마

`_build_trajectory()` 함수(api/main.py)가 step_traces로부터 조립.

```
goal      — 분석 목표
plan      — 실행 계획
actions   — 수행한 도구 호출 (step_traces 기반)
state     — LangGraph 상태 스냅샷
result    — 최종 판정 결과
recovery  — 예외·대안 처리
```

저장 위치:
- DB: `trajectories` 테이블 (`full_state` JSON 컬럼)
- 파일: `./trajectories/{trajectory_id}.json`

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/` | UI (index.html) |
| `GET` | `/health` | 헬스체크 |
| `POST` | `/api/v1/capa` | CAPA 분석 실행 (`model_mode`: original / lora / evaluate) |
| `POST` | `/api/v1/trajectories/save` | 분석 결과 DB 저장 |
| `POST` | `/api/v1/schedule/add` | 결과 DB 저장 + 파일 저장 + 스케줄 테이블 기록 |

## 실행 방법

> **사전 조건**: `nvidia-container-toolkit` 설치 필요

```
docker compose up --build
```

UI 접속: http://localhost:8002

API 직접 호출:
```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/capa `
  -ContentType "application/json" `
  -Body '{"product_code":"PROD-A01","required_quantity":10000,"deadline":"2026-07-20"}'
```

코드 수정 후 재시작 불필요 (`--reload` 적용):
- `agent/`, `api/`, `static/` → agent 컨테이너 자동 리로드
- `mock_mes/` → mes-api 컨테이너 자동 리로드

## 주요 설계 포인트

- **LLM 역할 최소화**: 기계 선정·판정은 Python 코드 담당, LLM은 추천 이유 자연어 생성만 수행
- **병렬 실행**: LangGraph `Send` API로 3개 기계 CAPA를 동시 계산, `operator.add` reducer로 자동 집계
- **Dual 모델 서빙**: 동일 vLLM 엔드포인트에서 base 모델과 LoRA 어댑터를 `--enable-lora`로 함께 서빙
- **evaluate 모드**: 파이프라인과 LoRA ReAct 루프를 `asyncio.gather`로 병렬 실행 → 두 결과 비교 가능
- **Trajectory 이중 저장**: DB(`full_state` JSON) + 파일 시스템(`.json`) 동시 보관
- **Hot Reload**: 소스 디렉토리 볼륨 마운트 + uvicorn `--reload`로 재빌드 없이 개발

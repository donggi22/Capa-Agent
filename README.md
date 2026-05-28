# 생산 CAPA Agent

사출성형 도메인 생산 CAPA 판단 단위 에이전트

---

## 구성

```
capa-agent/
├── mariadb/          MariaDB 초기화 SQL
├── mes-api/          MES Mock API 서버 (FastAPI)
├── scheduler/        MES 데이터 자동 업데이트 (1시간 주기)
├── agent/            생산 CAPA 판단 에이전트 (EXAONE 4.5 포함)
├── ui/               호출 UI (HTML)
├── k8s/              Kubernetes 배포 yaml
└── docker-compose.yml
```

---

## 실행 방법

### 1단계 — 로컬 검증 (docker-compose)

```bash
docker compose up --build
```

- UI:      http://localhost
- Agent:   http://localhost:8000
- MES API: http://localhost:8001

### 2단계 — Kubernetes 검증 (k3d)

```bash
# 클러스터 생성
k3d cluster create capa-cluster --port 8080:80@loadbalancer

# 이미지 k3d에 로드
k3d image import capa-mes-api:latest capa-scheduler:latest capa-agent:latest capa-ui:latest -c capa-cluster

# 배포
kubectl apply -f k8s/
kubectl get pods
```

---

## 시나리오별 테스트

| 시나리오 | order_id 예시   | 기대 결과                         |
|---------|----------------|----------------------------------|
| A       | ORD-A-0001     | feasible=true, alternatives=null |
| B       | ORD-B-0001     | feasible=false, alternatives 2개 이상 |
| C       | ORD-C-0001     | 경합 수주 인식 + 우선순위 판단      |
| ERROR   | ORD-X-9999     | recovery 발동, 간이 추정 결과      |

---

## 주의사항

- agent 컨테이너 빌드 시 EXAONE 4.5 2.4B 모델 파일 다운로드 (약 1.5GB)
- 최초 빌드 시간 15~30분 소요 예상 (네트워크 환경에 따라 다름)
- 모델 추론은 CPU 기반 (내장 GPU 미사용)
- 실제 MES API 연동 시 환경변수 MES_API_URL만 교체하면 됨

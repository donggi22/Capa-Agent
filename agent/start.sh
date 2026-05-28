#!/bin/bash
set -e

echo "[START] LLM 서버 시작..."
uvicorn llm_server:app --host 0.0.0.0 --port 8080 &

echo "[START] LLM 서버 준비 대기..."
until curl -s http://localhost:8080/health > /dev/null 2>&1; do
  sleep 3
done
echo "[START] LLM 서버 준비 완료"

echo "[START] Agent 서버 시작..."
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
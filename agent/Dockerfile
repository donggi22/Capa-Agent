FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    build-essential curl git cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# 0.3.x Q4_K repack 버그 회피 → 0.2.x 고정, AVX2 명시
RUN CMAKE_ARGS="-DLLAMA_AVX2=on -DLLAMA_AVX=on -DLLAMA_F16C=on -DLLAMA_FMA=on" \
    pip install --no-cache-dir "llama-cpp-python==0.2.90"

RUN pip install --no-cache-dir -r requirements.txt

COPY schema.py tools.py db.py agent.py main.py llm_server.py ./
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8000
CMD ["./start.sh"]

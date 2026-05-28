from fastapi import FastAPI
from pydantic import BaseModel
from llama_cpp import Llama
from typing import Union
import json
import re
import os

app = FastAPI()

GGUF_PATH = os.getenv("GGUF_MODEL_PATH", "/model-gguf/model.gguf")
N_THREADS = int(os.getenv("OMP_NUM_THREADS", "8"))

llm = Llama(
    model_path=GGUF_PATH,
    n_ctx=4096,
    n_threads=N_THREADS,
    n_batch=512,
    verbose=False,
)

class ChatRequest(BaseModel):
    model: str
    messages: list
    tools: list = []
    tool_choice: Union[str, dict] = "auto"
    max_tokens: int = 1000

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/chat/completions")
def chat(req: ChatRequest):
    messages = req.messages.copy()
    if req.tools:
        tool_desc = json.dumps(req.tools, ensure_ascii=False)

        forced_tool = None
        if isinstance(req.tool_choice, dict) and req.tool_choice.get("type") == "function":
            forced_tool = req.tool_choice["function"]["name"]

        force_msg = f'\n반드시 "{forced_tool}" Tool을 호출하세요.' if forced_tool else ""
        messages[0]["content"] += (
            f"\n\n사용 가능한 Tool:\n{tool_desc}"
            f"{force_msg}"
            "\nTool 호출 시 반드시 JSON 형식으로만 응답하세요: "
            "{\"tool_call\": {\"name\": \"함수명\", \"arguments\": {...}}}"
        )

    resp = llm.create_chat_completion(
        messages=messages,
        max_tokens=req.max_tokens,
        temperature=0.0,
    )

    text = (resp["choices"][0]["message"].get("content") or "").strip()

    # tool call 파싱 — 브라켓 카운팅으로 중첩 JSON 탐색
    tool_calls = None
    clean = re.sub(r'```(?:json)?\s*', '', text)
    clean = re.sub(r'```', '', clean).strip()
    depth, start = 0, None
    for i, ch in enumerate(clean):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(clean[start:i + 1])
                    if "tool_call" in parsed:
                        tc = parsed["tool_call"]
                        tool_calls = [{
                            "id": "call_0",
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": json.dumps(tc.get("arguments", {}))
                            }
                        }]
                        text = ""
                        break
                except Exception:
                    pass
                start = None

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls
            },
            "finish_reason": "stop"
        }]
    }

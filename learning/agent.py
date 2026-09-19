import asyncio
import os
import requests
from dotenv import load_dotenv
from tool import bash_tool, bash, read_file, read_file_tool
import json , uuid
from pathlib import Path
from transcripts import Transcript
import sys

TOOLS = [bash_tool, read_file_tool]
DISPATCH = {"bash": bash, "read_file" : read_file}

load_dotenv()

MODEL = "openai/gpt-oss-120b"

RETRYABLE = {429, 500, 502, 503, 529}


async def modelRequest(messages: list[dict], tools: list[dict] | None = None, retries: int = 4) -> dict:
    """POST one chat completion to Groq and return the parsed JSON body."""
    body = {
        "model": MODEL,
        "messages": messages,
        "reasoning_effort": "high",
        "temperature": 0,
        "top_p": 1,
        "max_completion_tokens": 4096,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    for attempt in range(retries):
        # ponytail: requests is sync, run in a thread; swap for httpx if streaming needed
        resp = await asyncio.to_thread(
            requests.post,
            url="https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ['groqKey']}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=300
        )

        if resp.status_code in RETRYABLE and attempt < retries - 1:
            wait = float(resp.headers.get("retry-after", 2 ** attempt))
            print(f"[{resp.status_code}, retrying in {wait}s]")
            await asyncio.sleep(wait)
            continue

        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code}: {resp.text}", response=resp)

        return resp.json()


def cleanMsg(msg: dict) -> dict:
    """Strip provider-specific extras; keep only what the API accepts back."""
    out = {"role": "assistant", "content": msg.get("content")}
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    return out


def toolRun(calls, history, hops_left, dispatch):
    for call in calls:
        name = call["function"]["name"]
        fn = dispatch.get(name)
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            print(f"Tool Run: [{name}] {args}")
            result = fn(**args) if fn else f"unknown tool: {name}"
            print("Tool Run Output: ", result, "\n")
        except Exception as e:
            result = f"error: {e}"

        history.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": f"{result}\n\n" + (
                "[tool budget exhausted — answer now with what you have so far]"
                if hops_left == 0 else
                f"[{hops_left} tool iterations left this turn]"
            ),
        })


async def modelTurns(history: list[dict], dispatch: dict, max_hops: int = 5) -> str:
    reply = await modelRequest(history, tools=TOOLS)
    msg = reply["choices"][0]["message"]
    history.append(cleanMsg(msg))

    for i in range(max_hops):
        calls = msg.get("tool_calls")
        if not calls:
            return msg["content"]

        toolRun(calls, history, hops_left=max_hops - i - 1, dispatch=dispatch)

        reply = await modelRequest(history, tools=TOOLS)
        msg = reply["choices"][0]["message"]
        history.append(cleanMsg(msg))

    # budget spent; model may still ask for tools. Refuse to run them, ask once
    # more for text. Tools stay in the request so the API never rejects a call.
    if msg.get("tool_calls"):
        for call in msg["tool_calls"]:
            history.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "[tool budget exhausted — answer now with what you have so far]",
            })
        reply = await modelRequest(history, tools=TOOLS)
        msg = reply["choices"][0]["message"]
        history.append(cleanMsg(msg))

    return msg["content"] or "[no answer: tool budget exhausted]"

SYSTEM = (
    "You are a coding agent working inside a git repository. Your job is to "
    "complete the user's task, not to explore the repository.\n\n"
    "Rules:\n"
    "- If the task is genuinely ambiguous, ask one short question. Otherwise act."
)


async def main():
    SESSIONS = Path(__file__).parent / "sessions"
    SESSIONS.mkdir(parents=True, exist_ok=True)

    resume_id = None
    if "--resume" in sys.argv:
        i = sys.argv.index("--resume")
        if i + 1 < len(sys.argv):
            resume_id = sys.argv[i + 1]
        else:
            sys.exit("--resume needs a session id")

    if resume_id:
        path = SESSIONS / f"{resume_id}.jsonl"
        if not path.exists():
            sys.exit(f"no such session: {resume_id}")
        session_id = resume_id
        history = Transcript(path)
        history.load()
        print(f"[resumed {session_id}, {len(history)} messages]")
    else:
        session_id = uuid.uuid4()
        history = Transcript(SESSIONS / f"{session_id}.jsonl")
        history.append({"role": "system", "content": SYSTEM})
        print(f"[session {session_id}]")


    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line.strip().lower() in ("quit", "exit"):
            print(f"resume this session by python3 agent.py --resume {session_id}")
            break

        mark = len(history)
        history.append({"role": "user", "content": line})
        try:
            reply = await modelTurns(history, dispatch=DISPATCH)
        except requests.HTTPError as e:
            print(f"\n[groq error: {e}]")
            del history[mark:]
            continue


        print(reply)


if __name__ == "__main__":
    asyncio.run(main())
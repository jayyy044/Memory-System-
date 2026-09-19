import json

class Transcript(list):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.f = path.open("a", buffering=1)

    def append(self, msg):
        super().append(msg)
        self.f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def load(self):
        for line in self.path.read_text().splitlines():
            if line.strip():
                list.append(self, json.loads(line))

        # session died mid-tool-call: the last assistant message asked for tools
        # and some results never got written. Answer them so the model is told,
        # not left to guess whether they ran. Only the tail can be dangling —
        # the harness writes call then result, so a gap is always at the end.
        last = next((i for i in range(len(self) - 1, -1, -1)
                     if self[i]["role"] == "assistant" and self[i].get("tool_calls")), None)
        if last is None:
            return
        answered = {m["tool_call_id"] for m in self[last + 1:] if m["role"] == "tool"}
        for call in self[last]["tool_calls"]:
            if call["id"] not in answered:
                self.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "[interrupted: this call never completed — the session died before it returned]",
                })

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


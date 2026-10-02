"""과제 상태와 이벤트를 디스크에 둔다. 세션이 바뀌거나 모델이 바뀌어도 인계 기준은 이 파일들이다."""

import json
import time
import uuid
from pathlib import Path


class Ledger:
    def __init__(self, home: Path):
        self.home = Path(home)
        (self.home / "tasks").mkdir(parents=True, exist_ok=True)

    def task_dir(self, tid: str) -> Path:
        return self.home / "tasks" / tid

    def new_task(self, **fields) -> dict:
        tid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        task = {
            "id": tid, "status": "pending", "stage": "baseline", "items": [], "sessions": {},
            "iterations": 0, "review_rounds": 0, "tamper_count": 0, "failure_sigs": [],
            "feedback": [], **fields,
        }
        self.task_dir(tid).mkdir(parents=True)
        self.save(task)
        return task

    def load(self, tid: str) -> dict:
        return json.loads((self.task_dir(tid) / "task.json").read_text())

    def save(self, task: dict) -> None:
        path = self.task_dir(task["id"]) / "task.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2))
        tmp.replace(path)

    def all(self) -> list[dict]:
        return [self.load(p.name) for p in sorted((self.home / "tasks").iterdir()) if (p / "task.json").exists()]

    def event(self, task: dict, kind: str, **data) -> None:
        line = {"ts": time.time(), "task": task["id"], "kind": kind, **data}
        with open(self.task_dir(task["id"]) / "events.jsonl", "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def events(self):
        for d in sorted((self.home / "tasks").iterdir()):
            p = d / "events.jsonl"
            if p.exists():
                for line in p.read_text().splitlines():
                    yield json.loads(line)

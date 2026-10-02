"""결정적 검증. 에이전트의 "다 했다"는 믿지 않고 이 결과만 믿는다."""

import hashlib
import re
import subprocess
from dataclasses import dataclass

FAIL_LINE = re.compile(r"(FAIL|ERROR|Error|Exception|assert)", re.IGNORECASE)


@dataclass
class Result:
    ok: bool
    output: str
    sig: str = ""


def run(cmd: str, cwd, timeout: int = 900) -> Result:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(False, f"$ {cmd}\nTIMEOUT after {timeout}s", "timeout")
    out = f"$ {cmd}\n{(p.stdout + p.stderr)[-8000:]}"
    return Result(p.returncode == 0, out, "" if p.returncode == 0 else failure_signature(out))


def failure_signature(output: str) -> str:
    """같은 실패가 반복되는지 보기 위한 지문. 숫자(시간·줄번호·주소)는 지워서 비교한다. 휴리스틱."""
    lines = sorted({re.sub(r"\d+", "N", l.strip()) for l in output.splitlines() if FAIL_LINE.search(l)})
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]


def run_suite(policy: dict, cwd) -> Result:
    v = policy["verify"]
    runs = [run(v["test"], cwd) for _ in range(max(1, v.get("flaky_runs", 1)))]
    if len({r.ok for r in runs}) > 1:
        return Result(False, "FLAKY: 같은 코드에서 결과가 갈림\n" + runs[-1].output, "flaky")
    results = [runs[0]] + [run(c, cwd) for c in v.get("checks", [])]
    failed = [r for r in results if not r.ok]
    if failed:
        return Result(False, "\n\n".join(r.output for r in failed), "+".join(r.sig for r in failed))
    return Result(True, "\n\n".join(r.output for r in results))


def run_mutation(policy: dict, cwd, changed: list[str]) -> str | None:
    cmd = policy["verify"].get("mutation", "")
    if not cmd:
        return None
    return run(cmd.replace("{changed}", ",".join(changed)), cwd, timeout=3600).output

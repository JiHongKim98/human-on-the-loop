"""Claude / Codex headless 어댑터. 설정은 전부 호출 플래그로 넘기고 ~/.claude, ~/.codex 설정 파일은 건드리지 않는다.

구독 요금제 전용: API 키 계열 환경변수가 자식 프로세스로 넘어가면 CLI가 종량 과금 인증으로 바뀔 수 있으므로
호출마다 지우고, 실행 전에 두 CLI가 구독 계정으로 로그인돼 있는지 확인한다.
"""

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

# 하나라도 있으면 구독 대신 API·클라우드 과금 경로로 인증될 수 있다
BILLED_AUTH_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "OPENAI_API_KEY", "CODEX_API_KEY",
)


class AgentError(Exception):
    pass


def subscription_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in BILLED_AUTH_ENV}


def check_subscription() -> None:
    """두 CLI가 구독 로그인 상태가 아니면 아무것도 실행하지 않고 멈춘다."""
    env = subscription_env()
    p = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, env=env)
    try:
        method = json.loads(p.stdout).get("authMethod")
    except json.JSONDecodeError:
        method = None
    if method != "claude.ai":
        raise SystemExit(f"claude가 구독(claude.ai) 로그인이 아니다: authMethod={method}. 실행하지 않는다.")
    p = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, env=env)
    if "ChatGPT" not in p.stdout + p.stderr:
        raise SystemExit("codex가 ChatGPT 로그인이 아니다. 실행하지 않는다.")


@dataclass
class Call:
    role: str
    prompt: str
    schema: dict
    cwd: Path
    workdir: Path            # schema·출력 임시 파일을 둘 곳(과제 디렉터리)
    write: bool = False
    session: str | None = None  # None: 무상태 1회성, "new": 이어갈 세션 시작, 그 외: 이어갈 세션 id
    model: str = ""             # 비우면 CLI 전역 기본값
    effort: str = ""


@dataclass
class Reply:
    data: dict
    session: str | None = None
    cost: float | None = None
    denials: int = 0


class ClaudeAgent:
    name = "claude"

    def __init__(self, cfg: dict, policy: dict):
        self.cfg = cfg
        self.budget = policy["budget"]
        self.supports_resume = cfg.get("resume", True)

    def run(self, call: Call) -> Reply:
        tools = self.cfg["write_tools" if call.write else "read_tools"]
        cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(call.schema),
               "--permission-mode", self.cfg["permission_mode"], "--allowedTools", ",".join(tools),
               "--max-budget-usd", str(self.budget["per_call_usd"])]
        if call.model:
            cmd += ["--model", call.model]
        if call.effort:
            cmd += ["--effort", call.effort]
        sid = None
        if call.session == "new":
            sid = str(uuid.uuid4())
            cmd += ["--session-id", sid]
        elif call.session:
            sid = call.session
            cmd += ["--resume", sid]
        else:
            cmd += ["--no-session-persistence"]
        cmd.append(call.prompt)

        p = _run(cmd, call.cwd, self.budget["call_timeout_sec"])
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise AgentError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-2000:]}")
        if out.get("is_error") or out.get("structured_output") is None:
            raise AgentError(f"claude {out.get('subtype')}: {str(out.get('result'))[-2000:]}")
        return Reply(out["structured_output"], out.get("session_id", sid),
                     out.get("total_cost_usd"), len(out.get("permission_denials") or []))


class CodexAgent:
    name = "codex"

    def __init__(self, cfg: dict, policy: dict):
        self.cfg = cfg
        self.budget = policy["budget"]
        self.supports_resume = cfg.get("resume", False)

    def run(self, call: Call) -> Reply:
        schema_file = call.workdir / f"{call.role}.schema.json"
        out_file = call.workdir / f"{call.role}.out.json"
        schema_file.write_text(json.dumps(call.schema))
        out_file.unlink(missing_ok=True)
        sandbox = "workspace-write" if call.write else "read-only"
        common = ["--json", "--output-schema", str(schema_file), "-o", str(out_file),
                  "-c", 'approval_policy="never"']
        if call.model:
            common += ["-m", call.model]
        if call.effort:
            common += ["-c", f'model_reasoning_effort="{call.effort}"']

        if call.session and call.session != "new":
            # 미검증: resume은 -s/-C를 받지 않아 config override로 sandbox를 넘긴다
            cmd = ["codex", "exec", "resume", *common, "-c", f'sandbox_mode="{sandbox}"',
                   call.session, call.prompt]
        else:
            cmd = ["codex", "exec", *common, "-s", sandbox, "-C", str(call.cwd)]
            if call.session is None:
                cmd.append("--ephemeral")
            cmd.append(call.prompt)

        p = _run(cmd, call.cwd, self.budget["call_timeout_sec"])
        if p.returncode != 0 or not out_file.exists():
            raise AgentError(f"codex exit {p.returncode}: {(p.stderr or p.stdout)[-2000:]}")
        try:
            data = json.loads(out_file.read_text())
        except json.JSONDecodeError:
            raise AgentError(f"codex 출력이 JSON이 아님: {out_file.read_text()[-2000:]}")
        sid = _find_session_id(p.stdout) if call.session == "new" else call.session
        return Reply(data, sid)


def _run(cmd, cwd, timeout) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=subscription_env())
    except subprocess.TimeoutExpired:
        raise AgentError(f"{cmd[0]} timeout after {timeout}s")


def _find_session_id(jsonl: str) -> str | None:
    """미검증: codex --json 이벤트에서 세션 id 필드 이름을 확인하지 않았다. 후보 키를 차례로 찾는다."""
    def walk(o):
        if isinstance(o, dict):
            for k in ("thread_id", "session_id", "conversation_id"):
                if isinstance(o.get(k), str):
                    return o[k]
            for v in o.values():
                if (r := walk(v)):
                    return r
        return None
    for line in jsonl.splitlines():
        try:
            if (sid := walk(json.loads(line))):
                return sid
        except json.JSONDecodeError:
            continue
    return None


def make_agents(policy: dict) -> dict:
    return {
        "claude": ClaudeAgent(policy["agents"]["claude"], policy),
        "codex": CodexAgent(policy["agents"]["codex"], policy),
    }

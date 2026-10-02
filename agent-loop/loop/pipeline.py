"""과제 하나를 단계별로 진행하는 상태 기계.

baseline → spec → (triage) → tests → implement ⇄ review → gate → done
각 단계는 끝날 때 task["stage"]를 다음 단계로 바꾼다. Blocked가 나면 그 자리에서 멈추고,
사람이 answer/approve/resume 하면 저장된 stage부터 다시 진행한다.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitops, policy as pol, prompts, schemas, verify
from .agents import AgentError, Call
from .ledger import Ledger


class Blocked(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class Loop:
    def __init__(self, policy: dict, ledger: Ledger, agents: dict):
        self.policy, self.ledger, self.agents = policy, ledger, agents
        roles = policy["roles"]
        for other in ("test_author", "reviewer"):
            if roles[other] == roles["implementer"]:
                raise ValueError(f"roles.{other}와 roles.implementer가 같은 모델이면 검증이 순환한다")

    # ── 진입점 ────────────────────────────────────────────────

    def decompose(self, repo: Path, agenda: str) -> list[dict]:
        tmp = self.ledger.home / "tmp"
        tmp.mkdir(exist_ok=True)
        agent = self.agents[self.policy["roles"]["decomposer"]]
        try:
            r = agent.run(Call("decomposer", prompts.decompose(agenda), schemas.DECOMPOSE, repo, tmp))
        except AgentError as e:
            raise SystemExit(f"decomposer 실패: {e}")
        return [self.ledger.new_task(repo=str(repo), agenda=agenda, **t) for t in r.data["tasks"]]

    def run(self, tid: str) -> dict:
        task = self.ledger.load(tid)
        task["status"] = "running"
        task.pop("blocked", None)
        try:
            while task["stage"] != "done":
                getattr(self, f"stage_{task['stage']}")(task)
                self.ledger.save(task)
            task["status"] = "done"
            self.ledger.event(task, "done")
        except Blocked as b:
            task["status"] = "blocked"
            task["blocked"] = {"reason": b.reason, "detail": b.detail[-4000:], "stage": task["stage"]}
            self.ledger.event(task, "blocked", reason=b.reason, stage=task["stage"])
        self.ledger.save(task)
        return task

    # ── 공통 ──────────────────────────────────────────────────

    def call(self, task, role, prompt, schema, *, cwd, write=False, session=None):
        agent = self.agents[self.policy["roles"][role]]
        call = Call(role, prompt, schema, Path(cwd), self.ledger.task_dir(task["id"]), write, session)
        try:
            r = agent.run(call)
        except AgentError as e:
            self.ledger.event(task, "agent_error", role=role, agent=agent.name, error=str(e)[:2000])
            raise Blocked("agent_error", f"{role}({agent.name}): {e}")
        self.ledger.event(task, "call", role=role, agent=agent.name, cost=r.cost, denials=r.denials)
        return r

    def wt(self, task) -> Path:
        return Path(task["worktree"])

    def type_cfg(self, task) -> dict:
        return self.policy["task_types"][task["type"]]

    def add_items(self, task, items: list[dict], next_stage: str) -> None:
        """새 가정을 판정하고, 사람에게 물어야 할 게 남으면 triage 단계에서 멈춘다."""
        pol.triage(items, task["type"], self.policy)
        task["items"] += items
        for i in items:
            self.ledger.event(task, "triage", item=i["id"], decision=i["decision"], score=i["score"],
                              touches=i["touches"], reversible=i["reversible"])
        task["stage"] = "triage"
        task["next_after_triage"] = next_stage
        self.stage_triage(task)

    def pending(self, task) -> list[dict]:
        return [i for i in task["items"] if i["decision"] == "escalated" and "answer" not in i]

    # ── 단계 ──────────────────────────────────────────────────

    def stage_baseline(self, task):
        repo = Path(task["repo"])
        wt = self.ledger.home / "worktrees" / task["id"]
        if not wt.exists():
            task["base"] = gitops.head(repo)
            task["branch"] = f"loop/{task['id']}"
            task["worktree"] = str(wt)
            gitops.add_worktree(repo, wt, task["branch"], task["base"])
        v = verify.run_suite(self.policy, wt)
        if not v.ok:
            raise Blocked("baseline_red", "과제 시작 전부터 검증이 실패한다. oracle이 없으니 먼저 고쳐야 한다.\n" + v.output)
        task["stage"] = "spec"

    def stage_spec(self, task):
        roles = self.policy["roles"]
        cwd = task["repo"]
        with ThreadPoolExecutor(2) as ex:  # 두 모델이 서로를 보지 않고 독립적으로 해석
            fa = ex.submit(self.call, task, "spec_a", prompts.spec(task), schemas.SPEC, cwd=cwd)
            fb = ex.submit(self.call, task, "spec_b", prompts.spec(task), schemas.SPEC, cwd=cwd)
            a, b = fa.result().data, fb.result().data
        if roles["spec_a"] == roles["spec_b"]:
            self.ledger.event(task, "warning", msg="spec_a와 spec_b가 같은 모델: 발산 신호가 약하다")
        m = self.call(task, "reconciler", prompts.reconcile(task, a, b), schemas.RECONCILE, cwd=cwd).data
        task["spec"] = {k: m[k] for k in ("summary", "acceptance_criteria", "interface_changes")}
        items = [{**x, "id": f"S-{x['id']}", "source": "assumption"} for x in m["assumptions"]]
        items += [{"id": f"S-{d['id']}", "source": "divergence", "question": d["question"], "chosen": d["chosen"],
                   "alternatives": [d["option_a"], d["option_b"]], "touches": d["touches"],
                   "reversible": d["reversible"]} for d in m["divergences"]]
        self.add_items(task, items, next_stage="tests")

    def stage_triage(self, task):
        pend = self.pending(task)
        if pend:
            lines = [f"[{i['id']}] {i['question']}\n    에이전트 선택: {i['chosen']}\n    대안: {' / '.join(i['alternatives'])}"
                     f"\n    태그: {','.join(i['touches'])}, reversible={i['reversible']}, score={i['score']}" for i in pend]
            raise Blocked("questions", "\n".join(lines))
        task["stage"] = task.pop("next_after_triage", "tests")
        if task["stage"] == "implement":
            task["feedback"].append("결정 사항이 갱신됐다. 사람이 확정한 답과 다르게 구현한 부분이 있으면 맞춰라:\n" + prompts.decisions(task))

    def stage_tests(self, task):
        wt, base, cfg = self.wt(task), task["base"], self.type_cfg(task)
        r = self.call(task, "test_author", prompts.tests(task, cfg), schemas.TESTS, cwd=wt, write=True).data

        changed = gitops.changed_files(wt, base)
        non_test = [f for f in changed if not pol.is_test(f, self.policy)]
        if non_test:
            gitops.restore(wt, base, non_test)
            self.ledger.event(task, "reverted", role="test_author", files=non_test)
        test_files = [f for f in changed if pol.is_test(f, self.policy)]
        existing = [f for f in test_files if gitops.exists_at(wt, base, f)]

        if existing:
            if not cfg["existing_tests_mutable"]:
                raise Blocked("test_tamper", f"{task['type']} 과제에서 기존 테스트 수정: {existing}")
            ac_ids = {ac["id"] for ac in task["spec"]["acceptance_criteria"]}
            justified = {m["file"] for m in r["modified_existing"] if m["ac_id"] in ac_ids}
            if unjustified := sorted(set(existing) - justified):
                raise Blocked("test_tamper", f"AC 연결 없이 수정된 기존 테스트: {unjustified}")
            if weak := pol.weakened_tests({f: gitops.file_diff(wt, base, f) for f in existing}):
                raise Blocked("test_weakening", f"assertion 감소 또는 skip 추가: {weak}")
        if not test_files and task["type"] != "refactor":
            raise Blocked("no_tests", "테스트 작성자가 테스트를 만들지 않았다")

        v = verify.run_suite(self.policy, wt)
        if cfg["require_red"] and v.ok:
            raise Blocked("tests_not_red", "구현 전인데 새 테스트가 통과한다. 아무것도 검증하지 않을 수 있다.\n" + v.output)
        if not cfg["require_red"] and not v.ok:
            raise Blocked("refactor_tests_red", "동작 보존 과제인데 특성 테스트가 현재 코드에서 실패한다.\n" + v.output)

        task["test_report"] = r
        task["red_output"] = v.output[-3000:]
        task["locked_commit"] = gitops.commit_all(wt, f"loop(tests): {task['title']}")
        self.ledger.event(task, "tests_locked", files=test_files, red=not v.ok)
        task["stage"] = "implement"

    def stage_implement(self, task):
        wt, b = self.wt(task), self.policy["budget"]
        agent = self.agents[self.policy["roles"]["implementer"]]
        while task["iterations"] < b["implement_iterations"]:
            task["iterations"] += 1
            sess = task["sessions"].get("implementer")
            if sess and agent.supports_resume:
                prompt, session = prompts.followup(task["feedback"]), sess
            else:
                prompt, session = prompts.implement(task), ("new" if agent.supports_resume else None)
            task["feedback"] = []
            r = self.call(task, "implementer", prompt, schemas.IMPLEMENT, cwd=wt, write=True, session=session)
            if r.session:
                task["sessions"]["implementer"] = r.session

            if new := r.data["assumptions"]:  # 구현 중 드러난 애매함 → 재판정
                n = task["iterations"]
                self.add_items(task, [{**x, "id": f"I{n}-{x['id']}", "source": "assumption"} for x in new],
                               next_stage="implement")

            tampered = [f for f in gitops.changed_files(wt, task["locked_commit"]) if pol.is_test(f, self.policy)]
            if tampered:
                gitops.restore(wt, task["locked_commit"], tampered)
                task["tamper_count"] += 1
                self.ledger.event(task, "tamper", files=tampered, count=task["tamper_count"])
                if task["tamper_count"] >= b["tamper_limit"]:
                    raise Blocked("test_tamper", f"구현자가 테스트를 반복 수정: {tampered}")
                task["feedback"].append(f"테스트 파일은 읽기 전용이다. 다음 변경을 되돌렸다: {tampered}")

            v = verify.run_suite(self.policy, wt)
            self.ledger.event(task, "verify", ok=v.ok, sig=v.sig, iteration=task["iterations"])
            if v.ok:  # 되돌린 뒤의 잠긴 테스트 기준으로 통과하면 인정
                gitops.commit_all(wt, f"loop(impl): {task['title']}")
                task["verify_output"] = v.output[-3000:]
                task["stage"] = "review"
                return
            if not v.ok:
                task["failure_sigs"].append(v.sig)
                recent = task["failure_sigs"][-b["same_failure_limit"]:]
                if len(recent) == b["same_failure_limit"] and len(set(recent)) == 1:
                    raise Blocked("no_progress", f"같은 실패가 {len(recent)}번 반복됐다.\n" + v.output)
                task["feedback"].append("검증 실패:\n" + v.output)
            self.ledger.save(task)
        raise Blocked("budget_iterations", "구현 반복 상한에 도달했다.\n" + "\n".join(task["feedback"])[-3000:])

    def stage_review(self, task):
        wt, base = self.wt(task), task["base"]
        changed = [f for f in gitops.changed_files(wt, base) if not pol.is_test(f, self.policy)]
        mutation = verify.run_mutation(self.policy, wt, changed)
        prompt = prompts.review(task, gitops.full_diff(wt, base), task["verify_output"], mutation)
        r = self.call(task, "reviewer", prompt, schemas.REVIEW, cwd=wt).data  # 무상태: 구현자의 맥락을 물려받지 않는다
        task["review"] = {**r, "mutation": mutation}
        blocking = [i for i in r["issues"] if i["severity"] == "blocking"]
        self.ledger.event(task, "review", verdict=r["verdict"], blocking=len(blocking))
        if r["verdict"] == "approve" and not blocking:
            task["stage"] = "gate"
            return
        task["review_rounds"] += 1
        if task["review_rounds"] > self.policy["budget"]["review_rounds"]:
            raise Blocked("review_disagreement", "\n".join(f"- {i['file']}: {i['description']}" for i in blocking))
        task["feedback"].append("리뷰어(다른 모델) 지적, blocking은 반드시 해결:\n"
                                + "\n".join(f"- [{i['severity']}] {i['file']}: {i['description']}" for i in r["issues"]))
        task["stage"] = "implement"

    def stage_gate(self, task):
        wt, base, g = self.wt(task), task["base"], self.policy["gate"]
        files = gitops.changed_files(wt, base)
        tier = max((pol.tier_of(f, self.policy) for f in files), default=1)
        mapped = {m["ac_id"] for m in task["test_report"]["mapping"] if m["tests"]}
        uncovered = sorted({ac["id"] for ac in task["spec"]["acceptance_criteria"]} - mapped
                           | set(task["review"]["uncovered_acs"]))
        task["gate"] = {"tier": tier, "files": files, "uncovered_acs": uncovered}
        self.write_report(task)
        self.ledger.event(task, "gate", tier=tier, uncovered=uncovered, approved=bool(task.get("approved")))
        needs = []
        if tier >= g["approve_from_tier"]:
            needs.append(f"tier {tier} 경로 변경: " + ", ".join(f for f in files if pol.tier_of(f, self.policy) >= g["approve_from_tier"]))
        if uncovered and not g["allow_uncovered_acs"]:
            needs.append(f"테스트로 검증되지 않는 AC: {uncovered}")
        if needs and not task.get("approved"):
            raise Blocked("approval", "\n".join(needs) + f"\n리포트: {self.ledger.task_dir(task['id']) / 'report.md'}")
        task["stage"] = "done"

    # ── 리포트 ────────────────────────────────────────────────

    def write_report(self, task):
        tests_by_ac = {m["ac_id"]: m["tests"] for m in task["test_report"]["mapping"]}
        uncovered = set(task["gate"]["uncovered_acs"])
        out = [f"# {task['title']}", "", f"- type: {task['type']}", f"- branch: `{task['branch']}`",
               f"- tier: {task['gate']['tier']}", f"- 구현 반복: {task['iterations']}, 리뷰 왕복: {task['review_rounds']}, "
               f"테스트 수정 시도: {task['tamper_count']}", "", "## AC ↔ 테스트", "", "| AC | 내용 | 테스트 | |", "|---|---|---|---|"]
        for ac in task["spec"]["acceptance_criteria"]:
            mark = "⚠️" if ac["id"] in uncovered else "✅"
            out.append(f"| {ac['id']} | {ac['text']} | {', '.join(tests_by_ac.get(ac['id'], [])) or '-'} | {mark} |")
        out += ["", "## 결정", "", "| id | 질문 | 결정 | 판정 |", "|---|---|---|---|"]
        for i in task["items"]:
            how = "사람" if "answer" in i else "에이전트 가정"
            out.append(f"| {i['id']} | {i['question']} | {i.get('answer', i['chosen'])} | {i['decision']} / {how} |")
        out += ["", "## 리뷰", "", f"verdict: {task['review']['verdict']}"]
        out += [f"- [{i['severity']}] {i['file']}: {i['description']}" for i in task["review"]["issues"]]
        out += ["", "## 변경", "", "```", gitops.diff_stat(self.wt(task), task["base"]).rstrip(), "```"]
        (self.ledger.task_dir(task["id"]) / "report.md").write_text("\n".join(out) + "\n")

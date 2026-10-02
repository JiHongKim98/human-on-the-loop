"""가짜 에이전트로 루프 로직만 검증한다(모델 호출 없음). 실행: python3 -m unittest -v"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from loop import gitops, policy as pol
from loop.agents import Reply
from loop.ledger import Ledger
from loop.pipeline import Loop

ROOT = Path(__file__).resolve().parent.parent

CALC = "def add(a, b):\n    return a + b\n"
TEST_ADD = "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
TEST_DIV = ("import unittest\nfrom calc import div\n\nclass D(unittest.TestCase):\n"
            "    def test_div(self):\n        self.assertEqual(div(6, 3), 2)\n"
            "    def test_div_zero(self):\n        with self.assertRaises(ValueError):\n            div(1, 0)\n")
GOOD = CALC + "\ndef div(a, b):\n    if b == 0:\n        raise ValueError('b is zero')\n    return a / b\n"
BAD = CALC + "\ndef div(a, b):\n    return a / b\n"

SPEC = {"summary": "div 추가", "interface_changes": ["calc.div(a, b)"],
        "acceptance_criteria": [{"id": "AC1", "text": "div(6,3)==2"}, {"id": "AC2", "text": "0으로 나누면 ValueError"}]}
A_PUBLIC = {"id": "A1", "question": "0 나누기 예외 타입", "chosen": "ValueError", "alternatives": ["ZeroDivisionError"],
            "touches": ["public_api"], "reversible": False}
A_INTERNAL = {"id": "A2", "question": "메시지 문구", "chosen": "b is zero", "alternatives": [],
              "touches": ["internal"], "reversible": True}


class FakeAgent:
    """역할별 스크립트. implementer는 호출 순서대로 scripts를 하나씩 쓴다."""

    def __init__(self, name, impl_scripts, tests_content=TEST_DIV):
        self.name, self.supports_resume = name, True
        self.impl_scripts, self.tests_content = list(impl_scripts), tests_content
        self.calls, self.settings = [], []

    def run(self, call):
        self.calls.append((call.role, call.session))
        self.settings.append((call.role, call.model, call.effort))
        wt = Path(call.cwd)
        match call.role:
            case "spec_a" | "spec_b":
                return Reply({**SPEC, "assumptions": [A_PUBLIC]})
            case "reconciler":
                return Reply({**SPEC, "assumptions": [A_PUBLIC, A_INTERNAL], "divergences": []})
            case "test_author":
                (wt / "tests" / "test_div.py").write_text(self.tests_content)
                return Reply({"test_files": ["tests/test_div.py"], "modified_existing": [],
                              "mapping": [{"ac_id": "AC1", "tests": ["test_div"]}, {"ac_id": "AC2", "tests": ["test_div_zero"]}]})
            case "implementer":
                for path, content in self.impl_scripts.pop(0).items():
                    p = wt / path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
                return Reply({"summary": "ok", "assumptions": []}, session=call.session if call.session != "new" else "sess-1")
            case "reviewer":
                return Reply({"verdict": "approve", "issues": [], "uncovered_acs": []})
        raise AssertionError(call.role)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(CALC)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_add.py").write_text(TEST_ADD)
    (repo / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    gitops.commit_all(repo, "init")
    return repo


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)  # 현재 프로젝트 안에서만 만들고 지운다
        root = Path(self.tmp.name)
        self.repo = make_repo(root)
        self.policy = pol.load(ROOT / "policy.toml")
        self.ledger = Ledger(root / ".loop")

    def tearDown(self):
        self.tmp.cleanup()

    def loop(self, impl_scripts, **kw):
        fake = FakeAgent("fake", impl_scripts, **kw)
        return Loop(self.policy, self.ledger, {"claude": fake, "codex": fake}), fake

    def new_task(self):
        return self.ledger.new_task(repo=str(self.repo), agenda="div 추가", type="feature_new",
                                    title="div 추가", description="calc에 div(a,b) 추가")

    def answer(self, tid, item):
        t = self.ledger.load(tid)
        for i in t["items"]:
            if i["id"] == item:
                i["answer"], i["accepted_default"] = i["chosen"], True
        self.ledger.save(t)

    def test_full_flow_with_question_and_tamper(self):
        tampered_test = TEST_DIV.replace("with self.assertRaises(ValueError):\n            div(1, 0)", "pass")
        loop, fake = self.loop([{"calc.py": BAD, "tests/test_div.py": tampered_test}, {"calc.py": GOOD}])
        t = loop.run(self.new_task()["id"])

        # 공개 API + 되돌릴 수 없음 → 질문, internal 가정은 auto
        self.assertEqual((t["status"], t["blocked"]["reason"]), ("blocked", "questions"))
        self.assertEqual({i["id"]: i["decision"] for i in t["items"]}, {"S-A1": "escalated", "S-A2": "auto"})

        self.answer(t["id"], "S-A1")
        t = loop.run(t["id"])
        self.assertEqual(t["status"], "done", t.get("blocked"))
        self.assertEqual(t["tamper_count"], 1)
        self.assertEqual(t["iterations"], 2)
        wt = Path(t["worktree"])
        self.assertEqual((wt / "tests" / "test_div.py").read_text(), TEST_DIV)  # 약화 시도는 되돌려짐
        self.assertTrue((self.ledger.task_dir(t["id"]) / "report.md").exists())
        # 두 번째 구현 호출은 첫 세션을 이어받고, ladder 다음 단계로 올라간다
        self.assertEqual([s for r, s in fake.calls if r == "implementer"], ["new", "sess-1"])
        ladder = [(s["model"], s["effort"]) for s in self.policy["roles"]["implementer"]["ladder"]]
        self.assertEqual([(m, e) for r, m, e in fake.settings if r == "implementer"], ladder[:2])
        spec_b = self.policy["roles"]["spec_b"]
        self.assertIn(("spec_b", spec_b["model"], spec_b["effort"]), fake.settings)

    def test_tier2_change_needs_approval(self):
        loop, _ = self.loop([{"calc.py": GOOD, "migrations/001.sql": "alter table x;"}])
        tid = self.new_task()["id"]
        self.answer_after_spec(loop, tid)
        t = loop.run(tid)
        self.assertEqual((t["status"], t["blocked"]["reason"]), ("blocked", "approval"))
        t["approved"] = True
        self.ledger.save(t)
        self.assertEqual(loop.run(tid)["status"], "done")

    def test_tests_that_already_pass_are_rejected(self):
        always_green = "import unittest\n\nclass D(unittest.TestCase):\n    def test_div(self):\n        self.assertTrue(True)\n"
        loop, _ = self.loop([], tests_content=always_green)
        tid = self.new_task()["id"]
        self.answer_after_spec(loop, tid)
        t = loop.run(tid)
        self.assertEqual(t["blocked"]["reason"], "tests_not_red")

    def test_no_progress_detected(self):
        loop, _ = self.loop([{"calc.py": BAD}] * 5)
        tid = self.new_task()["id"]
        self.answer_after_spec(loop, tid)
        t = loop.run(tid)
        self.assertEqual(t["blocked"]["reason"], "no_progress")
        self.assertEqual(t["iterations"], self.policy["budget"]["same_failure_limit"])

    def answer_after_spec(self, loop, tid):
        loop.run(tid)
        self.answer(tid, "S-A1")


class PolicyTest(unittest.TestCase):
    policy = pol.load(ROOT / "policy.toml")

    def test_aggregate_threshold_escalates_largest_first(self):
        items = [{"id": f"x{n}", "touches": ["user_visible"], "reversible": True} for n in range(5)]  # 각 2점, 합 10
        pol.triage(items, "feature_new", self.policy)  # threshold 8
        self.assertEqual(sum(i["decision"] == "escalated" for i in items), 1)

    def test_role_ladder_clamps_to_last_step(self):
        ladder = self.policy["roles"]["implementer"]["ladder"]
        self.assertEqual(pol.role(self.policy, "implementer", 1)["effort"], ladder[0]["effort"])
        self.assertEqual(pol.role(self.policy, "implementer", 99)["model"], ladder[-1]["model"])
        self.assertEqual(pol.role(self.policy, "reviewer")["agent"], "codex")

    def test_tiers_and_tests(self):
        self.assertEqual(pol.tier_of("db/migrations/001.sql", self.policy), 2)
        self.assertEqual(pol.tier_of(".env.local", self.policy), 3)
        self.assertEqual(pol.tier_of("src/app.py", self.policy), 1)
        self.assertTrue(pol.is_test("test_x.py", self.policy))
        self.assertTrue(pol.is_test("pkg/sub/test_x.py", self.policy))

    def test_weakening(self):
        diff = "--- a/t.py\n+++ b/t.py\n-        self.assertEqual(f(), 3)\n+        pass\n"
        self.assertEqual(pol.weakened_tests({"t.py": diff}), ["t.py"])
        self.assertEqual(pol.weakened_tests({"t.py": "+    @unittest.skip('x')\n"}), ["t.py"])


if __name__ == "__main__":
    unittest.main()

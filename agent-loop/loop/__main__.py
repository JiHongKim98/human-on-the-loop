"""사용법:
  python -m loop run --repo PATH --agenda "..."           아젠다를 과제로 나누고 순서대로 진행
  python -m loop run --repo PATH --task TYPE --title "..." --agenda "..."   분해 없이 과제 하나
  python -m loop inbox                                     사람 응답을 기다리는 것만 보기
  python -m loop answer TASK ITEM "답" | --accept          질문에 답(또는 에이전트 선택 수락)
  python -m loop answer TASK ITEM "답"                     auto로 넘어간 가정을 뒤집기(override로 기록)
  python -m loop approve TASK                              게이트 승인
  python -m loop resume TASK [--note "힌트"]               막힌 과제 재개
  python -m loop status | show TASK | stats
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import policy as pol
from .agents import make_agents
from .ledger import Ledger
from .pipeline import Loop

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loop")
    ap.add_argument("--home", type=Path, default=ROOT / ".loop")
    ap.add_argument("--policy", type=Path, default=ROOT / "policy.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo", type=Path, required=True)
    r.add_argument("--agenda", required=True)
    r.add_argument("--task", choices=["bugfix", "refactor", "feature_change", "feature_new"])
    r.add_argument("--title")
    sub.add_parser("status")
    sub.add_parser("inbox")
    sub.add_parser("stats")
    s = sub.add_parser("show"); s.add_argument("task")
    a = sub.add_parser("answer"); a.add_argument("task"); a.add_argument("item"); a.add_argument("text", nargs="?")
    a.add_argument("--accept", action="store_true")
    p = sub.add_parser("approve"); p.add_argument("task")
    u = sub.add_parser("resume"); u.add_argument("task"); u.add_argument("--note")
    args = ap.parse_args(argv)

    policy = pol.load(args.policy)
    ledger = Ledger(args.home)
    loop = lambda: Loop(policy, ledger, make_agents(policy))  # noqa: E731 — 조회 명령은 에이전트 없이

    match args.cmd:
        case "run":
            repo = args.repo.resolve()
            if args.task:
                tasks = [ledger.new_task(repo=str(repo), agenda=args.agenda, type=args.task,
                                         title=args.title or args.agenda[:60], description=args.agenda)]
            else:
                tasks = loop().decompose(repo, args.agenda)
            lp = loop()
            for t in tasks:
                print_task(lp.run(t["id"]))
        case "status":
            for t in ledger.all():
                print(f"{t['id']}  {t['status']:<8} {t['stage']:<10} {t['type']:<15} {t['title']}")
        case "inbox":
            for t in ledger.all():
                if t["status"] == "blocked":
                    print_task(t)
        case "show":
            rep = ledger.task_dir(args.task) / "report.md"
            print(rep.read_text() if rep.exists() else ledger.load(args.task))
        case "answer":
            answer(ledger, args)
        case "approve":
            t = ledger.load(args.task)
            t["approved"] = True
            ledger.save(t)
            ledger.event(t, "approve")
            print_task(loop().run(args.task))
        case "resume":
            t = ledger.load(args.task)
            if t["status"] != "blocked":
                sys.exit(f"{args.task}는 blocked 상태가 아니다 ({t['status']})")
            t.update(iterations=0, review_rounds=0, tamper_count=0, failure_sigs=[])  # 사람이 계속하라고 판단
            if args.note:
                t["feedback"].append("사람의 메모: " + args.note)
            ledger.save(t)
            ledger.event(t, "resume", note=args.note)
            print_task(loop().run(args.task))
        case "stats":
            stats(ledger)


def answer(ledger, args):
    t = ledger.load(args.task)
    it = next((i for i in t["items"] if i["id"] == args.item), None)
    if it is None:
        sys.exit(f"항목 없음: {args.item}")
    if not args.accept and not args.text:
        sys.exit("답을 주거나 --accept")
    it["answer"] = it["chosen"] if args.accept else args.text
    it["accepted_default"] = args.accept or args.text == it["chosen"]
    if it["decision"] == "auto":
        it["overridden"] = True  # 묻지 않고 넘어간 가정을 사람이 뒤집음 → triage가 너무 느슨했다는 신호
        t["feedback"].append(f"사람이 가정 [{it['id']}]을 바꿨다: {it['question']} → {it['answer']}")
        if t["status"] == "done":
            print("주의: 이미 끝난 과제다. 반영하려면 새 과제로 다시 돌려야 한다.")
    ledger.save(t)
    ledger.event(t, "answer", item=it["id"], decision=it["decision"],
                 accepted_default=it["accepted_default"], overridden=it.get("overridden", False))
    left = [i["id"] for i in t["items"] if i["decision"] == "escalated" and "answer" not in i]
    print(f"기록됨. 남은 질문: {left or '없음'}" + ("" if left else f" → python -m loop resume {t['id']}"))


def stats(ledger):
    items = [i for t in ledger.all() for i in t["items"]]
    esc = [i for i in items if i["decision"] == "escalated" and "answer" in i]
    auto = [i for i in items if i["decision"] == "auto"]
    blocks = Counter(e["reason"] for e in ledger.events() if e["kind"] == "blocked")
    cost = sum(e.get("cost") or 0 for e in ledger.events() if e["kind"] == "call")
    rate = lambda n, d: f"{n}/{d} ({n / d:.0%})" if d else "-"  # noqa: E731
    print(f"과제: {dict(Counter(t['status'] for t in ledger.all()))}")
    print(f"불필요한 질문 (사람이 에이전트 선택을 그대로 수락): {rate(sum(i['accepted_default'] for i in esc), len(esc))}  → 높으면 triage를 느슨하게")
    print(f"놓친 질문 (auto 가정을 사람이 뒤집음): {rate(sum(bool(i.get('overridden')) for i in auto), len(auto))}  → 높으면 triage를 빡빡하게")
    print(f"막힌 이유: {dict(blocks)}")
    print(f"비용(claude 보고분): ${cost:.2f}")


def print_task(t):
    print(f"\n[{t['id']}] {t['title']} — {t['status']} @ {t['stage']}")
    if b := t.get("blocked"):
        print(f"  사유: {b['reason']}")
        print("  " + b["detail"].replace("\n", "\n  "))


if __name__ == "__main__":
    main()

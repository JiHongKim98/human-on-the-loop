"""역할별 프롬프트. 상태는 대화 기억이 아니라 여기서 매번 디스크 상태로부터 다시 조립한다."""

import json

HEADER = """너는 자동화된 개발 루프에서 '{role}' 역할을 맡은 에이전트다.
- 사람에게 질문하지 마라. 애매한 점은 가장 보수적인 선택으로 진행하고 assumptions에 기록한다.
- 최종 응답은 지정된 JSON schema 하나로만 낸다.
"""

TAGS = """assumptions의 touches 태그 정의:
- public_api: 외부에서 호출하는 함수·엔드포인트·CLI의 시그니처나 동작
- schema: DB·파일·메시지 포맷
- data: 기존 데이터를 바꾸거나 지움
- user_visible: 사용자가 보는 동작·문구·UI
- security: 인증·권한·비밀값
- internal: 위에 해당하지 않는 내부 구현
reversible: 나중에 다른 선택으로 바꿔도 외부 영향 없이 되돌릴 수 있으면 true."""


def _j(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=2)


def decisions(task: dict) -> str:
    lines = []
    for i in task["items"]:
        if "answer" in i:
            lines.append(f"- [{i['id']}] {i['question']} → {i['answer']} (사람이 확정, 반드시 따를 것)")
        elif i.get("decision") == "auto":
            lines.append(f"- [{i['id']}] {i['question']} → {i['chosen']} (에이전트 가정)")
    return "\n".join(lines) or "(없음)"


def decompose(agenda: str) -> str:
    return HEADER.format(role="decomposer") + f"""
아래 아젠다를 이 저장소에서 독립적으로 완료·검증할 수 있는 과제로 나눠라.
각 과제의 type: bugfix(재현 가능한 버그), refactor(동작 보존), feature_change(기존 동작 변경), feature_new(새 동작).
과제 하나는 리뷰 가능한 PR 하나 크기로. 코드를 읽어 판단하되 수정하지 마라.

아젠다:
{agenda}"""


def spec(task: dict) -> str:
    return HEADER.format(role="spec writer") + f"""
과제를 스펙으로 구체화하라. 코드는 읽기만 한다.
- acceptance_criteria: 관측 가능하고 테스트로 판정 가능한 동작만. id는 AC1, AC2...
- interface_changes: 바뀌는 공개 시그니처·엔드포인트·스키마
- assumptions: 과제 설명만으로 정해지지 않아 네가 고른 것 전부. id는 A1, A2...
{TAGS}

과제 ({task['type']}): {task['title']}
{task['description']}"""


def reconcile(task: dict, a: dict, b: dict) -> str:
    return HEADER.format(role="reconciler") + f"""
두 에이전트가 같은 과제를 독립적으로 스펙화했다. 하나로 합쳐라.
- 두 초안이 관측 가능한 동작·인터페이스·범위에서 다른 지점은 divergences로 낸다(id D1, D2...).
  option_a/option_b는 각 초안의 입장, chosen은 더 보수적인 쪽.
- 표현만 다르고 동작이 같으면 divergence가 아니다.
- assumptions는 두 초안의 가정을 중복 없이 합친다(id A1, A2...).
{TAGS}

과제 ({task['type']}): {task['title']}
{task['description']}

초안 A:
{_j(a)}

초안 B:
{_j(b)}"""


def tests(task: dict, cfg: dict) -> str:
    red = ("새 테스트는 지금 코드에서 반드시 실패해야 한다(아직 구현 전)." if cfg["require_red"]
           else "이 과제는 동작 보존이다. 추가하는 테스트는 지금 코드에서 통과해야 한다(특성 테스트).")
    mutable = ("기존 테스트를 고쳐야 하면 modified_existing에 파일·근거 AC·이유를 적어라. 연결 없는 수정과 assertion 약화는 거부된다."
               if cfg["existing_tests_mutable"] else "기존 테스트 파일은 수정하지 마라. modified_existing은 빈 배열.")
    return HEADER.format(role="test author") + f"""
스펙의 acceptance_criteria를 검증하는 테스트를 작성하라. 구현 코드는 작성·수정하지 마라(되돌려진다).
- 구현 세부가 아니라 관측 가능한 동작을 검증한다. 각 테스트에 의미 있는 assertion이 있어야 한다.
- {red}
- {mutable}
- mapping: 모든 AC에 대해 그것을 검증하는 테스트 이름 목록.

스펙:
{_j(task['spec'])}

확정·가정된 결정:
{decisions(task)}"""


def implement(task: dict) -> str:
    files = ", ".join(task["test_report"]["test_files"]) or "(기존 테스트)"
    return HEADER.format(role="implementer") + f"""
스펙을 구현해 테스트를 통과시켜라.
- 테스트 파일은 읽기 전용이다. 수정하면 자동으로 되돌려지고 반복되면 사람에게 넘어간다.
- 구현 중 새로 정해야 하는 애매한 점이 생기면 보수적으로 고르고 assumptions에 기록하라(id N1, N2...).

스펙:
{_j(task['spec'])}

확정·가정된 결정:
{decisions(task)}

검증 대상 테스트: {files}
{followup(task['feedback']) if task['feedback'] else ''}"""


def followup(feedback: list[str]) -> str:
    return "이전 시도에 대한 피드백:\n" + "\n\n".join(feedback)


def review(task: dict, diff: str, verify_output: str, mutation: str | None) -> str:
    extra = f"\n\nmutation 결과:\n{mutation}" if mutation else ""
    return HEADER.format(role="reviewer") + f"""
다른 에이전트의 변경을 리뷰하라. 코드는 읽기만 한다.
- 각 AC가 실제로 구현됐는지, 그리고 테스트가 그 AC를 정말 검증하는지(약한 assertion, 구현을 그대로 베낀 기대값) 본다.
- blocking은 스펙 위반·버그·잘못된 테스트만. 스타일 취향은 minor.
- uncovered_acs: 의미 있게 검증되지 않는 AC id.

스펙:
{_j(task['spec'])}

확정·가정된 결정:
{decisions(task)}

테스트 매핑:
{_j(task['test_report']['mapping'])}

검증 결과(통과):
{verify_output[-3000:]}{extra}

diff:
{diff}"""

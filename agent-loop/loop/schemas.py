"""에이전트 출력 schema. Codex strict 모드를 고려해 모든 속성을 required, additionalProperties=false로 둔다."""

TOUCHES = ["public_api", "schema", "data", "user_visible", "security", "internal"]
TASK_TYPES = ["bugfix", "refactor", "feature_change", "feature_new"]

S = {"type": "string"}
B = {"type": "boolean"}


def obj(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


def enum(values):
    return {"type": "string", "enum": values}


ASSUMPTION = obj(id=S, question=S, chosen=S, alternatives=arr(S), touches=arr(enum(TOUCHES)), reversible=B)
AC = obj(id=S, text=S)
_SPEC = dict(summary=S, acceptance_criteria=arr(AC), interface_changes=arr(S), assumptions=arr(ASSUMPTION))

DECOMPOSE = obj(tasks=arr(obj(title=S, type=enum(TASK_TYPES), description=S)))
SPEC = obj(**_SPEC)
RECONCILE = obj(
    **_SPEC,
    divergences=arr(obj(id=S, question=S, option_a=S, option_b=S, chosen=S,
                        touches=arr(enum(TOUCHES)), reversible=B)),
)
TESTS = obj(
    test_files=arr(S),
    mapping=arr(obj(ac_id=S, tests=arr(S))),
    modified_existing=arr(obj(file=S, ac_id=S, reason=S)),
)
IMPLEMENT = obj(summary=S, assumptions=arr(ASSUMPTION))
REVIEW = obj(
    verdict=enum(["approve", "request_changes"]),
    issues=arr(obj(severity=enum(["blocking", "minor"]), file=S, description=S)),
    uncovered_acs=arr(S),
)

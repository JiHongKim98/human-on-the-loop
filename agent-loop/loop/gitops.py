"""worktree 격리와 diff 조회. 커밋 identity는 호출마다 -c로 넘겨 git 전역 설정을 건드리지 않는다."""

import subprocess
from pathlib import Path

IDENTITY = ["-c", "user.name=agent-loop", "-c", "user.email=agent-loop@localhost"]


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def head(cwd) -> str:
    return git(cwd, "rev-parse", "HEAD").strip()


def add_worktree(repo, path: Path, branch: str, base: str) -> None:
    git(repo, "worktree", "add", "-b", branch, str(path), base)


def commit_all(cwd, message: str) -> str:
    git(cwd, "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd).returncode != 0:
        git(cwd, *IDENTITY, "commit", "-q", "-m", message)
    return head(cwd)


def changed_files(cwd, ref: str) -> list[str]:
    """ref 대비 작업 트리에서 바뀐 파일(추적 안 되는 새 파일 포함)."""
    tracked = git(cwd, "diff", "--name-only", ref).split()
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


def exists_at(cwd, ref: str, path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{ref}:{path}"], cwd=cwd, capture_output=True).returncode == 0


def restore(cwd, ref: str, paths: list[str]) -> None:
    """paths를 ref 상태로 되돌린다. ref에 없던 파일은 지운다."""
    for p in paths:
        if exists_at(cwd, ref, p):
            git(cwd, "checkout", ref, "--", p)
        else:
            (Path(cwd) / p).unlink(missing_ok=True)


def file_diff(cwd, ref: str, path: str) -> str:
    return git(cwd, "diff", ref, "--", path)


def full_diff(cwd, ref: str, limit: int = 60_000) -> str:
    """커밋된 변경 기준. 호출 전에 commit_all로 작업 트리를 커밋해 둔다."""
    d = git(cwd, "diff", ref)
    return d if len(d) <= limit else d[:limit] + f"\n... (diff {len(d)}자 중 {limit}자만 표시)"


def diff_stat(cwd, ref: str) -> str:
    return git(cwd, "diff", "--stat", ref)

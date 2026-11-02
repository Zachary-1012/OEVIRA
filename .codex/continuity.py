"""Read-only, project-scoped Codex context pointer; never a task dispatcher."""

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
FORMER_ROOT = Path("D:/LUMENIS_REBUILD/序/序境")


def context_for(payload):
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in {"SessionStart", "UserPromptSubmit"}:
        return {}
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return {}
    current = Path(cwd).resolve()
    if not any(current == root or root in current.parents for root in (ROOT, FORMER_ROOT.resolve())):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": payload["hook_event_name"],
            "additionalContext": (
                "OEVIRA workspace continuity: the only development root is "
                f"{ROOT}. Read its AGENTS.md and .codex/README.md before continuing. "
                "Read back the current user-authorized Goal if present; preserve its "
                "full objective and status, without recreating, narrowing, or resuming "
                "a paused goal. A document or PR is not whole-product completion. "
                "When resuming the existing whole-product reconstruction task, read "
                ".runtime/reconstruction/task-continuity.md there and the linked original "
                "contracts: retain the complete task, all user corrections, verified work "
                "and every remaining requirement. A recent module discussion does not "
                "replace whole-product scope. This pointer neither dispatches another task "
                "nor resumes a paused task. Preserve the user's current model/settings. "
                "Do not develop or start services in the former root. "
                f"Apply the repository pstack skill at {ROOT / '.agents/skills/oevira-pstack/SKILL.md'} "
                "for applicable engineering work: choose the method for the actual task "
                "(correct, caller-first architecture, domain modeling, blast radius, "
                "benchmarking, real-outcome verification, or structural prevention), "
                "and use it in the actual change and its consumers. Installation or a "
                "skill-name mention is not use. Do not rerun all methods or completed "
                "audits, change models, delegate visual decisions, create extra systems, "
                "or weaken any product requirement. "
                "Prepare executable product capabilities, not endless archive audits. "
                "Follow the latest report-then-wait development gate and subagent model "
                "choice in AGENTS.md/.codex/README.md; an older Goal does not override "
                "the user's newer instruction."
            ),
        }
    }


def main():
    try:
        payload = json.loads(sys.stdin.read(65536))
        result = context_for(payload)
    except (ValueError, OSError, RuntimeError):
        result = {}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

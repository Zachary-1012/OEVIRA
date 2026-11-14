"""Validate repository contracts alongside mandatory product build/test jobs."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {".agents", ".codex", ".github", ".oevira", "app", "assets", "docs", "scripts", "server", "tests"}
ROOT_FILES = {".editorconfig", ".gitattributes", ".gitignore", "AGENTS.md", "CONTRIBUTING.md", "DESIGN.md", "LICENSE", "NOTICE", "PRODUCT.md", "README.md", "THIRD_PARTY_NOTICES.md"}
IGNORED = {
    ".git", ".runtime", ".venv", "data", "__pycache__", ".pytest_cache",
    ".ruff_cache", "node_modules", "dist", "build", ".dart_tool", "coverage",
    "test-results", "ephemeral", ".auth", "研究资料",
}
DOCUMENTS = {
    "product": "PRODUCT.md", "functionality": "docs/PRODUCT-SPEC.md",
    "experience": "DESIGN.md", "architecture": "docs/ARCHITECTURE.md",
    "acceptance": "docs/ACCEPTANCE.md",
}
EXPECTED_SOURCE_ROOTS = ["app", "server", "assets"]
EXPECTED_REQUIRED_CHECKS = ["Repository contracts", "Cross-platform repository checks"]
EXPECTED_PRESERVED_SHA256 = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "docs/reference/NOTICE": "3fa4ebd8fdd28376eba5eaf5b3ef81e57d60aa2bb119a126be74d76b3bf4aa69",
    "docs/reference/LICENSING.md": "5a930ace78267354887dfbebeec684366e53d2f58f6ea0292ce552a2bf5250e1",
    "docs/reference/THIRD_PARTY_NOTICES.md": "6dea307ae7834f1f16361042c99a10332c6f056ef6105ac77f4053be3180c89f",
}
EXPECTED_WORKFLOW_JOBS = {
    "contracts": "Repository contracts",
    "windows": "Cross-platform repository checks",
}
IMPLEMENTATION_FILES = {
    "server/requirements.txt", "server/oevira/__init__.py",
    "server/tests/test_core.py", "server/tests/test_materials.py",
    "docs/API.md",
    "scripts/start.ps1",
}
PRODUCT_CODE_SUFFIXES = {
    ".py", ".pyc", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".dart", ".html", ".htm", ".css", ".scss", ".sh", ".ps1", ".bat",
    ".cmd", ".exe", ".dll", ".so", ".dylib", ".ipynb",
}
SECRET_PATH = re.compile(
    r"(?i)(?:^\.env(?:\..*)?$|(?:^|[._-])(?:cookie|cookies|session|sessions|secret|secrets|credential|credentials)(?:[._-]|$)|private[-_ ]?keys?|id_(?:rsa|ed25519))"
)
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|sk-(?:proj-|live-|svcacct-)?[A-Za-z0-9_-]{32,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?i:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|private[_-]?key)\b\s*[:=]\s*[\"'](?!your_|example|replace|redacted|\$\{)[A-Za-z0-9_./+=-]{32,}[\"']))"
)


def _tracked_files(root: Path, supplied: list[str] | None) -> tuple[list[str], str | None]:
    if supplied is not None:
        return supplied, None
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return [], str(exc)
    if result.returncode:
        return [], result.stderr.decode("utf-8", "replace").strip() or "git ls-files failed"
    return [part.decode("utf-8", "surrogateescape") for part in result.stdout.split(b"\0") if part], None


def _is_sensitive_path(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if any(part.lower() in IGNORED for part in parts):
        return True
    for part in parts:
        if part.lower() == ".env.example":
            continue
        if SECRET_PATH.search(part) or part.lower().endswith((
            ".pem", ".key", ".p12", ".pfx", ".p7b", ".p8", ".der",
            ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm",
        )):
            return True
        if part.lower().endswith(".private.json"):
            return True
    return False


def _read_text_bytes(path: Path) -> str | None:
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _workflow_errors(root: Path) -> list[str]:
    path = root / ".github/workflows/repository-checks.yml"
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        return [f"Repository workflow is unreadable: {exc}"]
    errors: list[str] = []
    match = re.search(r"(?ms)^on:\s*\n(.*?)(?=^[A-Za-z0-9_-]+:|\Z)", text)
    if not match:
        return ["Repository workflow is missing its main-branch triggers"]
    triggers = match.group(1)
    for trigger in ("push", "pull_request"):
        block = re.search(
            rf"(?ms)^  {trigger}:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", triggers
        )
        if not block or not re.search(r"(?m)^    branches:\s*\[\s*main\s*\]\s*$", block.group(1)):
            errors.append(f"Repository workflow must run {trigger} checks for main")
    jobs_match = re.search(r"(?ms)^jobs:\s*\n(.*)\Z", text)
    if not jobs_match:
        return errors + ["Repository workflow is missing its required jobs"]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9_-]*):\s*$", jobs_match.group(1))
    jobs = {parts[index]: parts[index + 1] for index in range(1, len(parts) - 1, 2)}
    if set(jobs) != set(EXPECTED_WORKFLOW_JOBS):
        errors.append("Repository workflow job IDs differ from the two required checks")
    for job_id, job_name in EXPECTED_WORKFLOW_JOBS.items():
        body = jobs.get(job_id, "")
        name_match = re.search(r"(?m)^    name:\s*(.*?)\s*$", body)
        if not name_match or name_match.group(1) != job_name:
            errors.append(f"Repository workflow check name is missing or changed: {job_name}")
        build = "flutter build windows --release" if job_id == "windows" else "flutter build web --release --no-web-resources-cdn"
        for command in ("python scripts/verify_repository.py", "python -m unittest discover -s tests -v",
                        "python -m unittest discover -s server/tests -v", "flutter analyze", "flutter test", build):
            if not re.search(rf"(?m)^\s+run:\s*{re.escape(command)}\s*$", body):
                errors.append(f"Repository workflow job {job_id} is missing required step: {command}")
    return errors



CANONICAL_PLANNING_FILES = (
    "PRODUCT.md", "DESIGN.md", "AGENTS.md", "CONTRIBUTING.md",
    "docs/PRODUCT-SPEC.md", "docs/FRONTEND-EXECUTION.md",
    "docs/ARCHITECTURE.md", "docs/API.md", "docs/ACCEPTANCE.md",
    "docs/domains/business.md", "docs/domains/creative.md", "docs/domains/intelligence.md",
)
RETIRED_PLANNING_ROOTS = ("docs/reference/reconstruction", "docs/decisions")


def _canonical_planning_errors(root: Path) -> list[str]:
    """Check current-file ownership and retired links, not product completeness."""
    errors: list[str] = []
    for name in CANONICAL_PLANNING_FILES:
        path = root / name
        try:
            if not path.read_text(encoding="utf-8").strip():
                errors.append(f"Canonical planning file is empty: {name}")
        except (OSError, UnicodeError):
            errors.append(f"Canonical planning file is unavailable: {name}")
    for name in RETIRED_PLANNING_ROOTS:
        directory = root / name
        if directory.exists() and any(p.is_file() for p in directory.rglob("*")):
            errors.append(f"Retired planning copy is active: {name}")
    candidates = [root / name for name in CANONICAL_PLANNING_FILES]
    candidates += [root / "README.md", root / "docs/domains/README.md",
                   root / ".agents/skills/oevira-pstack/SKILL.md", root / ".codex/README.md"]
    retired = [(root / name).resolve() for name in RETIRED_PLANNING_ROOTS]
    for path in candidates:
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", body):
            url = urlsplit(target.strip("<>"))
            if url.scheme or url.netloc or target.startswith("#"):
                continue
            destination = (path.parent / unquote(url.path)).resolve()
            if any(destination == old or destination.is_relative_to(old) for old in retired):
                errors.append(f"Active contract depends on retired planning: {path.relative_to(root).as_posix()}: {target}")
    return errors


def validate(
    root: Path, *, require_product: bool = False, tracked_paths: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    manifest_path = root / ".oevira/repository.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"Repository contract unreadable: {exc}"]
    if manifest.get("repository") != "Zachary-1012/OEVIRA":
        errors.append("Repository identity differs from the approved OEVIRA repository")
    if manifest.get("canonical_documents") != DOCUMENTS:
        errors.append("Canonical document responsibilities differ from the approved mapping")
    if manifest.get("source_roots") != EXPECTED_SOURCE_ROOTS:
        errors.append("Source responsibilities differ from the approved fixed set")
    if manifest.get("required_checks") != EXPECTED_REQUIRED_CHECKS:
        errors.append("Required workflow checks differ from the approved fixed set")
    if manifest.get("preserved_sha256") != EXPECTED_PRESERVED_SHA256:
        errors.append("Preserved license/attribution digest set differs from the approved fixed set")
    if manifest.get("stage") != "development" or manifest.get("release_ready") is not False:
        errors.append("Development builds do not establish full-product release acceptance")
    if require_product:
        errors.append("PRODUCT_NOT_RELEASED: complete product, packaging and real-work acceptance remain required")
    for name in DOCUMENTS.values():
        path = root / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"Required specification is missing or empty: {name}")
    for name in EXPECTED_SOURCE_ROOTS:
        readme = root / name / "README.md"
        if not readme.is_file():
            errors.append(f"Source responsibility is missing: {name}/README.md")
    for name in IMPLEMENTATION_FILES:
        path = root / name
        if not path.is_file() or not path.stat().st_size:
            errors.append(f"Required development source or contract missing: {name}")
    for name, expected in EXPECTED_PRESERVED_SHA256.items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"Preserved license/attribution bytes changed: {name}")
    for top in root.iterdir():
        if top.name in IGNORED:
            continue
        if top.is_symlink():
            errors.append(f"Unexpected repository symlink: {top.name}")
        elif top.is_dir() and top.name not in DIRECTORIES:
            errors.append(f"Unexpected active directory: {top.name}")
        elif top.is_file() and top.name not in ROOT_FILES:
            errors.append(f"Unexpected root file: {top.name}")
    tracked, git_error = _tracked_files(root, tracked_paths)
    if git_error:
        errors.append(f"Tracked-file inventory unavailable: {git_error}")
    else:
        for name in tracked:
            path = root.joinpath(*PurePosixPath(name).parts)
            if _is_sensitive_path(name):
                errors.append(f"Sensitive or ignored path is tracked: {name}")
                continue
            if not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                errors.append(f"Tracked path escapes repository: {name}")
                continue
            if name.startswith("docs/") and path.suffix.lower() in PRODUCT_CODE_SUFFIXES:
                errors.append(f"Product code cannot be hidden in docs: {name}")
            try:
                body = _read_text_bytes(path)
            except OSError as exc:
                errors.append(f"Tracked file cannot be inspected: {name}: {exc}")
                continue
            if body is not None and SECRET.search(body):
                errors.append(f"Credential-like content found: {name}")
            if body is not None and path.suffix.lower() == ".md" and not name.startswith("docs/reference/"):
                for target in re.findall(r"\]\(([^)]+)\)", body):
                    target = target.strip("<>")
                    url = urlsplit(target)
                    if not url.scheme and not url.netloc and not target.startswith("#"):
                        destination = (path.parent / unquote(url.path)).resolve()
                        if not destination.is_relative_to(root.resolve()):
                            errors.append(f"Active document link leaves repository in {name}: {target}; use a repository reference and record the historical source as text")
                        elif not destination.exists():
                            errors.append(f"Broken local link in {name}: {target}")
    errors.extend(_canonical_planning_errors(root))
    errors.extend(_workflow_errors(root))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-product", action="store_true")
    args = parser.parse_args()
    errors = validate(ROOT, require_product=args.require_product)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("PASS: repository contracts. Product build, real-work and release evidence are separate gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

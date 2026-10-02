"""Execution sandbox — application-level isolation for risky tools.

Applies to: OpenCode subprocess tools, MCP stdio servers, browser worker
supply chain (not the page), and any future external execution.

ISOLATION LEVEL (honest): application-level restrictions — workspace
confinement, allowlisted binaries, scrubbed environment, timeouts, output
caps, secret filtering, temp workspaces, artifact limits. This is NOT a
container/VM/seccomp sandbox (no Docker here). Limits are documented in
docs/SANDBOX.md; callers must not claim OS-level isolation.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

SECRET_ENV = ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "ASSISTANT_TOKEN", "ZARA_DEVICE_KEY", "AWS_SECRET",
              "GITHUB_TOKEN")

SENSITIVE_PATH = [re.compile(p, re.I) for p in
                  (r"\.ssh", r"\.gnupg", r"\.pki", r"credential", r"secret",
                   r"private[ _-]?key")]

BASE_PATH = ("/usr/bin", "/bin")


@dataclass
class SandboxPolicy:
    roots: tuple = ()  # allowed filesystem roots (workspace confinement)
    timeout_s: float = 60.0
    max_output_bytes: int = 8000
    allowed_binaries: tuple = ()  # bare names resolved to fixed dirs
    extra_path_dirs: tuple = ()
    allow_network: bool = False  # documented, not enforced w/o netns
    max_artifacts_bytes: int = 5_000_000


@dataclass
class SandboxResult:
    returncode: int
    output: str
    truncated: bool = False


def resolve_binary(name: str, policy: SandboxPolicy) -> str:
    if "/" in name or name not in policy.allowed_binaries:
        raise PermissionError(f"binary not allowlisted: {name}")
    found = shutil.which(name, path=os.pathsep.join(
        BASE_PATH + tuple(policy.extra_path_dirs)))
    if found is None:
        raise RuntimeError(f"binary not installed: {name}")
    return found


def check_path(path: str, policy: SandboxPolicy) -> str:
    real = os.path.realpath(os.path.expanduser(path))
    if any(p.search(real) for p in SENSITIVE_PATH):
        raise PermissionError(f"sensitive path refused: {path}")
    if policy.roots:
        if not any(real == r or real.startswith(r + os.sep)
                   for r in policy.roots):
            raise PermissionError(f"outside sandbox roots: {path}")
    return real


def scrub_env(keep: tuple = ()) -> dict:
    env = {"PATH": os.pathsep.join(BASE_PATH), "HOME": os.environ.get("HOME", ""),
           "NO_COLOR": "1", "LANG": "C.UTF-8"}
    for k in keep:
        if k in os.environ and k not in SECRET_ENV:
            env[k] = os.environ[k]
    return env


def run(argv: list[str], policy: SandboxPolicy, cwd: str = "",
        env_keep: tuple = (), input_text: str = "") -> SandboxResult:
    """Run an allowlisted binary under the policy. No shell, ever."""
    if not argv:
        raise ValueError("empty argv")
    binary = resolve_binary(argv[0], policy)
    workdir = check_path(cwd or policy.roots[0], policy) if cwd or policy.roots else None
    if policy.roots and not workdir:
        raise PermissionError("sandbox requires cwd inside roots")
    for tok in argv[1:]:
        s = str(tok)
        if s.startswith("/") or s.startswith("~"):
            check_path(s, policy)  # absolute path args stay inside roots
    if input_text and len(input_text) > 200000:
        raise ValueError("stdin too large")
    proc = subprocess.run(
        [binary] + [str(a) for a in argv[1:]], cwd=workdir,
        capture_output=True, text=True, timeout=policy.timeout_s,
        env=scrub_env(env_keep),
        input=input_text if input_text else None)
    out = (proc.stdout or "") + (proc.stderr or "")
    truncated = len(out) > policy.max_output_bytes
    return SandboxResult(returncode=proc.returncode,
                         output=out[-policy.max_output_bytes:],
                         truncated=truncated)


class TempWorkspace:
    """Self-cleaning temp workspace under the system temp dir."""

    def __init__(self, prefix: str = "zara-sb-") -> None:
        self.path = tempfile.mkdtemp(prefix=prefix)

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

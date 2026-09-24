"""The agentic nodes: LLM-driven implementation and self-repair.

Two distinct jobs are done here, and it matters that they stay distinct:

* **Implementation agents** (Nodes 2 and 3) are allowed to write code -- the
  LLVM pass and the gem5 PHQ -- through a ``BashTool`` pinned to the worker
  that owns the checkout.
* **Repair agents** are handed a *specific* failure (a compiler diagnostic, a
  gem5 assertion, a gate verdict) and asked to fix exactly that.

What neither of them can do is touch the verification path.  The correctness
gate, the structural invariants, and the benchmark checksums live on the worker
but are never presented to the agent as editable targets, and the gate's verdict
is computed by the loop from stats the agent does not author.  This mirrors the
isolation CHIA provides between AI models and golden-reference infrastructure;
without it, "the gate passes" means nothing.

Repair outcomes are recorded in the store because the autonomous-repair success
rate is one of the project's headline results.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

log = logging.getLogger("hg.agents")


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------

@dataclass
class AgentResult:
    success: bool
    response: str = ""
    stderr: str = ""
    latency_s: float = 0.0
    backend: str = ""
    prompt_chars: int = 0
    error: str = ""


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

class _DirectGeminiLLM:
    """Resilient Gemini/Vertex wrapper supporting AI Studio API keys, ADC, and gcloud CLI auth.

    Works out-of-the-box across:
      1. Standalone API Key (`export GEMINI_API_KEY=...` or `GOOGLE_API_KEY=...`)
      2. Vertex AI with Application Default Credentials (`GOOGLE_CLOUD_PROJECT`)
      3. Vertex AI with active `gcloud` CLI login (`gcloud auth print-access-token` + `gcloud config get-value project`)
    """

    def __init__(self, model: str, system_message: str = ""):
        self.model = model
        self.system_message = system_message

    def prompt(self, user_message: str, tools: list | None = None):
        import os
        import subprocess
        from types import SimpleNamespace

        # Prevent Conda LD_LIBRARY_PATH vs system mTLS cert-provider helper crash
        os.environ.pop("GOOGLE_API_CERTIFICATE_CONFIG", None)
        os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

        from google import genai
        from google.genai import types

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key:
            client = genai.Client(api_key=api_key)
        else:
            project = (
                os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("HG_GCP_PROJECT")
                or getattr(config, "GCP_PROJECT", "")
            )
            if not project:
                try:
                    project = subprocess.check_output(
                        ["gcloud", "config", "get-value", "project"],
                        text=True,
                        stderr=subprocess.DEVNULL,
                    ).strip()
                except Exception:
                    project = ""
            location = (
                os.environ.get("GOOGLE_CLOUD_LOCATION")
                or getattr(config, "GCP_LOCATION", "")
                or "us-central1"
            )
            adc_file = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
            if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or adc_file.is_file():
                client = genai.Client(vertexai=True, project=project, location=location)
            else:
                from google.oauth2 import credentials
                token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                creds = credentials.Credentials(token=token)
                client = genai.Client(
                    vertexai=True, project=project, location=location, credentials=creds
                )

        cfg = types.GenerateContentConfig(
            system_instruction=self.system_message or None,
            temperature=0.4,
        )
        resp = client.models.generate_content(
            model=self.model,
            contents=user_message,
            config=cfg,
        )
        text = getattr(resp, "text", "") or ""
        return SimpleNamespace(
            result=text,
            returncode=0 if text else 1,
            stderr="",
            stream_result=text,
            success=bool(text),
        )


def make_llm(backend: str | None = None, model: str | None = None,
             *, system_message: str = ""):
    """Construct the configured LLM/agent backend.

    Supports both standalone `GEMINI_API_KEY` (Google AI Studio) and Vertex AI
    (`GOOGLE_CLOUD_PROJECT` / `gcloud` CLI credentials) out of the box, as well
    as CHIA's Claude, OpenCode, Codex, and Ollama backends.
    """
    import os

    backend = (backend or config.LLM_BACKEND).lower()
    model = model or config.LLM_MODEL
    sys_msg = system_message or DEFAULT_SYSTEM_MESSAGE

    if backend in ("vertex", "gemini"):
        return _DirectGeminiLLM(model=model, system_message=sys_msg)
    if backend == "claude":
        from chia.models.claude import ClaudeCodeLLM
        return ClaudeCodeLLM(model=model, system_message=sys_msg)
    if backend == "opencode":
        from chia.models.opencode import OpenCodeLLM
        return OpenCodeLLM(model=model, system_message=sys_msg)
    if backend == "codex":
        from chia.models.codex import CodexLLM
        return CodexLLM(model=model, system_message=sys_msg)
    if backend == "ollama":
        from chia.models.ollama import OllamaLLM
        return OllamaLLM(model=model, system_message=sys_msg)
    raise ValueError(
        f"unknown HG_LLM_BACKEND={backend!r}; expected one of "
        f"vertex|gemini|claude|opencode|codex|ollama")


DEFAULT_SYSTEM_MESSAGE = """\
You are a computer architecture and compiler engineer working inside an
automated hardware/software co-design loop (CHIA).

Ground rules:
- Make the smallest change that fixes the stated problem. You are one step in a
  loop that will re-run the build and the tests; you do not need to fix
  everything at once.
- Never weaken, disable, or edit a test, a correctness gate, an assertion, or a
  benchmark checksum. If a gate fails, the implementation is wrong, not the
  gate.
- Never change the semantics defined in docs/DESIGN.md. If the spec appears
  wrong, say so explicitly in your reply instead of silently deviating.
- Prefer editing inside the delimited `BEGIN HINT.GATHER` / `END HINT.GATHER`
  regions when they exist.
- When you finish, state in one paragraph what you changed and why.
"""


# --------------------------------------------------------------------------
# Prompt loading
# --------------------------------------------------------------------------

_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str, **fields: Any) -> str:
    """Load ``prompts/<name>.md`` and substitute ``{placeholders}``.

    Uses ``str.format_map`` with a forgiving mapping so a stray brace in a
    pasted compiler diagnostic cannot blow up prompt construction -- a class of
    failure that is maddening to debug at 3am.
    """
    if name not in _PROMPT_CACHE:
        path = Path(config.PROMPTS_DIR) / f"{name}.md"
        _PROMPT_CACHE[name] = path.read_text()
    template = _PROMPT_CACHE[name]

    class _Forgiving(dict):
        def __missing__(self, key):  # noqa: D105
            return "{" + key + "}"

    try:
        return template.format_map(_Forgiving(fields))
    except (ValueError, IndexError):
        # Unbalanced braces in the template or in a substituted value: fall
        # back to naive replacement rather than failing the whole repair.
        out = template
        for key, value in fields.items():
            out = out.replace("{" + key + "}", str(value))
        return out


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def make_bash_tool(name: str, cwd: str, resources: dict[str, Any]):
    """A bash MCP tool pinned to the worker holding a particular checkout."""
    from chia.base.tools.BashTool import BashTool
    return BashTool(name, cwd, task_options={"resources": dict(resources)})


class ToolScope:
    """Context manager guaranteeing a tool is stopped.

    A ``BashTool`` holds the worker's resource for its whole lifetime.  Leaking
    one starves every other node that needs that resource, and the symptom is a
    loop that simply hangs -- so this is not optional hygiene.
    """

    def __init__(self, *tools):
        self._tools = [t for t in tools if t is not None]

    def __enter__(self):
        return self._tools[0] if len(self._tools) == 1 else self._tools

    def __exit__(self, *exc):
        for tool in self._tools:
            try:
                tool.stop()
            except Exception as e:  # never mask the original exception
                log.warning("failed to stop tool %s: %s",
                            getattr(tool, "name", "?"), e)
        return False


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------

def ask(llm, prompt: str, *, tools: list | None = None,
        remote: bool = True) -> AgentResult:
    """Send one prompt, with timing and uniform error handling."""
    from runner import resolve, submit

    start = time.time()
    try:
        if remote and not config.LOCAL_MODE:
            ref = submit(llm.prompt, llm, prompt, tools=tools or [])
            result = resolve(ref)
        else:
            result = llm.prompt(prompt, tools=tools or [])
    except Exception as e:  # a failed LLM call must not kill the generation
        return AgentResult(
            success=False, latency_s=time.time() - start,
            backend=type(llm).__name__, prompt_chars=len(prompt),
            error=f"{type(e).__name__}: {e}")

    return AgentResult(
        success=bool(getattr(result, "success", False)
                     or getattr(result, "returncode", 1) == 0),
        response=getattr(result, "result", "") or "",
        stderr=getattr(result, "stderr", "") or "",
        latency_s=time.time() - start,
        backend=type(llm).__name__,
        prompt_chars=len(prompt),
    )


# --------------------------------------------------------------------------
# Self-repair
# --------------------------------------------------------------------------

REPAIR_PROMPTS = {
    "llvm_build": "repair_llvm_build",
    "bench_build": "repair_bench_build",
    "gem5_build": "repair_gem5_build",
    "gem5_gate": "repair_gem5_gate",
    "champsim_build": "repair_champsim_build",
}

REPAIR_WORKDIR = {
    "llvm_build": lambda: str(config.LLVM_DIR),
    "bench_build": lambda: str(config.BENCH_DIR),
    "gem5_build": lambda: str(config.GEM5_ROOT),
    "gem5_gate": lambda: str(config.GEM5_ROOT),
    "champsim_build": lambda: str(config.CHAMPSIM_ROOT),
}

REPAIR_RESOURCES = {
    "llvm_build": config.RES_LLVM,
    "bench_build": config.RES_LLVM,
    "gem5_build": config.RES_GEM5,
    "gem5_gate": config.RES_GEM5,
    "champsim_build": config.RES_CHAMPSIM,
}


def repair(node: str, *, error: str, attempt: int, llm=None,
           store=None, genome=None, extra: dict[str, Any] | None = None,
           ) -> AgentResult:
    """Hand one concrete failure to an agent and let it edit the tree.

    ``node`` selects both the prompt and the worker the bash tool lands on.
    The agent sees the failure, the spec, and a shell -- nothing else.
    """
    if node not in REPAIR_PROMPTS:
        raise ValueError(f"no repair prompt registered for node {node!r}")

    llm = llm or make_llm()
    workdir = REPAIR_WORKDIR[node]()
    resources = REPAIR_RESOURCES[node]

    prompt = load_prompt(
        REPAIR_PROMPTS[node],
        attempt=attempt,
        max_attempts=config.MAX_REPAIR_ATTEMPTS,
        error=error,
        workdir=workdir,
        design_doc=str(config.DOCS_DIR / "DESIGN.md"),
        genome=(genome.to_json(search_only=True) if genome else "{}"),
        **(extra or {}),
    )

    tool_name = f"hg_{node}_bash"
    log.info("repair attempt %d/%d for %s (%d chars of context)",
             attempt, config.MAX_REPAIR_ATTEMPTS, node, len(prompt))

    if config.LOCAL_MODE:
        # No MCP tool server in local mode: the agent gets the prompt but no
        # shell, so it can only propose a patch.  Useful for dry runs; real
        # self-healing needs the cluster.
        result = ask(llm, prompt, tools=[], remote=False)
    else:
        tool = make_bash_tool(tool_name, workdir, resources)
        with ToolScope(tool):
            result = ask(llm, prompt, tools=[tool])

    if store is not None:
        store.record_repair(
            genome_id=(genome.genome_id if genome else None),
            generation=(genome.generation if genome else None),
            node=node,
            attempt=attempt,
            error_excerpt=error,
            succeeded=result.success,
            llm_latency_s=result.latency_s,
            diff_lines=None,
        )
    return result


def repair_loop(node: str, *, attempt_action, error: str, llm=None, store=None,
                genome=None, extra: dict[str, Any] | None = None,
                max_attempts: int | None = None):
    """Run ``attempt_action`` until it succeeds, repairing in between.

    ``attempt_action`` is a zero-argument callable returning an object with a
    ``.success`` attribute and a diagnostics string; it is re-invoked after each
    repair.  Returns ``(final_result, attempts_used, repaired)``.
    """
    max_attempts = max_attempts or config.MAX_REPAIR_ATTEMPTS
    llm = llm or make_llm()
    current_error = error

    for attempt in range(1, max_attempts + 1):
        outcome = repair(node, error=current_error, attempt=attempt, llm=llm,
                         store=store, genome=genome, extra=extra)
        if not outcome.success:
            log.warning("repair agent call failed on attempt %d: %s",
                        attempt, outcome.error or outcome.stderr[:200])
            # The agent call itself failed (rate limit, auth). Retrying the
            # build is pointless, but retrying the agent may help.
            continue

        retried = attempt_action()
        if getattr(retried, "success", False) or getattr(retried, "passed", False):
            log.info("%s repaired after %d attempt(s)", node, attempt)
            return retried, attempt, True

        current_error = (getattr(retried, "diagnostics", "")
                         or getattr(retried, "reason", "")
                         or "no diagnostics produced")
        log.info("%s still failing after attempt %d: %s",
                 node, attempt, current_error[:300])

    return None, max_attempts, False


# --------------------------------------------------------------------------
# Implementation agents (Nodes 2 and 3 bootstrap)
# --------------------------------------------------------------------------

def implement_microarchitecture(llm=None, store=None, *,
                                gem5_root: str | None = None) -> AgentResult:
    """Ask the microarch agent to (re)implement the PHQ in a gem5 checkout.

    Used when bootstrapping from scratch or when the patcher reports that its
    anchors no longer match the local gem5 version -- which is the common real
    failure, because gem5's O3 file layout drifts between releases.
    """
    llm = llm or make_llm()
    gem5_root = gem5_root or str(config.GEM5_ROOT)
    prompt = load_prompt(
        "microarch_agent",
        gem5_root=gem5_root,
        hg_gem5_dir=str(config.GEM5_DIR),
        design_doc=str(config.DOCS_DIR / "DESIGN.md"),
    )
    if config.LOCAL_MODE:
        return ask(llm, prompt, tools=[], remote=False)
    tool = make_bash_tool("hg_microarch_bash", gem5_root, config.RES_GEM5)
    with ToolScope(tool):
        return ask(llm, prompt, tools=[tool])


def implement_compiler_pass(llm=None, store=None) -> AgentResult:
    """Ask the compiler agent to fix up the pass for the local LLVM version."""
    llm = llm or make_llm()
    prompt = load_prompt(
        "compiler_agent",
        llvm_dir=str(config.LLVM_DIR),
        llvm_install=str(config.LLVM_INSTALL),
        design_doc=str(config.DOCS_DIR / "DESIGN.md"),
    )
    if config.LOCAL_MODE:
        return ask(llm, prompt, tools=[], remote=False)
    tool = make_bash_tool("hg_compiler_bash", str(config.LLVM_DIR), config.RES_LLVM)
    with ToolScope(tool):
        return ask(llm, prompt, tools=[tool])


# --------------------------------------------------------------------------
# LLM mutation operator (Node 5)
# --------------------------------------------------------------------------

def propose_genomes(llm, *, history: str, current_best: str, count: int,
                    lambda_value: float) -> list[dict]:
    """Ask the model to propose the next genomes, given what has been tried.

    This is the AlphaEvolve-flavoured half of the search: the random operator
    explores, this one exploits a reading of the trend.  Output is parsed
    defensively -- a malformed proposal costs one candidate, never the run.
    """
    import json
    import re

    prompt = load_prompt(
        "evolve_mutate",
        history=history,
        current_best=current_best,
        count=count,
        lambda_value=f"{lambda_value:.4f}",
        design_doc=str(config.DOCS_DIR / "DESIGN.md"),
    )
    result = ask(llm, prompt, tools=[])
    if not result.success:
        log.warning("LLM mutation call failed: %s", result.error)
        return []

    text = result.response
    # Accept a bare JSON array, or one fenced in markdown.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    blob = fenced.group(1) if fenced else None
    if blob is None:
        start, end = text.find("["), text.rfind("]")
        blob = text[start:end + 1] if (start != -1 and end > start) else None
    if blob is None:
        log.warning("LLM mutation produced no JSON array; ignoring")
        return []

    try:
        payload = json.loads(blob)
    except json.JSONDecodeError as e:
        log.warning("LLM mutation JSON did not parse: %s", e)
        return []

    if isinstance(payload, dict):
        payload = [payload]
    return [p for p in payload if isinstance(p, dict)]

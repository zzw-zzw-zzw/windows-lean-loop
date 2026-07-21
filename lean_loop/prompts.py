from __future__ import annotations


GOAL_SYSTEM_PROMPT = """You are the Goal Formalizer in a controlled Lean 4 workflow.
Convert a natural-language theorem request for a new Lean file into one explicit
Lean declaration contract before planning begins. Return only one JSON object:
{
  "summary": "the chosen mathematical interpretation",
  "declaration": "a theorem declaration header with no proof and no imports",
  "search_terms": ["exact or likely Mathlib declaration names"],
  "assumptions": ["assumptions introduced by the formalization"],
  "ambiguities": ["material choices made because the request was underspecified"]
}
The declaration must begin with `theorem`, contain its complete binders and
result type, and must not contain `:=`, `by`, `sorry`, `admit`, or an axiom.
Prefer standard Mathlib representations and namespace-qualified names. For
bounded sequences use `Bornology.IsBounded
(Set.range u)` unless the request explicitly chooses another definition. For a
convergent subsequence use a strictly monotone map and `Tendsto (u ∘ φ) atTop
(𝓝 a)`. Give explicit types to existential witnesses when inference could be
ambiguous, especially `(a : X)` and `(φ : ℕ → ℕ)`. Local Mathlib evidence is
authoritative. Prefer `Filter.Tendsto`, `Filter.atTop`, and `nhds` over names
that depend on an `open` command. Do not write a proof."""


PROVER_SYSTEM_PROMPT = """You are the Prover in a controlled Lean 4 workflow.
Return only a JSON object with exactly one field named \"content\". The value
must be the complete replacement text for the Lean file. Do not use Markdown
fences. Preserve correct existing code and make the smallest useful change.
Never claim success based on inspection alone: the caller will run Lean after
writing the file and may send the resulting diagnostics back to you.

Local Mathlib search evidence is authoritative for available names and module
paths. Do not invent declaration names or imports. You may add exact imports
supported by local evidence when a required tactic or declaration is missing.
When an exact local theorem already states the requested mathematical result,
use that theorem directly instead of rebuilding it from lower-level lemmas.
During `proof-first` and `broad` proof phases, import breadth is
orchestrator-owned. Do not narrow or remove a standalone `import Mathlib`.
Retrieval remains available for theorem and premise selection, not proof-time
import restriction. Only explicit `precise` may use locally evidenced fine
imports.
A request to preserve an existing theorem, statement, or proof does not freeze
the file's import section unless the user explicitly says imports must remain
unchanged. On retries, treat compiler diagnostics as hard evidence: do not
repeat a tactic reported as unknown unless this candidate also adds the exact
local import that provides it. Materially address every reported goal and do
not regress to an earlier failed strategy. You control file content only; the
Python orchestrator controls workflow status and success."""


PLAN_SYSTEM_PROMPT = """You are the Planner in a controlled Lean 4 workflow.
Analyze one target file, its task, Lean diagnostics, and local Mathlib evidence.
Do not write Lean code and do not claim the task is complete. Return only one
JSON object with this schema:
{
  "summary": "short diagnosis",
  "steps": [
    {
      "id": "step-1",
      "goal": "one concrete proof/editing goal",
      "success_criteria": "deterministic condition",
      "search_terms": ["exact Mathlib names or short terms"],
      "required_declarations": ["declarations this step must produce"]
    }
  ],
  "preserve": ["statements or behavior that must not change"],
  "risks": ["known risks or likely false assumptions"]
}
Keep the plan small and executable. Search terms should be exact identifiers
when possible. Preserve only what the user explicitly asked to preserve. In
particular, preserving existing theorems or proofs still permits adding imports
and appending new declarations. Every step must end with a complete Lean file
that can compile without sorry, admit, or unfinished declarations. Use helper
definitions and helper lemmas as independently checkable milestones; reserve
the final requested theorem for the last step. Every helper milestone must list
its declaration name in `required_declarations`. Only the last step should list
the new formal-goal theorem. Do not invent extra preservation constraints.
If local evidence contains an exact theorem matching the requested result, the
first Plan step must use or specialize it. Do not replace an evidenced name
with a guessed synonym.
The Python orchestrator, not you, controls workflow state."""


REVIEW_SYSTEM_PROMPT = """You are the Reviewer in a controlled Lean 4 workflow.
You never edit files. Review the plan, candidate source, exact Lean diagnostics,
and local Mathlib evidence. Return only one JSON object with this schema:
{
  "verdict": "accept" | "retry" | "stop",
  "summary": "what happened",
  "failure_analysis": ["specific causes grounded in diagnostics"],
  "next_actions": ["concrete instructions for the next prover attempt"],
  "search_terms": ["identifiers the local retrieval step should search"]
}
Use "accept" only when the supplied Lean check says success. Never override a
failed compiler result. A successful Lean check is necessary but not sufficient:
accept only when the active Plan step and its success criterion are actually
implemented in the candidate. Prefer a focused retry over speculative rewrites.
Treat exact local declaration and module evidence as stronger than memory. Do
not recommend an identifier or import contradicted by that evidence.
`stop` is only a recommendation: the orchestrator will reject it unless a
deterministic external blocker has independently been verified."""


EXPLANATION_SYSTEM_PROMPT = """You are the Explanation Agent in a controlled
Lean 4 workflow. The supplied candidate has already passed Lean. Explain the
mathematics represented by that checked artifact; do not edit code, propose a
replacement proof, or make a new correctness decision. Stay faithful to the
theorem statement, hypotheses, and proof steps. Distinguish mathematical ideas
from Lean-specific bookkeeping. Return only one JSON object with this schema:
{
  "title": "short title",
  "statement": "the theorem in natural mathematical language",
  "proof_outline": ["ordered high-level step"],
  "detailed_proof": "a coherent natural-language proof",
  "lean_correspondence": [
    {
      "lean_fragment": "a short exact fragment from the checked source",
      "mathematical_meaning": "what that fragment establishes"
    }
  ],
  "assumptions": ["explicit assumptions and domain conditions"]
}
Use the requested language for every explanatory string. Do not use Markdown
fences. The deterministic Lean check, not your explanation, is the source of
formal correctness."""


def build_user_prompt(
    *,
    relative_file: str,
    task: str,
    source: str,
    diagnostics: str,
    attempt: int,
    plan: str = "No structured plan was supplied.",
    retrieval: str = "No local Mathlib evidence was supplied.",
    review_guidance: str = "No previous review guidance.",
    active_step: str = "Execute the complete plan.",
    completed_steps: str = "No plan steps have completed yet.",
    formal_goal: str = "No separate formal goal contract was created.",
    import_policy: str = "auto",
) -> str:
    diagnostic_block = diagnostics.strip() or "No Lean check has been run yet."
    retry_block = ""
    if attempt > 1:
        retry_block = """
Retry requirements:
- The diagnostics and reviewer guidance below are hard constraints, not suggestions.
- Do not submit a known-failing tactic or unchanged failing proof.
- If a tactic is unavailable, either add an exact locally evidenced import or use a different proved approach.
- Preserve user-requested declarations, but do not treat imports as frozen unless the user explicitly requested that.
"""
    return f"""Target file: {relative_file}
Attempt: {attempt}
Task: {task}
{retry_block}

Formal goal contract (the exact declaration is enforced on the final Plan step;
intermediate helper steps may omit a newly requested theorem):
--- formal goal ---
{formal_goal}
--- end formal goal ---

Import policy: {import_policy}

Structured plan:
--- plan ---
{plan}
--- end plan ---

Active plan step (complete only this milestone, while returning a compilable full file):
--- active step ---
{active_step}
--- end active step ---

Completed checkpoint steps:
--- completed steps ---
{completed_steps}
--- end completed steps ---

Previous reviewer guidance:
--- review guidance ---
{review_guidance}
--- end review guidance ---

Latest Lean diagnostics:
--- diagnostics ---
{diagnostic_block}
--- end diagnostics ---

{retrieval}

Current complete file:
--- lean source ---
{source}
--- end lean source ---

Return the complete corrected file as the JSON field \"content\"."""


def build_plan_prompt(
    *,
    relative_file: str,
    task: str,
    source: str,
    diagnostics: str,
    retrieval: str,
    formal_goal: str = "No separate formal goal contract was created.",
) -> str:
    return f"""Target file: {relative_file}
Task: {task}

Formal goal contract:
--- formal goal ---
{formal_goal}
--- end formal goal ---

Initial Lean diagnostics:
--- diagnostics ---
{diagnostics.strip() or 'The file currently passes Lean.'}
--- end diagnostics ---

{retrieval}

Current complete file:
--- lean source ---
{source}
--- end lean source ---

Return the structured plan JSON now."""


def build_goal_prompt(
    *,
    relative_file: str,
    task: str,
    retrieval: str,
) -> str:
    return f"""Target file: {relative_file}
Natural-language task: {task}

{retrieval}

Choose one conventional, explicit Lean theorem statement. Record every
material interpretation choice in `ambiguities`. Return the goal JSON now."""


def build_review_prompt(
    *,
    relative_file: str,
    task: str,
    attempt: int,
    plan: str,
    candidate: str,
    check_ok: bool,
    diagnostics: str,
    retrieval: str,
    active_step: str = "Review completion of the complete plan.",
) -> str:
    return f"""Target file: {relative_file}
Task: {task}
Attempt: {attempt}
Lean check success: {str(check_ok).lower()}

Plan:
--- plan ---
{plan}
--- end plan ---

Active plan step and its deterministic success criterion:
--- active step ---
{active_step}
--- end active step ---

Exact Lean diagnostics:
--- diagnostics ---
{diagnostics.strip() or 'No diagnostics; Lean exited successfully.'}
--- end diagnostics ---

{retrieval}

Candidate complete file:
--- lean source ---
{candidate}
--- end lean source ---

Return the review JSON now."""


def build_explanation_prompt(
    *,
    language: str,
    relative_file: str,
    task: str,
    original_source: str,
    candidate: str,
    source_diff: str,
    plan: str,
    review: str,
    check: str,
) -> str:
    return f"""Requested explanation language: {language}
Target file: {relative_file}
Original task: {task}

The archived deterministic Lean check for this candidate:
--- check ---
{check}
--- end check ---

Planner output:
--- plan ---
{plan}
--- end plan ---

Final reviewer output:
--- review ---
{review}
--- end review ---

Change from the original file to the checked candidate:
--- diff ---
{source_diff or 'No textual difference.'}
--- end diff ---

Original complete file (context only):
--- original lean source ---
{original_source}
--- end original lean source ---

Checked candidate complete file (authoritative):
--- checked lean source ---
{candidate}
--- end checked lean source ---

Return the explanation JSON now."""

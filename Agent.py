"""
Code Review Intelligence Agent
================================
Uses Gemini 2.x + GitHub API to perform multi-pass, structured code
reviews with severity scoring, then drops into an interactive
"pair-review" chat so you can drill into any finding.

No auth needed for public repos; set GITHUB_TOKEN in .env for higher
rate limits or private repos.

Usage:
    python Agent.py pallets/flask          # one-shot review
    python Agent.py pallets/flask --chat   # review then interactive Q&A
    python Agent.py pallets/flask --save   # also save JSON report
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError

load_dotenv()

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Model fallback order — tries each until one succeeds
MODELS: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
]

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOL_DECLARATIONS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="fetch_github_file_list",
        description=(
            "Given a GitHub repo in 'owner/repo' format, returns a flat list "
            "of all source file paths in the default branch (up to 50 files)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format, e.g. 'pallets/flask'",
                }
            },
            "required": ["repo"],
        },
    ),
    types.FunctionDeclaration(
        name="fetch_file_content",
        description=(
            "Fetches the raw content of a single file from a public GitHub repo. "
            "Use this to read source files before reviewing them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "path": {"type": "string", "description": "File path inside the repo"},
            },
            "required": ["repo", "path"],
        },
    ),
    types.FunctionDeclaration(
        name="submit_review",
        description=(
            "Call this ONCE after reviewing all relevant files. "
            "Submits a structured JSON review with findings, scores, and suggestions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "overall_score": {
                    "type": "integer",
                    "description": "Quality score 0-100",
                },
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence executive summary of codebase quality",
                },
                "findings": {
                    "type": "array",
                    "description": "Specific issues found, sorted most-severe first",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "description": "One of: critical, major, minor, info",
                            },
                            "file": {"type": "string"},
                            "line_hint": {"type": "string"},
                            "issue": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["severity", "file", "issue", "suggestion"],
                    },
                },
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Things done well in this codebase",
                },
                "top_refactor_suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "High-impact refactoring ideas, ordered by priority",
                },
            },
            "required": [
                "overall_score",
                "summary",
                "findings",
                "strengths",
                "top_refactor_suggestions",
            ],
        },
    ),
]

TOOLS: list[types.Tool] = [types.Tool(function_declarations=TOOL_DECLARATIONS)]

# ── GitHub helpers ────────────────────────────────────────────────────────────

SOURCE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".go", ".java", ".rb", ".rs",
    ".cpp", ".c", ".cs", ".php", ".kt", ".swift", ".tsx", ".jsx",
}


def _gh_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_file_list(repo: str) -> dict[str, object]:
    """Recursively fetch all source file paths from a public GitHub repo."""
    url = f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=15)
        if resp.status_code != 200:
            return {"error": f"GitHub API error {resp.status_code}: {resp.text[:300]}"}
        tree = resp.json().get("tree", [])
        all_files = [item["path"] for item in tree if item["type"] == "blob"]
        source_files = [
            f for f in all_files
            if any(f.endswith(ext) for ext in SOURCE_EXTENSIONS)
        ]
        return {
            "files": source_files[:50],
            "total_source_files": len(source_files),
            "total_files": len(all_files),
        }
    except requests.RequestException as e:
        return {"error": f"Network error: {e}"}


def fetch_file_content(repo: str, path: str) -> dict[str, object]:
    """Fetch raw file content from GitHub (truncates files > 6 000 chars)."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=10)
        if resp.status_code != 200:
            return {"error": f"Could not fetch {path}: HTTP {resp.status_code}"}
        content = resp.text
        truncated = len(content) > 6000
        if truncated:
            content = content[:6000] + "\n\n... [file truncated at 6 000 chars]"
        return {"path": path, "content": content, "truncated": truncated}
    except requests.RequestException as e:
        return {"error": str(e)}


def execute_tool(tool_name: str, tool_input: dict[str, object]) -> dict[str, object]:
    """Route tool calls to Python implementations with safe argument extraction."""
    try:
        if tool_name == "fetch_github_file_list":
            repo = str(tool_input.get("repo", ""))
            return fetch_github_file_list(repo)
        if tool_name == "fetch_file_content":
            repo = str(tool_input.get("repo", ""))
            path = str(tool_input.get("path", ""))
            return fetch_file_content(repo, path)
        if tool_name == "submit_review":
            return dict(tool_input)  # terminal — data flows back to caller
        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Tool execution error: {e}"}


# ── System prompts ────────────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """You are a senior staff engineer performing a formal code review.

Your review process:
1. Call fetch_github_file_list to map the codebase structure
2. Call fetch_file_content on 5-10 files: prioritise entry points, auth, data models, config, and any file whose name suggests risk
3. Call submit_review exactly once with your structured findings

Review dimensions (be specific — name file + location):
- Security: injection, hardcoded secrets, improper auth/authz, unsafe deserialization
- Reliability: missing error handling, unhandled edge cases, race conditions
- Performance: N+1 queries, blocking I/O, missing caching, memory leaks
- Maintainability: duplication, long functions, poor naming, missing tests
- Architecture: coupling, separation of concerns, testability

Severity guide:
  critical = security flaw or data-loss risk
  major    = breaks under realistic load or edge cases
  minor    = maintainability / correctness smell
  info     = style, convention, nice-to-have

Be constructive: always pair each issue with a concrete fix suggestion."""

CHAT_SYSTEM_PROMPT = """You are a senior staff engineer who just completed a detailed code review.
The full review report is embedded in the conversation.

Help the developer understand and act on the findings:
- Explain any finding in more depth when asked
- Suggest concrete code fixes (show before/after diffs when helpful)
- Prioritise which issues to tackle first
- Generate checklists, PR templates, or remediation plans on request

Stay grounded in the actual review findings."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_text(response: object) -> str:
    """Return response text safely — returns '' if text is None or missing."""
    text = getattr(response, "text", None)
    return text.strip() if text else ""


def _generate_with_fallback(
    contents: list[types.Content],
    config: types.GenerateContentConfig,
    verbose: bool = False,
) -> types.GenerateContentResponse:
    """
    Try each model in MODELS order. On 429/quota error move to the next.
    Raises RuntimeError if all models are exhausted.
    """
    for model_name in MODELS:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if verbose:
                    print(f"  ⚠️   {model_name} quota exhausted, trying next model...")
                time.sleep(5)
                continue
            raise  # non-quota error — propagate immediately
    raise RuntimeError(
        "All models exhausted their quota. Wait a minute and try again."
    )


def _chat_with_fallback(config: types.GenerateContentConfig) -> genai.chats.Chat:
    """Create a chat session, trying models in fallback order."""
    for model_name in MODELS:
        try:
            return client.chats.create(model=model_name, config=config)
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                time.sleep(5)
                continue
            raise
    raise RuntimeError("All models exhausted their quota for chat. Try again later.")


# ── Core agentic loop ─────────────────────────────────────────────────────────

def run_review(repo: str, verbose: bool = True) -> dict[str, object] | None:
    """
    Agentic review loop using Gemini function calling.
    Returns the structured review dict from submit_review, or None on failure.
    """
    config = types.GenerateContentConfig(
        system_instruction=REVIEW_SYSTEM_PROMPT,
        tools=TOOLS,
        temperature=0.1,
        # Force a tool call every turn — prevents plain-text responses
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY
            )
        ),
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        f"Please review this GitHub repository: **{repo}**\n\n"
                        "Start with fetch_github_file_list to understand structure, "
                        "then read the most critical files, then call submit_review."
                    )
                )
            ],
        )
    ]

    review_result: dict[str, object] | None = None

    if verbose:
        print(f"\n🔍  Starting review: {repo}")
        print("─" * 55)

    for iteration in range(20):  # safety cap — agentic loops need a ceiling
        if verbose and iteration > 0:
            print("  ⏳  Waiting for model response (may take 15-30s)...", flush=True)

        response = _generate_with_fallback(contents, config, verbose=verbose)

        # Guard: empty candidates means content was blocked
        if not response.candidates:
            if verbose:
                print("  ⚠️   Response blocked or empty — stopping.")
            break

        assistant_content = response.candidates[0].content

        # Guard: content can be None on filtered responses
        if assistant_content is None:
            if verbose:
                print("  ⚠️   Empty assistant content — stopping.")
            break

        # Print any reasoning text the model emits
        if verbose:
            for part in assistant_content.parts:
                text = getattr(part, "text", None)
                if text and text.strip():
                    print(f"  💭  {text.strip()[:250]}")

        # Collect function calls (function_call.name is '' when absent)
        fn_calls = [
            p for p in assistant_content.parts
            if getattr(p, "function_call", None) and p.function_call.name
        ]

        if not fn_calls:
            if verbose:
                print("  ✅  Agent finished.\n")
            break

        # Append assistant turn to history before executing tools
        contents.append(assistant_content)

        fn_response_parts: list[types.Part] = []
        for part in fn_calls:
            name = part.function_call.name
            # Deep-convert proto MapComposite → plain dict
            raw_args = part.function_call.args
            args: dict[str, object] = json.loads(
                json.dumps(dict(raw_args))
            ) if raw_args else {}

            if verbose:
                print(f"  🔧  {name}({json.dumps(args)[:80]})")

            result = execute_tool(name, args)

            if name == "submit_review" and "error" not in result:
                review_result = result
                if verbose:
                    print("  📋  Review submitted.")

            fn_response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=name,
                        response={"result": json.dumps(result)},
                    )
                )
            )

        # All tool results go back as a single user turn
        contents.append(types.Content(role="user", parts=fn_response_parts))

        if review_result is not None:
            break

    return review_result


# ── Interactive chat mode ─────────────────────────────────────────────────────

def run_chat(review: dict[str, object], repo: str) -> None:
    """
    Pair-review conversation — model has full review JSON as context.
    Uses same model fallback logic as the review phase.
    """
    print("\n" + "═" * 55)
    print("  💬  PAIR-REVIEW CHAT MODE")
    print("  Ask anything about the findings. Type 'exit' to quit.")
    print("═" * 55)
    print("  Examples:")
    print("    • Explain the critical findings in detail")
    print("    • Show me a fix for the auth issues")
    print("    • Generate a PR checklist for these findings")
    print("    • What should I tackle first?")
    print("─" * 55 + "\n")

    chat_config = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM_PROMPT,
        temperature=0.3,
    )

    chat_session = _chat_with_fallback(chat_config)

    seed = (
        f"The review of `{repo}` is complete. Full structured report:\n\n"
        f"```json\n{json.dumps(review, indent=2)}\n```\n\n"
        "I'm ready to discuss any of the findings in depth."
    )

    try:
        ack = chat_session.send_message(seed)
        ack_text = _safe_text(ack) or "Ready to discuss the findings."
        print(f"  🤖  {ack_text}\n")
    except ClientError as e:
        print(f"  ⚠️   Could not start chat: {e}\n")
        return

    while True:
        try:
            user_input = input("  You › ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Exiting chat. Goodbye!\n")
            break

        if user_input.lower() in {"exit", "quit", "bye", "q"}:
            print("\n  Chat ended. Goodbye!\n")
            break

        if not user_input:
            continue

        try:
            response = chat_session.send_message(user_input)
            reply = _safe_text(response) or "(no response)"
            print(f"\n  🤖  {reply}\n")
        except ClientError as e:
            print(f"\n  ⚠️   API error: {e}\n")


# ── Pretty-print report ───────────────────────────────────────────────────────

SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "info": "🔵",
}
SEVERITY_ORDER: list[str] = ["critical", "major", "minor", "info"]


def print_review(review: dict[str, object], repo: str) -> None:
    score = int(review.get("overall_score", 0))
    bar = "█" * (score // 10) + "░" * (10 - score // 10)

    print(f"\n{'═' * 60}")
    print(f"  CODE REVIEW REPORT — {repo}")
    print(f"{'═' * 60}")
    print(f"\n  Quality Score: [{bar}] {score}/100\n")
    print(f"  {review.get('summary', '')}\n")

    print(f"{'─' * 60}")
    print("  FINDINGS\n")

    findings = review.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    def _severity_rank(item: object) -> int:
        sev = item.get("severity", "") if isinstance(item, dict) else ""  # type: ignore[union-attr]
        return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else len(SEVERITY_ORDER)

    for f in sorted(findings, key=_severity_rank):
        if not isinstance(f, dict):
            continue
        emoji = SEVERITY_EMOJI.get(str(f.get("severity", "")), "⚪")
        print(f"  {emoji} [{str(f.get('severity', '?')).upper()}]  {f.get('file', '')}")
        if f.get("line_hint"):
            print(f"       @ {f['line_hint']}")
        print(f"     Issue : {f.get('issue', '')}")
        print(f"     Fix   : {f.get('suggestion', '')}")
        print()

    print(f"{'─' * 60}")
    print("  STRENGTHS\n")
    for s in review.get("strengths", []):  # type: ignore[union-attr]
        print(f"  ✅  {s}")

    print(f"\n{'─' * 60}")
    print("  TOP REFACTOR SUGGESTIONS\n")
    for i, s in enumerate(review.get("top_refactor_suggestions", []), 1):  # type: ignore[union-attr]
        print(f"  {i}. {s}")

    print(f"\n{'═' * 60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("Usage: python Agent.py <owner/repo> [--chat] [--save]")
        print("Example: python Agent.py pallets/flask --chat")
        sys.exit(1)

    repo = args[0]
    chat_mode = "--chat" in args
    save_mode = "--save" in args

    review = run_review(repo, verbose=True)

    if not review:
        print("❌  Review failed — submit_review was never called.")
        sys.exit(1)

    print_review(review, repo)

    if save_mode:
        out_path = f"review_{repo.replace('/', '_')}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(review, fh, indent=2)
        print(f"💾  Full JSON saved → {out_path}\n")

    if chat_mode:
        run_chat(review, repo)


if __name__ == "__main__":
    main()

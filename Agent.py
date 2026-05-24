"""
Code Review Intelligence Agent
================================
Uses Claude (claude-opus-4-5) + GitHub API to perform multi-pass,
structured code reviews with severity scoring, then drops into an
interactive "pair-review" chat so you can drill into any finding.

No auth needed for public repos; set GITHUB_TOKEN in .env for higher
rate limits or private repos.

Usage:
    python Agent.py pallets/flask          # one-shot review
    python Agent.py pallets/flask --chat   # review then interactive Q&A
    python Agent.py pallets/flask --chat --save  # also save JSON report
"""

import os
import sys
import json
import readline  # noqa: F401 — enables arrow-key history in input()
import requests
from anthropic import Anthropic
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "fetch_github_file_list",
        "description": (
            "Given a GitHub repo in 'owner/repo' format, returns a flat list "
            "of all source file paths in the default branch (up to 300 files)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format, e.g. 'pallets/flask'"
                }
            },
            "required": ["repo"]
        }
    },
    {
        "name": "fetch_file_content",
        "description": (
            "Fetches the raw content of a single file from a public GitHub repo. "
            "Use this to read source files before reviewing them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "path": {"type": "string", "description": "File path inside the repo"}
            },
            "required": ["repo", "path"]
        }
    },
    {
        "name": "submit_review",
        "description": (
            "Call this ONCE after reviewing all relevant files. "
            "Submits a structured JSON review with findings, scores, and suggestions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "overall_score": {
                    "type": "integer",
                    "description": "Quality score 0-100"
                },
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence executive summary of codebase quality"
                },
                "findings": {
                    "type": "array",
                    "description": "Specific issues found, sorted most-severe first",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "major", "minor", "info"]
                            },
                            "file": {"type": "string"},
                            "line_hint": {
                                "type": "string",
                                "description": "Approximate location or function name"
                            },
                            "issue": {"type": "string"},
                            "suggestion": {"type": "string"}
                        },
                        "required": ["severity", "file", "issue", "suggestion"]
                    }
                },
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Things done well in this codebase"
                },
                "top_refactor_suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "High-impact refactoring ideas, ordered by priority"
                }
            },
            "required": [
                "overall_score", "summary", "findings",
                "strengths", "top_refactor_suggestions"
            ]
        }
    }
]

# ── GitHub helpers ────────────────────────────────────────────────────────────

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".go", ".java", ".rb", ".rs",
    ".cpp", ".c", ".cs", ".php", ".kt", ".swift", ".tsx", ".jsx"
}

def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_file_list(repo: str) -> dict:
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
        # Cap at 50 files to stay within token budget
        return {
            "files": source_files[:50],
            "total_source_files": len(source_files),
            "total_files": len(all_files)
        }
    except requests.RequestException as e:
        return {"error": f"Network error: {e}"}


def fetch_file_content(repo: str, path: str) -> dict:
    """Fetch raw file content from GitHub (truncates files > 6000 chars)."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=10)
        if resp.status_code != 200:
            return {"error": f"Could not fetch {path}: HTTP {resp.status_code}"}
        content = resp.text
        truncated = len(content) > 6000
        if truncated:
            content = content[:6000] + "\n\n... [file truncated at 6000 chars for token budget]"
        return {"path": path, "content": content, "truncated": truncated}
    except requests.RequestException as e:
        return {"error": str(e)}


def execute_tool(tool_name: str, tool_input: dict):
    """Route tool calls to Python implementations."""
    if tool_name == "fetch_github_file_list":
        return fetch_github_file_list(**tool_input)
    elif tool_name == "fetch_file_content":
        return fetch_file_content(**tool_input)
    elif tool_name == "submit_review":
        return tool_input  # terminal tool — data flows back to caller
    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ── System prompts ────────────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """You are a senior staff engineer performing a formal code review.

Your review process:
1. Call `fetch_github_file_list` to map the codebase structure
2. Call `fetch_file_content` on 5-10 files: prioritise entry points, auth, data models, config, and any file whose name suggests risk
3. Call `submit_review` exactly once with your structured findings

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
The full review report is embedded in the conversation history.

Help the developer understand and act on the findings:
- Explain any finding in more depth when asked
- Suggest concrete code fixes (show before/after diffs when helpful)
- Prioritise which issues to tackle first given their goals
- Answer architecture questions about the codebase you reviewed
- Generate checklists, PR templates, or remediation plans on request

Stay grounded in the actual review findings. If asked about something outside
the reviewed files, say so honestly rather than hallucinating."""


# ── Core agentic loop ─────────────────────────────────────────────────────────

def run_review(repo: str, verbose: bool = True) -> Optional[dict]:
    """
    Run the full agentic code review loop.

    Returns the structured review dict from submit_review, or None on failure.
    Also returns the full message history so the chat phase can use it.
    """
    messages = [
        {
            "role": "user",
            "content": (
                f"Please review this GitHub repository: **{repo}**\n\n"
                "Be thorough — start with the file list to understand structure, "
                "then read the most critical files, then submit your review."
            )
        }
    ]

    review_result = None
    max_iterations = 20

    if verbose:
        print(f"\n🔍  Starting review: {repo}")
        print("─" * 55)

    for iteration in range(1, max_iterations + 1):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=REVIEW_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # Print any reasoning text the model emits
        if verbose:
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    # Show first 250 chars of each reasoning block
                    snippet = block.text.strip()[:250]
                    print(f"  💭  {snippet}")

        if response.stop_reason == "end_turn":
            if verbose:
                print("  ✅  Agent finished.\n")
            break

        if response.stop_reason != "tool_use":
            if verbose:
                print(f"  ⚠️   Unexpected stop: {response.stop_reason}\n")
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if verbose:
                arg_preview = json.dumps(block.input)[:80]
                print(f"  🔧  {block.name}({arg_preview})")

            result = execute_tool(block.name, block.input)

            if block.name == "submit_review" and "error" not in result:
                review_result = result
                if verbose:
                    print("  📋  Review submitted.")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        if review_result is not None:
            break

    return review_result, messages


# ── Interactive chat mode ─────────────────────────────────────────────────────

def run_chat(review: dict, review_messages: list, repo: str):
    """
    Drop into an interactive pair-review conversation.

    The full review context (all tool calls + findings) is pre-loaded so
    Claude can answer deep follow-up questions grounded in actual file content.
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

    # Seed the chat history with the review phase so Claude has full context
    chat_messages = list(review_messages)

    # Append a system note summarising what was reviewed
    review_json = json.dumps(review, indent=2)
    chat_messages.append({
        "role": "user",
        "content": (
            f"The review of `{repo}` is complete. Here is the full structured report "
            f"for reference:\n\n```json\n{review_json}\n```\n\n"
            "I'm ready to discuss any of the findings."
        )
    })
    # Get Claude to acknowledge before first user message
    ack = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=256,
        system=CHAT_SYSTEM_PROMPT,
        messages=chat_messages
    )
    ack_text = next(
        (b.text for b in ack.content if hasattr(b, "text")), ""
    ).strip()
    print(f"  🤖  {ack_text}\n")
    chat_messages.append({"role": "assistant", "content": ack.content})

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

        chat_messages.append({"role": "user", "content": user_input})

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=CHAT_SYSTEM_PROMPT,
            messages=chat_messages
        )

        reply = next(
            (b.text for b in response.content if hasattr(b, "text")), ""
        ).strip()
        print(f"\n  🤖  {reply}\n")
        chat_messages.append({"role": "assistant", "content": response.content})


# ── Pretty-print report ───────────────────────────────────────────────────────

SEVERITY_EMOJI = {
    "critical": "🔴",
    "major":    "🟠",
    "minor":    "🟡",
    "info":     "🔵"
}

SEVERITY_ORDER = ["critical", "major", "minor", "info"]


def print_review(review: dict, repo: str):
    score = review["overall_score"]
    filled = score // 10
    bar = "█" * filled + "░" * (10 - filled)

    print(f"\n{'═' * 60}")
    print(f"  CODE REVIEW REPORT — {repo}")
    print(f"{'═' * 60}")
    print(f"\n  Quality Score: [{bar}] {score}/100\n")
    print(f"  {review['summary']}\n")

    # Findings grouped by severity
    print(f"{'─' * 60}")
    print("  FINDINGS\n")
    findings = sorted(
        review["findings"],
        key=lambda x: SEVERITY_ORDER.index(x.get("severity", "info"))
    )
    for f in findings:
        emoji = SEVERITY_EMOJI.get(f["severity"], "⚪")
        print(f"  {emoji} [{f['severity'].upper()}]  {f['file']}")
        if f.get("line_hint"):
            print(f"       @ {f['line_hint']}")
        print(f"     Issue : {f['issue']}")
        print(f"     Fix   : {f['suggestion']}")
        print()

    print(f"{'─' * 60}")
    print("  STRENGTHS\n")
    for s in review["strengths"]:
        print(f"  ✅  {s}")

    print(f"\n{'─' * 60}")
    print("  TOP REFACTOR SUGGESTIONS\n")
    for i, s in enumerate(review["top_refactor_suggestions"], 1):
        print(f"  {i}. {s}")

    print(f"\n{'═' * 60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("Usage: python Agent.py <owner/repo> [--chat] [--save]")
        print("Example: python Agent.py pallets/flask --chat")
        sys.exit(1)

    repo = args[0]
    chat_mode = "--chat" in args
    save_mode = "--save" in args

    review, messages = run_review(repo, verbose=True)

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
        run_chat(review, messages, repo)


if __name__ == "__main__":
    main()

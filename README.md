# Code Review Intelligence Agent

> **Multi-pass agentic code reviewer with interactive pair-review chat**  
> Powered by Claude (claude-opus-4-5) + GitHub API

---

## What it does

Most "AI code review" tools are glorified prompts. This one is an **autonomous agent** that:

1. **Explores** the repository structure by fetching the file tree
2. **Selects** the 5–10 highest-risk files (entry points, auth, data models, config) using its own judgment
3. **Reviews** each file in depth across security, performance, reliability, and architecture
4. **Submits** a structured JSON report with severity-scored findings
5. **Opens a pair-review chat** (optional) where you can ask follow-up questions grounded in the actual file content it just read

The chat mode is the key differentiator — the agent retains the full tool call history (every file it fetched) as context, so answers like _"show me a fix for the auth issue in `routes/user.py`"_ are specific, not generic.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Agent.py                             │
│                                                          │
│  ┌──────────────┐   tool calls    ┌────────────────────┐ │
│  │  Claude      │ ─────────────►  │  Tool Executor     │ │
│  │  (Opus 4.5)  │                 │                    │ │
│  │              │ ◄────────────── │  fetch_file_list   │ │
│  │  REVIEW      │   tool results  │  fetch_file_content│ │
│  │  LOOP        │                 │  submit_review     │ │
│  └──────────────┘                 └────────────────────┘ │
│         │                                   │            │
│         │ full message history               │ GitHub API │
│         ▼                                   ▼            │
│  ┌──────────────┐                   ┌───────────────┐    │
│  │  CHAT MODE   │                   │  raw.github   │    │
│  │  (same model │                   │  .com         │    │
│  │   + context) │                   └───────────────┘    │
│  └──────────────┘                                        │
└──────────────────────────────────────────────────────────┘
```

**Key design decisions:**

| Decision | Rationale |
|---|---|
| `submit_review` as a tool | Forces Claude to output structured JSON regardless of phrasing |
| Full message history in chat | Chat answers are grounded in actual file content, not hallucinated |
| File cap at 50, truncation at 6000 chars | Stays within context window without hitting token limits |
| `claude-opus-4-5` | Best reasoning for identifying subtle security patterns |

---

## Setup

**Prerequisites:** Python 3.9+, pip

```bash
git clone https://github.com/YOUR_USERNAME/code-review-agent
cd code-review-agent

pip install -r requirements.txt

cp .env.example .env
# Edit .env → add your ANTHROPIC_API_KEY
```

---

## Usage

### One-shot review
```bash
python Agent.py pallets/flask
```

### Review + interactive Q&A chat
```bash
python Agent.py pallets/flask --chat
```

### Review + save JSON report
```bash
python Agent.py tiangolo/fastapi --chat --save
```

### Example session

```
🔍  Starting review: tiangolo/fastapi
─────────────────────────────────────────────────────
  💭  Let me start by exploring the repository structure...
  🔧  fetch_github_file_list({"repo": "tiangolo/fastapi"})
  💭  I can see this is a large Python project. I'll focus on the core...
  🔧  fetch_file_content({"repo": "...", "path": "fastapi/security/oauth2.py"})
  🔧  fetch_file_content({"repo": "...", "path": "fastapi/routing.py"})
  ...
  📋  Review submitted.

════════════════════════════════════════════════════════════
  CODE REVIEW REPORT — tiangolo/fastapi
════════════════════════════════════════════════════════════

  Quality Score: [████████░░] 82/100

  ...findings...

💬  PAIR-REVIEW CHAT MODE
  You › Explain the security finding in oauth2.py in detail
  🤖  In fastapi/security/oauth2.py around line 94, the token...
```

---

## Sample Output (JSON)

```json
{
  "overall_score": 82,
  "summary": "Well-structured Python web framework with strong typing...",
  "findings": [
    {
      "severity": "major",
      "file": "fastapi/routing.py",
      "line_hint": "APIRouter.add_api_route()",
      "issue": "Exception handlers swallow the original traceback...",
      "suggestion": "Use 'raise ... from e' to preserve exception chain..."
    }
  ],
  "strengths": ["Excellent use of Python type hints throughout"],
  "top_refactor_suggestions": ["Extract validation logic into dedicated module"]
}
```

---

## Engineering Challenges

**Challenge: Making chat answers specific, not generic**

The naive approach is a separate chat session where you paste in the review summary. The problem: Claude doesn't know *why* a finding was flagged — it only sees the summary text.

The solution was threading the entire review message history (including every `fetch_file_content` tool result) into the chat session. This way, when you ask _"how would I fix the auth bug?"_, Claude has the actual source code in context and can give a diff-level answer rather than a generic recommendation.

The tricky part was that the chat must use a **different system prompt** (no tools, different persona) while reusing the review history — which meant carefully separating the system prompt from the conversation state.

---

## What it doesn't do (yet)

- Private repos without a `GITHUB_TOKEN`
- PR-level diff reviews (branch comparison)
- Automated GitHub PR comments
- Caching (re-fetches on every run)

---

## License

MIT

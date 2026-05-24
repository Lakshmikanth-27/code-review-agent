# Code Review Intelligence Agent

> **Autonomous multi-pass code reviewer with interactive pair-review chat**  
> Powered by **Google Gemini** (google-genai SDK) + GitHub API

---

## What it does

Most "AI code review" tools paste your code into a prompt and return a wall of text.  
This is an **autonomous agent** that thinks and acts like a senior engineer:

1. **Explores** — fetches the full file tree of any public GitHub repo
2. **Selects** — autonomously decides which 5–12 files are highest risk (entry points, auth, config, data models)
3. **Reads** — fetches each file's raw content from GitHub
4. **Reviews** — scores and categorises every finding by severity
5. **Reports** — produces a structured JSON report with file, location, issue, and fix for each finding
6. **Chats** *(optional)* — drops into an interactive pair-review conversation where you can ask follow-up questions grounded in the actual code it just read

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Agent.py                               │
│                                                                 │
│  ┌─────────────────────┐  tool calls   ┌─────────────────────┐  │
│  │  Gemini Model       │ ────────────► │   Tool Executor     │  │
│  │  (function calling) │              │                     │  │
│  │                     │ ◄──────────── │  fetch_file_list    │  │
│  │  FunctionCallingMode│  tool results │  fetch_file_content │  │
│  │  = ANY (forced)     │              │  submit_review      │  │
│  └─────────────────────┘              └─────────────────────┘  │
│            │                                     │              │
│            │ review JSON                          │ GitHub API  │
│            ▼                                     ▼              │
│  ┌─────────────────────┐              ┌─────────────────────┐   │
│  │  CHAT MODE          │              │  raw.githubusercontent  │
│  │  (fresh chat session│              │  .com               │   │
│  │   + review context) │              └─────────────────────┘   │
│  └─────────────────────┘                                        │
│                                                                 │
│  Model fallback order:                                          │
│  gemini-2.5-flash-lite → gemini-2.0-flash-lite → gemini-2.5-flash │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| `submit_review` as a **tool** | Forces structured JSON output regardless of how the model phrases its reasoning |
| `FunctionCallingConfigMode.ANY` | Prevents the model from responding with plain text instead of taking the next action |
| Model fallback chain | Automatically retries on quota exhaustion — free-tier friendly |
| Safety guards on `candidates` and `content` | Handles blocked/filtered responses without crashing |
| JSON roundtrip for proto args | Deep-converts Gemini's `MapComposite` proto objects to plain Python dicts before passing to tools |
| Review JSON seeded into chat | Chat answers are grounded in actual file content, not generic advice |
| Separate system prompts per phase | Review phase forces tool use; chat phase disables tools for free-form Q&A |
| File cap 50, truncation at 6 000 chars | Keeps token budget predictable across all models |

---

## Project structure

```
code-review-agent/
├── Agent.py          # All agent logic — review loop, chat, tools, CLI
├── requirements.txt  # Dependencies
├── .gitignore
└── README.md
```

---

## Setup

**Prerequisites:** Python 3.10+, pip

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/code-review-agent
cd code-review-agent
python -m venv venv

# Windows
.\venv\Scripts\Activate.ps1

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Get a Gemini API key (free)

1. Go to **[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)**
2. Click **Create API key**
3. Copy the key

### 3. Configure

Create a `.env` file in the project root:

```env
GOOGLE_API_KEY=your-gemini-api-key-here

# Optional — raises GitHub rate limit from 60 to 5000 req/hr
GITHUB_TOKEN=your-github-token-here
```

---

## Usage

### One-shot review
```bash
python Agent.py pallets/flask
```

### Review + interactive pair-review chat
```bash
python Agent.py pallets/flask --chat
```

### Review + save JSON report to disk
```bash
python Agent.py tiangolo/fastapi --save
```

### All flags combined
```bash
python Agent.py django/django --chat --save
```

---

## Example output

```
🔍  Starting review: tiangolo/fastapi
───────────────────────────────────────────────────────
  🔧  fetch_github_file_list({"repo": "tiangolo/fastapi"})
  ⏳  Waiting for model response (may take 15-30s)...
  🔧  fetch_file_content({"repo": "tiangolo/fastapi", "path": "fastapi/routing.py"})
  🔧  fetch_file_content({"repo": "tiangolo/fastapi", "path": "fastapi/security/oauth2.py"})
  🔧  fetch_file_content({"repo": "tiangolo/fastapi", "path": "fastapi/dependencies/utils.py"})
  ...
  ⏳  Waiting for model response (may take 15-30s)...
  🔧  submit_review({...})
  📋  Review submitted.

════════════════════════════════════════════════════════════
  CODE REVIEW REPORT — tiangolo/fastapi
════════════════════════════════════════════════════════════

  Quality Score: [█████████░] 95/100

  FastAPI is a well-designed, robust framework with excellent
  performance and maintainability...

────────────────────────────────────────────────────────────
  FINDINGS

  🟡 [MINOR]  fastapi/routing.py
       @ add_api_route()
     Issue : Exception chain not preserved on re-raise
     Fix   : Use `raise X from e` to retain original traceback

────────────────────────────────────────────────────────────
  STRENGTHS

  ✅  Excellent use of Python type hints throughout
  ✅  Dependency injection system reduces coupling
  ✅  Async-first design with proper await usage

────────────────────────────────────────────────────────────
  TOP REFACTOR SUGGESTIONS

  1. Add explicit type hints to internal variables in hot paths
  2. Consider extracting middleware validation into separate module
════════════════════════════════════════════════════════════


💬  PAIR-REVIEW CHAT MODE — ask anything about the findings
  You › Show me a fix for the exception chain issue
  🤖  In fastapi/routing.py, the current pattern is:
      try:
          ...
      except Exception:
          raise HTTPException(...)   # ← loses original traceback

      Fix — preserve the chain:
      except Exception as e:
          raise HTTPException(...) from e
```

---

## Structured JSON report

Every review produces a consistent JSON schema (use `--save` to write it to disk):

```json
{
  "overall_score": 95,
  "summary": "Well-structured Python web framework...",
  "findings": [
    {
      "severity": "minor",
      "file": "fastapi/routing.py",
      "line_hint": "add_api_route()",
      "issue": "Exception chain not preserved on re-raise",
      "suggestion": "Use 'raise X from e' to retain original traceback"
    }
  ],
  "strengths": [
    "Excellent use of Python type hints throughout"
  ],
  "top_refactor_suggestions": [
    "Add explicit type hints to internal variables in hot paths"
  ]
}
```

Severity levels: `critical` 🔴 → `major` 🟠 → `minor` 🟡 → `info` 🔵

---

## Engineering challenges

### 1. Forcing structured output without prompt hacking
Early versions used prompt instructions like "respond in JSON". The model would sometimes deviate under long contexts. The fix: implement `submit_review` as a **Gemini function declaration**. The model cannot "decide" to skip it — it must call the tool to end the review, and the tool's schema enforces every required field.

### 2. Preventing plain-text responses mid-loop
After returning tool results, Gemini sometimes responded with text ("I'll now read the auth files...") instead of immediately calling the next tool. Fix: `FunctionCallingConfigMode.ANY` in `ToolConfig` forces every model response to be a tool call until `submit_review` fires and we break the loop.

### 3. Grounding chat answers in real code
The naive chat approach seeds a new session with just the review summary JSON. The problem: when asked "how do I fix the auth issue?", the model gives generic advice because it doesn't have the actual source code.

The fix: seed the chat session with the full review JSON **and** the complete conversation history from the review phase, which includes every `fetch_file_content` result (raw source files). The model can now quote specific lines and produce diff-level answers.

### 4. Proto type leakage into tool arguments
Gemini's SDK returns function call arguments as `MapComposite` — a proto wrapper that looks like a dict but isn't. Passing it directly to Python functions caused silent failures on nested values. Fix: JSON roundtrip (`json.loads(json.dumps(dict(args)))`) deep-converts all nested proto types to plain Python objects before tool execution.

---

## Supported languages

`.py` `.js` `.ts` `.go` `.java` `.rb` `.rs` `.cpp` `.c` `.cs` `.php` `.kt` `.swift` `.tsx` `.jsx`

---

## Limitations

- Public repos only (private repos require `GITHUB_TOKEN` with `repo` scope)
- Reviews current `HEAD` only — no PR diff / branch comparison
- Free-tier quota: ~15 requests/min on Gemini; the agent auto-retries on exhaustion
- Large monorepos are capped at 50 source files for token budget

---

## License

MIT

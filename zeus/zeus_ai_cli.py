"""`claude -p` compatible command line, backed by whatever AI you configured.

Feature modules call:  <CLAUDE_BIN> -p --model haiku --output-format json --system-prompt "…" [flags]
with the user prompt on stdin, and parse {"result": …, "usage": …} from stdout.
This script accepts the same flags (unknown ones are ignored) and answers in the
same envelope, so every module works with Claude CLI, the Anthropic API or any
OpenAI-compatible endpoint without being rewritten.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zeus_ai  # noqa: E402

_TAKES_VALUE = {"--model", "--output-format", "--system-prompt", "--append-system-prompt", "--mcp-config",
                "--setting-sources", "--max-turns", "--allowedTools", "--allowed-tools", "--disallowedTools",
                "--permission-mode", "--input-format", "--effort", "--fallback-model", "--tools"}


def main(argv: list[str]) -> int:
    model, fmt, system, append = "haiku", "text", "", ""
    prompt_args: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _TAKES_VALUE and i + 1 < len(argv):
            v = argv[i + 1]
            if a == "--model":
                model = v
            elif a == "--output-format":
                fmt = v
            elif a == "--system-prompt":
                system = v
            elif a == "--append-system-prompt":
                append = v
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        prompt_args.append(a)
        i += 1
    user = sys.stdin.read() if not sys.stdin.isatty() else ""
    if prompt_args:
        user = (" ".join(prompt_args) + ("\n\n" + user if user else ""))
    sys_prompt = system + (("\n\n" + append) if append else "")
    text, usage, err = zeus_ai.complete(sys_prompt, user, model=model)
    if err:
        if fmt == "json":
            print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": err}))
        sys.stderr.write(err + "\n")
        return 1
    if fmt == "json":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                          "usage": usage or {}, "total_cost_usd": 0}, ensure_ascii=False))
    else:
        sys.stdout.write(text or "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

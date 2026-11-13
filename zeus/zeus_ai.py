"""One AI front door for every feature (digest summaries, market brief, AI picks,
position review, trade-journal review, S/R alert enrichment, narratives…).

Bring your own model — whichever you configure in Settings:

  claude_cli  your Claude subscription through the Claude Code CLI (`claude -p`)
  anthropic   Anthropic API key
  openai      any OpenAI-compatible endpoint (OpenAI, DeepSeek, OpenRouter, Ollama, …)

The original feature modules were written against `claude -p --output-format json`.
Rather than rewrite each of them, `zeus_ai_cli.py` speaks that exact CLI dialect
and forwards to the configured provider. `install_cli_shim()` points CLAUDE_BIN at it,
so every module (and every subprocess it spawns) transparently uses your provider.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import zeus_config as hc

ANTHROPIC_DEFAULT_FAST = "claude-haiku-5-5"
ANTHROPIC_DEFAULT_SMART = "claude-sonnet-5-5"
_FAST_ALIASES = {"haiku", "fast", "claude-haiku", "small"}


def _real_claude_bin() -> str | None:
    """Locate the genuine Claude Code CLI (never our shim)."""
    cand = (hc.get("claude_bin") or "").strip()
    if cand and Path(cand).exists() and "zeus-ai" not in cand:
        return cand
    for c in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude"):
        if c and Path(c).exists() and "zeus-ai" not in c:
            return c
    return None


def provider() -> str:
    p = (hc.get("ai_provider") or "auto").strip().lower()
    if p == "off":
        return "off"
    if p == "auto":
        if hc.get("anthropic_api_key"):
            return "anthropic"
        if hc.get("openai_api_key"):
            return "openai"
        if _real_claude_bin():
            return "claude_cli"
        return "off"
    if p == "claude_cli" and not _real_claude_bin():
        return "off"
    if p == "anthropic" and not hc.get("anthropic_api_key"):
        return "off"
    if p == "openai" and not hc.get("openai_api_key") and not _local_endpoint(hc.get("openai_base_url") or ""):
        return "off"
    return p


def _local_endpoint(url: str) -> bool:
    """A model server on this computer (Ollama, LM Studio, …) needs no key. From the Docker
    image the computer is host.docker.internal."""
    return any(h in url for h in ("localhost", "127.0.0.1", "host.docker.internal"))


def available() -> bool:
    return provider() != "off"


def describe() -> dict:
    p = provider()
    return {"provider": p, "fast": resolve_model("haiku"), "smart": resolve_model("sonnet")} if p != "off" \
        else {"provider": "off"}


def answer_language() -> str:
    lang = (hc.get("ai_language") or "auto").lower()
    if lang in ("zh", "en"):
        return lang
    ui = (hc.get("language") or "auto").lower()
    return "en" if ui == "en" else "zh"     # zh and bilingual (bi) both answer in Chinese


def resolve_model(requested: str | None) -> str:
    """Map the model names hard-coded in feature modules ('haiku', 'sonnet',
    full Claude ids) onto the configured provider."""
    req = (requested or "").strip()
    fast = req.lower() in _FAST_ALIASES or "haiku" in req.lower()
    p = provider()
    tier_model = (hc.get("ai_model_fast") if fast else hc.get("ai_model_smart")) or ""
    if p == "claude_cli":
        return tier_model or req or ("haiku" if fast else "sonnet")
    if p == "anthropic":
        if tier_model:
            return tier_model
        if req.startswith("claude-"):
            return req
        return ANTHROPIC_DEFAULT_FAST if fast else ANTHROPIC_DEFAULT_SMART
    if p == "openai":
        return tier_model or (hc.get("ai_model_fast") or hc.get("ai_model_smart") or "gpt-4o-mini")
    return req


def _language_suffix() -> str:
    if answer_language() == "en":
        return ("\n\nIMPORTANT: Write every human-readable value of your answer in English, "
                "even if the instructions above ask for Chinese. Keep JSON keys exactly as specified.")
    return ""


def complete(system: str, user: str, model: str = "haiku", max_tokens: int = 4096,
             timeout: int = 300) -> tuple[str | None, dict | None, str | None]:
    """→ (text, usage, error). Never raises."""
    p = provider()
    system = (system or "") + _language_suffix()
    m = resolve_model(model)
    try:
        if p == "anthropic":
            import requests
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": hc.get("anthropic_api_key"), "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": m, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=timeout)
            if r.status_code >= 400:
                return None, None, f"anthropic {r.status_code}: {r.text[:300]}"
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            return text, d.get("usage"), None
        if p == "openai":
            import requests
            base = (hc.get("openai_base_url") or "https://api.openai.com/v1").rstrip("/")
            headers = {"content-type": "application/json"}
            if hc.get("openai_api_key"):
                headers["authorization"] = "Bearer " + hc.get("openai_api_key")
            r = requests.post(base + "/chat/completions", headers=headers, timeout=timeout,
                              json={"model": m, "messages": [{"role": "system", "content": system},
                                                             {"role": "user", "content": user}]})
            if r.status_code >= 400:
                return None, None, f"openai-compatible {r.status_code}: {r.text[:300]}"
            d = r.json()
            text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            u = d.get("usage") or {}
            return text, {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")}, None
        if p == "claude_cli":
            env = clean_cli_env()
            cmd = [_real_claude_bin(), "-p", "--model", m, "--output-format", "json",
                   "--system-prompt", system, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--setting-sources", "project,local", "--no-session-persistence"]
            pr = subprocess.run(cmd, input=user, capture_output=True, text=True, env=env,
                                cwd=tempfile.gettempdir(), timeout=timeout)
            if pr.returncode != 0:
                return None, None, (pr.stderr or pr.stdout or "claude CLI failed")[:400]
            d = json.loads(pr.stdout)
            return d.get("result"), d.get("usage"), None
        return None, None, "AI 未配置:请到 ⚙ 设置 → AI 选择一个提供方 / AI is not configured: open ⚙ Settings → AI"
    except Exception as e:  # network, timeout, JSON…
        return None, None, f"{type(e).__name__}: {e}"


def clean_cli_env() -> dict:
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)  # use the subscription, not a stray paid key
    for k in list(env):
        if k.startswith(("CLAUDE_CODE", "CLAUDE_AGENT")) or k in ("CLAUDE_EFFORT", "BAGGAGE", "CLAUDE_BIN"):
            env.pop(k, None)
    return env


def install_cli_shim() -> str:
    """Write ~/.zeus-terminal/bin/zeus-ai (a tiny launcher for zeus_ai_cli.py with
    this interpreter) and export CLAUDE_BIN to it. Returns the shim path."""
    bindir = hc.home() / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    cli = Path(__file__).resolve().parent / "zeus_ai_cli.py"
    if os.name == "nt":
        shim = bindir / "zeus-ai.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{cli}" %*\r\n', encoding="utf-8")
    else:
        shim = bindir / "zeus-ai"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{cli}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["CLAUDE_BIN"] = str(shim)
    return str(shim)

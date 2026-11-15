# Contributing to Rove

Thank you for your interest in contributing to **Rove**!

## Core Engineering Principles

1. **Detached Lifetimes**: Never tie AI agent execution or PTY shell lifetimes to an HTTP request or WebSocket connection. Workers (`workstation/worker.py`) and terminals (`workstation/terminal.py`) must run as independent child processes orchestrated through SQLite WAL state and cursor replay.
2. **Zero-State Client, Local-First Execution**: Keep all credentials, OAuth tokens, VAPID keys, and repository mutations strictly on the host workstation (`~/.config/walking-workstation` with `0700`/`0600` permissions). Never log secrets or pass passwords via CLI arguments.
3. **Anti-AI-Slop Frontend Standard**:
   - **No Cliche Gradients**: Never use purple/pink/indigo neon gradients or heavy blurry glassmorphism. Use neutral surface layering (`Base -> Surface -> Elevated`) with a single restrained accent color.
   - **1px Hairline Borders**: Prefer `1px` semi-transparent hairline borders over heavy black drop shadows.
   - **Restrained Geometry**: Keep border-radius disciplined (`6-8px` for buttons, `10-12px` for cards, `14-16px` for modals).
   - **Complete Interactive States**: Every interactive element must implement `:hover`, `:active`, `:disabled`, and `:focus-visible` states, plus `aria-*` accessibility attributes and `@media (prefers-reduced-motion: reduce)`.

## Running Tests

Before submitting a Pull Request, run both backend and frontend test suites:

```bash
# Python backend unit & regression tests
./.venv/bin/python -m unittest discover -s tests -v

# Frontend parser, cache, model catalog & service worker tests
node --test tests/*.test.mjs
```

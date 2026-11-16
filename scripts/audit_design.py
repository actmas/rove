"""CLI gate to audit frontend/src for Anti-AI-Slop violations and hardcoded hex colors."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstation.design_guard import audit_frontend_directory


def main() -> int:
    src = ROOT / 'frontend' / 'src'
    findings = audit_frontend_directory(str(src))
    if not findings:
        print('OK: No Anti-AI-Slop or hardcoded-hex violations found in frontend/src.')
        return 0
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

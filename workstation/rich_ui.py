"""Composable streaming rich-answer protocol (rove-ui) with Anti-AI-Slop governance.

Inspired by makecindy/cindy's cindy-ui streaming fence DSL:
- Models emit line-oriented JSONL operations inside ```rove-ui fences for
  structured metrics, comparison cards, progress bars, or action buttons.
- Enforces strict Anti-AI-Slop color governance: literal hex/rgb colors,
  backgroundColor, and borderColor are stripped; only semantic roles
  (primary, secondary, tertiary, success, warning, danger) are allowed.
- Used on demand: plain Markdown remains the default for normal text/code.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

ALLOWED_COLOR_ROLES = frozenset({'primary', 'secondary', 'tertiary', 'success', 'warning', 'danger'})
ALLOWED_NODE_TYPES = frozenset({'card', 'row', 'stat', 'text', 'badge', 'bar', 'button', 'divider'})
ALLOWED_OPS = frozenset({'create', 'append', 'patch', 'remove'})

RICH_UI_PROMPT_GUIDE = """[可选流式富组件协议 (rove-ui，按需使用)]
普通对话、解释与代码请直接输出标准 Markdown，不要套用组件。
仅当展示多维指标对比、进度条、结构化摘要卡片或带明确下一步操作按钮能显著提升手机/桌面阅读效率时，可使用 ```rove-ui 围栏（每行一条 JSON 操作）：
- {"op":"create","id":"root","type":"card","title":"标题"}
- {"op":"append","parent":"root","id":"r1","type":"row"}
- {"op":"append","parent":"r1","id":"s1","type":"stat","label":"状态","value":"通过","tone":"success"}
- {"op":"append","parent":"root","id":"b1","type":"bar","label":"进度","percent":80,"tone":"primary"}
- {"op":"append","parent":"root","id":"btn1","type":"button","label":"继续修复","send":"请继续修复 P1 问题"}
硬性约束：严禁输出任何 HTML/CSS 或十六进制颜色；`tone` 仅限 primary / secondary / tertiary / success / warning / danger；按 390px 手机窄屏可读性设计。"""


def sanitize_node_props(props: Dict[str, Any]) -> Dict[str, Any]:
    """Strip literal colors/styles and normalize semantic tone roles."""
    cleaned: Dict[str, Any] = {}
    for k, v in props.items():
        if k in ('backgroundColor', 'borderColor', 'color', 'style', 'className', 'css'):
            continue
        if k == 'tone':
            role = str(v or 'primary').strip().lower()
            cleaned['tone'] = role if role in ALLOWED_COLOR_ROLES else 'primary'
            continue
        if k == 'send' and isinstance(v, str):
            cleaned['send'] = v[:4000]
            continue
        if k == 'percent':
            try:
                cleaned['percent'] = max(0, min(100, int(round(float(v)))))
            except (TypeError, ValueError):
                cleaned['percent'] = 0
            continue
        cleaned[k] = v
    return cleaned


def parse_rove_ui_lines(text: str) -> Dict[str, Any]:
    """Parse a ```rove-ui block into a normalized node tree and error count."""
    nodes: Dict[str, Dict[str, Any]] = {}
    roots: List[str] = []
    errors = 0

    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            op_obj = json.loads(line)
        except Exception:
            errors += 1
            continue
        if not isinstance(op_obj, dict):
            errors += 1
            continue
        op = op_obj.get('op')
        node_id = str(op_obj.get('id') or '').strip()
        if op not in ALLOWED_OPS or not node_id:
            errors += 1
            continue

        if op == 'remove':
            nodes.pop(node_id, None)
            if node_id in roots:
                roots.remove(node_id)
            for n in nodes.values():
                if node_id in n['children']:
                    n['children'].remove(node_id)
            continue

        if op == 'patch':
            target = nodes.get(node_id)
            if not target:
                errors += 1
                continue
            patch_props = sanitize_node_props({
                k: v for k, v in op_obj.items() if k not in ('op', 'id', 'parent', 'type')
            })
            target['props'].update(patch_props)
            continue

        node_type = op_obj.get('type')
        if node_type not in ALLOWED_NODE_TYPES:
            errors += 1
            continue
        props = sanitize_node_props({
            k: v for k, v in op_obj.items() if k not in ('op', 'id', 'parent', 'type')
        })
        nodes[node_id] = {
            'id': node_id,
            'type': node_type,
            'props': props,
            'children': [],
        }
        if op == 'create':
            if node_id not in roots:
                roots.append(node_id)
        elif op == 'append':
            parent_id = str(op_obj.get('parent') or '').strip()
            parent = nodes.get(parent_id)
            if parent and node_id not in parent['children']:
                parent['children'].append(node_id)
            elif not parent:
                roots.append(node_id)

    return {'roots': roots, 'nodes': nodes, 'errors': errors}

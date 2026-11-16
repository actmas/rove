"""Anti-AI-Slop Design & Aesthetic Guard for Frontend Code.

Inspired by pbakaus/impeccable (★103k), Nutlope/hallmark (★48.1k), and unslop.
Eliminates typical AI-generated frontend slop (cliche gradients, blurry glass,
excessive border-radius, missing interactive states) and enforces crisp,
accessible, modern design systems (Apple HIG / Linear / Vercel grade).
"""
import re
from typing import Any, Dict, List, Optional

from . import memories

# Keyword triggers indicating frontend/UI intent
UI_KEYWORDS = [
    '页面', '前端', '界面', '组件', '样式', '设计', '弹窗', '按钮',
    '颜色', '配色', '排版', '自适应', '响应式', '重构ui', '优化ui',
    '模态框', '侧栏', '导航栏', '顶栏', '深色模式', '暗黑', 'css',
    'ui', 'html', 'react', 'tailwind', 'style', 'component', 'modal',
    'button', 'layout', 'typography', 'theme', 'dark mode', 'frontend',
    'card', 'flex', 'grid', 'view', 'navbar', 'dialog',
]

ANTI_SLOP_DIRECTIVE = """[前端代码审美审查规范 (Anti-AI-Slop / Impeccable Design Standard)]
你作为具备顶级工业质感追求的资深设计师与前端专家，在编写/修改 UI 代码时，严格拒绝典型的“AI 平庸味 (AI Slop)”：
1. 【色彩与表面】严禁滥用大面积俗套紫粉霓虹渐变（如 purple/pink/indigo 滥用）与低质毛玻璃。界面应建立清晰的中性表面分层（Base -> Surface -> Elevated）。主强调色（Accent）需克制专注。
2. 【边框与阴影】以细腻的 1px 半透明微对比边框替代浓黑超大阴影，避免脏灰感；严禁使用不透明度过高的粗暴黑色投影。
3. 【几何与圆角】圆角严格遵循设计系统规范（小按钮 6-8px，卡片 10-12px，大模态框 14-16px），杜绝到处滥用毫无克制的 rounded-3xl 或全圆角。
4. 【排版阶梯】严格保持字阶节奏（标题 Bold/Tight，正文 Regular/Normal，说明文字 Compact），正负对比鲜明，严禁用低对比度灰色破坏可读性。
5. 【状态机与无障碍】所有按钮/链接/输入框必须具备完整的 :hover（微提亮或微妙边框响应）、:active（微内缩按压反馈）、:disabled（置灰与禁用光标）及 :focus-visible（清晰键盘轮廓），并标注 aria- 属性。
6. 【动效与双主题】微交互过渡控制在 150ms~200ms ease，支持 @media (prefers-reduced-motion: reduce)，颜色优先复用已有 CSS 变量以天然契合浅色/深色主题。"""


def is_ui_task(prompt_text: str) -> bool:
    """Check if the user prompt pertains to frontend/UI design and styling."""
    if not prompt_text:
        return False
    lower = prompt_text.lower()
    return any(kw in lower for kw in UI_KEYWORDS)


def get_design_directive(force: bool = False, prompt_text: str = '') -> str:
    """Return the Anti-Slop design directive if applicable."""
    if force or is_ui_task(prompt_text):
        return ANTI_SLOP_DIRECTIVE
    return ''


# Static anti-slop rules for auditing generated code
SLOP_PATTERNS = [
    (
        r'bg-gradient-to-[rblt]+.*from-(purple|pink|indigo|fuchsia)-[0-9]+.*to-(pink|indigo|purple|rose)-[0-9]+',
        'cliche_gradient',
        '检测到俗套的 AI 模板渐变（紫粉/青紫搭配），建议使用克制的中性色阶与单一强调色。',
    ),
    (
        r'box-shadow:\s*0\s+(2[0-9]|[3-9][0-9])px.*rgba\(0,\s*0,\s*0,\s*0\.[4-9]\)',
        'heavy_shadow',
        '检测到浓重模糊的深黑阴影，建议改为细腻的 1px 微对比边框或柔和的多层微投影。',
    ),
    (
        r'border-radius:\s*(3[0-9]|[4-9][0-9])px|rounded-3xl|rounded-full(?!\s*(?:avatar|icon|badge|indicator))',
        'excessive_radius',
        '检测到超大圆角滥用，除徽标/头像外，容器与卡片建议保持在 8px~16px 之间。',
    ),
    (
        r'color:\s*#(?:888|999|aaa|bbb)(?!;|\s*var)',
        'low_contrast_text',
        '检测到低对比度硬编码灰色字体，建议采用系统 CSS 变量（如 var(--text-secondary)）确保可读性。',
    ),
]


def audit_code_changes(code_text: str) -> List[Dict[str, Any]]:
    """Scan code or diff for known anti-slop violations and return recommendations."""
    findings = []
    if not code_text:
        return findings

    for pattern, rule_id, advice in SLOP_PATTERNS:
        match = re.search(pattern, code_text, re.IGNORECASE)
        if match:
            findings.append({
                'rule_id': rule_id,
                'matched': match.group(0)[:60],
                'advice': advice,
            })
    return findings


def seed_design_memory(project_id: str) -> Dict[str, Any]:
    """Seed the anti-slop design standard directly into project memories (P0)."""
    return memories.add_memory(
        project_id=project_id,
        content='前端 UI 开发严格遵从 Anti-AI-Slop 规范：禁止俗套紫粉渐变，使用 1px 半透明微边框，保持细腻圆角与完整的 hover/active/focus-visible 状态交互。',
        category='convention',
        pinned=True,
    )


HARDCODED_HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b')


def audit_frontend_directory(
    src_dir: str,
    exemptions: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Scan frontend source files for Anti-AI-Slop violations and non-token hex literals."""
    from pathlib import Path

    root = Path(src_dir)
    if not root.is_dir():
        return []
    exempt = exemptions or {'style.css', 'RoveMark.jsx'}
    results: List[Dict[str, Any]] = []
    for path in sorted(root.rglob('*')):
        if path.suffix not in ('.jsx', '.tsx', '.js', '.ts', '.css') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for finding in audit_code_changes(text):
            results.append({'file': str(path.relative_to(root)), **finding})
        if path.name not in exempt:
            for m in HARDCODED_HEX_RE.finditer(text):
                # Allow SVG/data URLs or CSS var fallbacks if explicitly marked
                results.append({
                    'file': str(path.relative_to(root)),
                    'rule_id': 'hardcoded_hex_color',
                    'matched': m.group(0),
                    'advice': '组件层禁止硬编码十六进制色值，请使用 style.css 中的语义 CSS 变量。',
                })
                break
    return results

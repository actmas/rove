"""Vision Bridge: image-to-text translation for text-only models.

Inspired by makecindy/cindy's vision-bridge-design:
- When a user attaches images while running a text-only model (e.g., DeepSeek,
  local Ollama text models where supportsImage=False), translate each image into
  a grounded text description via a configured multimodal helper endpoint.
- Bounded per-image timeout (30s); on individual failure, emit a safe
  placeholder instead of letting text models hallucinate or crash with HTTP 400.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Callable, Dict, List, Optional
import urllib.request

from . import store as db

DEFAULT_IMAGE_TIMEOUT_SECS = 30.0
MAX_BRIDGE_IMAGES = 6


def resolve_vision_bridge_config() -> Optional[Dict[str, str]]:
    """Resolve vision helper endpoint from config.json `vision_bridge` or enabled vision HTTP agent."""
    try:
        cfg = db.config()
    except Exception:
        cfg = {}

    vb = cfg.get('vision_bridge')
    if isinstance(vb, dict) and vb.get('enabled') and vb.get('base_url') and vb.get('model'):
        return {
            'base_url': str(vb['base_url']).strip().rstrip('/'),
            'api_key': str(vb.get('api_key') or '').strip(),
            'model': str(vb['model']).strip(),
        }

    # Fallback: check custom HTTP agents with supportsImage=True and api_key configured
    for agent in cfg.get('custom_agents') or []:
        if not isinstance(agent, dict) or agent.get('adapter') != 'http' or agent.get('enabled') is False:
            continue
        base_url = str(agent.get('base_url') or '').strip().rstrip('/')
        api_key = str(agent.get('api_key') or '').strip()
        if not base_url:
            continue
        for m in agent.get('models') or []:
            if isinstance(m, dict) and m.get('supportsImage') and m.get('id'):
                return {
                    'base_url': base_url,
                    'api_key': api_key,
                    'model': str(m['id']),
                }
    return None


def _encode_image_data_url(image_path: Path) -> Optional[str]:
    try:
        raw = image_path.read_bytes()
    except OSError:
        return None
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime or not mime.startswith('image/'):
        mime = 'image/png'
    b64 = base64.b64encode(raw).decode('ascii')
    return f'data:{mime};base64,{b64}'


def call_openai_vision(
    image_path: Path,
    focus_hint: str,
    bridge_cfg: Dict[str, str],
    timeout: float = DEFAULT_IMAGE_TIMEOUT_SECS,
) -> Optional[str]:
    """Call an OpenAI-compatible chat/completions endpoint to describe one image."""
    data_url = _encode_image_data_url(image_path)
    if not data_url:
        return None

    instruction = (
        '请客观、精确地描述这张图片中的全部关键信息（UI 元素、报错文本、代码、数据或状态）。'
        '只陈述图中可见事实，不要推测不可见内容。'
    )
    if focus_hint.strip():
        instruction += f'\n用户当前关注上下文：{focus_hint.strip()[:500]}'

    body = json.dumps({
        'model': bridge_cfg['model'],
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': instruction},
                {'type': 'image_url', 'image_url': {'url': data_url}},
            ],
        }],
        'max_tokens': 800,
    }).encode('utf-8')

    headers = {'Content-Type': 'application/json'}
    if bridge_cfg.get('api_key'):
        headers['Authorization'] = f"Bearer {bridge_cfg['api_key']}"

    req = urllib.request.Request(
        f"{bridge_cfg['base_url']}/chat/completions",
        data=body,
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    choices = payload.get('choices') or []
    if not choices:
        return None
    msg = (choices[0].get('message') or {}).get('content')
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return None


def describe_images_for_text_model(
    image_paths: List[Path],
    focus_hint: str = '',
    bridge_cfg: Optional[Dict[str, str]] = None,
    describe_fn: Optional[Callable[[Path, str, Dict[str, str]], Optional[str]]] = None,
) -> str:
    """Translate attached images into a grounded text block for text-only models."""
    if not image_paths:
        return ''
    cfg = bridge_cfg if bridge_cfg is not None else resolve_vision_bridge_config()
    if not cfg:
        return (
            '\n\n[视觉桥提示：当前所选模型不支持直接看图，且未配置视觉转译模型；'
            f'用户附带了 {len(image_paths)} 张图片，请勿猜测图片内容，可提示用户切换到多模态模型或配置 vision_bridge。]'
        )

    fn = describe_fn or call_openai_vision
    blocks: List[str] = ['[视觉桥转译：用户附带了以下图片，已由多模态辅助模型客观转译为文字]']
    for idx, path in enumerate(image_paths[:MAX_BRIDGE_IMAGES], 1):
        desc = None
        try:
            desc = fn(path, focus_hint, cfg)
        except Exception:
            desc = None
        if desc:
            blocks.append(f'- 图片 #{idx} (`{path.name}`):\n{desc}')
        else:
            blocks.append(f'- 图片 #{idx} (`{path.name}`): [图片不可用，请勿推测内容]')
    return '\n'.join(blocks)

"""Read-only runtime health check. Does not print secrets or submit model prompts."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from workstation import store as db
from workstation.rpc import CodexRPC


async def main():
    db.init()
    cfg = db.config()
    port = os.environ.get('WORKSTATION_PORT', '8765')
    report = {}
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=15) as client:
        report['local_health'] = client.get('/healthz').json().get('ok') is True
        report['local_login_page'] = client.get('/').status_code == 200
        report['unauthenticated_api_denied'] = client.get('/api/state').status_code == 401
    async def noop(*args):
        pass
    codex_bin = cfg.get('codex_bin', '')
    if codex_bin and Path(codex_bin).is_file():
        rpc = CodexRPC(codex_bin, noop, noop)
        try:
            await rpc.start()
            account = await rpc.call('account/read', {})
            report['codex_handshake'] = True
            report['codex_auth_type'] = (account.get('account') or {}).get('type')
        except Exception as exc:
            report['codex_handshake'] = False
            report['codex_error'] = str(exc)
        finally:
            await rpc.close()
    else:
        report['codex_handshake'] = False
    public_url = (cfg.get('public_base_url') or os.environ.get('ROVE_PUBLIC_URL') or '').strip()
    if public_url:
        with httpx.Client(timeout=20) as client:
            response = client.get(public_url, follow_redirects=False)
            report['public_url_status'] = response.status_code
    report['sessions'] = len(db.query('SELECT id FROM sessions'))
    report['active_jobs'] = len(db.query("SELECT id FROM jobs WHERE status IN ('queued','running','waiting')"))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())

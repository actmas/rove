"""Deploy Nginx reverse-proxy gateway with pre-compressed static assets."""
import argparse
import base64
import gzip
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

CONFIG_TEMPLATE = '''# Rove Workstation Gateway ({domain})
map $http_upgrade $workstation_connection {{
    default upgrade;
    '' close;
}}
server {{
    listen 127.0.0.1:{listen_port};
    server_name {domain};
    access_log off;
    client_max_body_size 50m;
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_min_length 500;
    gzip_types text/plain text/css application/json application/javascript text/javascript image/svg+xml application/manifest+json;

    location ~ ^/(?:watch/)?assets/(.+)$ {{
        root /srv/web/rove/assets;
        gzip_static on;
        try_files /$1 @workstation_backend;
        expires 365d;
        add_header Cache-Control "public, max-age=31536000, immutable" always;
    }}

    location @workstation_backend {{
        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $http_cf_connecting_ip;
        proxy_set_header Connection "";
        proxy_buffering on;
        proxy_buffers 16 64k;
        proxy_buffer_size 32k;
    }}

    location / {{
        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_set_header Host {domain};
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $http_cf_connecting_ip;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $workstation_connection;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
}}
'''


def deploy(domain: str, upstream: str, ssh_host: str, ctid: str, listen_port: int):
    config_text = CONFIG_TEMPLATE.format(
        domain=domain,
        upstream=upstream.rstrip('/'),
        listen_port=listen_port,
    )
    assets_dir = ROOT / 'frontend/dist/assets'
    bundled_assets = {}
    if assets_dir.is_dir():
        for f in assets_dir.iterdir():
            if f.is_file() and f.suffix in ('.js', '.css'):
                raw = f.read_bytes()
                bundled_assets[f.name] = base64.b64encode(raw).decode('ascii')
                bundled_assets[f.name + '.gz'] = base64.b64encode(gzip.compress(raw, compresslevel=9)).decode('ascii')

    script = '''import base64, subprocess
from pathlib import Path
assets_root = Path('/srv/web/rove/assets')
assets_root.mkdir(parents=True, exist_ok=True)
for old_asset in assets_root.iterdir():
    if old_asset.is_file() and old_asset.name not in ASSETS:
        old_asset.unlink(missing_ok=True)
for name, b64 in ASSETS.items():
    (assets_root / name).write_bytes(base64.b64decode(b64))
for stale_bak in Path('/etc/nginx/conf.d').glob('walking-workstation.conf.bak-*'):
    stale_bak.unlink(missing_ok=True)
p = Path('/etc/nginx/conf.d/walking-workstation.conf')
previous = p.read_bytes() if p.exists() else None
backup = Path('/var/backups/walking-workstation.conf.bak')
if previous is not None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(previous)
p.write_text(CONFIG)
try:
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
except Exception:
    if previous is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(previous)
    raise
print(f'Installed gateway with {len(ASSETS)//2} local pre-compressed static bundles')
'''
    payload = f'CONFIG = {config_text!r}\nASSETS = {bundled_assets!r}\n{script}'
    cmd = ['ssh', '-o', 'BatchMode=yes', ssh_host]
    if ctid:
        cmd.extend(['pct', 'exec', str(ctid), '--'])
    cmd.extend(['python3', '-'])
    subprocess.run(cmd, input=payload.encode(), check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Deploy Nginx gateway for Rove Workstation')
    parser.add_argument('--domain', default=os.environ.get('ROVE_DOMAIN', 'rove.example.com'))
    parser.add_argument('--upstream', default=os.environ.get('ROVE_UPSTREAM', 'http://192.168.1.100:8765'))
    parser.add_argument('--host', default=os.environ.get('ROVE_GATEWAY_SSH', 'pve-home'))
    parser.add_argument('--ctid', default=os.environ.get('ROVE_GATEWAY_CTID', ''))
    parser.add_argument('--listen-port', type=int, default=int(os.environ.get('ROVE_GATEWAY_PORT', '8086')))
    args = parser.parse_args()
    deploy(args.domain, args.upstream, args.host, args.ctid, args.listen_port)

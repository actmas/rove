"""Local-only service configuration; passwords never enter chat or command arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workstation import store as db

LABEL = 'local.walking-workstation'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def install():
    db.init()
    logs = db.DATA / 'logs'
    logs.mkdir(exist_ok=True, mode=0o700)
    if sys.platform != 'darwin':
        service_path = Path('/etc/systemd/system/walking-workstation.service')
        unit = f'''[Unit]
Description=Rove Universal Multi-Agent Workstation Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={os.environ.get('USER', 'root')}
WorkingDirectory={db.ROOT}
Environment="PATH={Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="WORKSTATION_DATA={db.DATA}"
ExecStart={db.ROOT / '.venv/bin/python'} -m uvicorn workstation.server:app --host 0.0.0.0 --port 8765 --proxy-headers --forwarded-allow-ips=* --no-access-log
Restart=always
RestartSec=3
KillMode=process
StandardOutput=append:{logs / 'server.log'}
StandardError=append:{logs / 'server-error.log'}

[Install]
WantedBy=multi-user.target
'''
        service_path.write_text(unit, encoding='utf-8')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'walking-workstation.service'], check=True)
        print('已安装并启动 systemd 服务：walking-workstation.service')
        print('本机：http://127.0.0.1:8765')
        return

    trusted_proxies = os.environ.get('ROVE_TRUSTED_PROXIES', '127.0.0.1')
    value = {'Label': LABEL, 'ProgramArguments': [str(db.ROOT / '.venv/bin/python'), '-m', 'uvicorn',
              'workstation.server:app', '--host', '0.0.0.0', '--port', '8765', '--proxy-headers',
              '--forwarded-allow-ips', trusted_proxies, '--no-access-log'],
             'WorkingDirectory': str(db.ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
             'EnvironmentVariables': {'PATH': f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                      'PYTHONUNBUFFERED': '1', 'WORKSTATION_DATA': str(db.DATA)},
             'StandardOutPath': str(logs / 'server.log'), 'StandardErrorPath': str(logs / 'server-error.log'),
             'AbandonProcessGroup': True}
    PLIST.parent.mkdir(exist_ok=True)
    db.write_private(PLIST, plistlib.dumps(value).decode())
    target = f'gui/{os.getuid()}'
    # Detached task workers are intentionally outside this service's lifetime.
    subprocess.run(['launchctl', 'bootout', target + '/' + LABEL], capture_output=True)
    import time
    time.sleep(0.5)
    subprocess.run(['launchctl', 'bootstrap', target, str(PLIST)], check=True)
    subprocess.run(['launchctl', 'kickstart', target + '/' + LABEL], check=True)
    print('已安装登录后自启：' + LABEL)
    print('本机：http://127.0.0.1:8765')
    print('访问密码保存在：' + str(db.DATA / 'access-password.txt'))


def password():
    db.init()
    first = getpass.getpass('设置工作台访问密码（至少 12 位）：')
    if len(first) < 12 or first != getpass.getpass('再次输入：'):
        raise SystemExit('密码过短或两次不一致，未修改。')
    salt = secrets.token_hex(16)
    db.write_private(db.DATA / 'auth.json', json.dumps({'salt': salt, 'hash': db.password_hash(first, salt)}))
    db.execute('DELETE FROM logins')
    (db.DATA / 'access-password.txt').unlink(missing_ok=True)
    print('访问密码已更新，所有已登录设备需要重新登录。')


def backup():
    db.init()
    import sqlite3
    target = db.DATA / 'backups'
    target.mkdir(exist_ok=True, mode=0o700)
    import datetime
    path = target / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
    with db.connect() as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    os.chmod(path, 0o600)
    print('会话与输出已备份到：' + str(path))


def storage():
    from workstation import retention
    stats = retention.get_storage_stats()
    print("=" * 55)
    print("       Rove 行走工作台 · SQLite 存储健康报告")
    print("=" * 55)
    print(f"数据库文件: {stats['db_path']}")
    print(f"磁盘总占用: {stats['total_size_formatted']} (主库: {stats['db_size_formatted']}, WAL: {stats['wal_size_formatted']})")
    print("-" * 55)
    print("数据表占用概览 (按空间降序):")
    for name, info in sorted(stats['tables'].items(), key=lambda x: x[1]['bytes'], reverse=True):
        if info['count'] > 0:
            print(f"  - {name:<22}: {info['count']:>6} 行 | {info['size_formatted']:>10}")
    print("-" * 55)
    diag = stats['diagnostics']
    print("生命周期与清理评估:")
    print(f"  - 总会话数              : {diag['total_sessions']} 个 (置顶: {diag['pinned_sessions']} 个)")
    print(f"  - 活跃运行中任务        : {diag['active_jobs']} 个 (清理时绝对保护)")
    print(f"  - 超过 7 天未更新会话   : {diag['unpinned_old_7d']} 个")
    print(f"  - 超过 14 天未更新会话  : {diag['unpinned_old_14d']} 个")
    print(f"  - 可精简过程事件碎片    : {diag['compactable_events_total']} 条 (含流式 delta 与内嵌图片 blob)")
    print(f"  - 可清理过期登录态      : {diag['expired_logins']} 个")
    print("=" * 55)


def prune_cmd(args):
    from workstation import retention
    print(f"正在启动存储生命周期治理 (保留天数: {args.days} 天, 演练模式: {args.dry_run})...")
    res = retention.run_lifecycle_maintenance({
        'keep_days': args.days,
        'compact_events': not args.no_compact,
        'prune_sessions': args.prune_sessions,
        'vacuum': not args.no_vacuum,
        'backup_before': args.backup,
        'dry_run': args.dry_run,
    })
    if res.get('backup_path'):
        print(f"已创建自动快照: {res['backup_path']}")
    if res.get('dry_run'):
        print("【演练报告 (未实际修改数据库)】")
    else:
        print("【存储治理完成】")
    print(f"  - 过程事件精简 : {res['compact'].get('total_events_pruned', 0)} 条 (含图片 blob {res['compact'].get('images_compacted', 0)} 个)")
    print(f"  - 旧会话清理   : {res['sessions'].get('pruned_sessions', 0)} 个 (关联事件 {res['sessions'].get('pruned_events', 0)} 条)")
    print(f"  - 临时数据清理 : 过期登录 {res['ephemeral'].get('expired_logins', 0)} 个, 终端输出 {res['ephemeral'].get('old_terminal_outputs', 0)} 条")
    if not res.get('dry_run'):
        print(f"  - 物理磁盘释放 : {res.get('freed_formatted', '0 B')} (优化前 {res['size_before_formatted']} -> 优化后 {res['size_after_formatted']})")


def snapshot_export_cmd(args):
    from workstation import cluster_sync
    res = cluster_sync.create_snapshot(include_projects=not args.db_only, label=args.label)
    print(f"已创建全量容灾快照: {res['path']} ({res['size_formatted']}, 归档文件数: {res['manifest']['total_files_archived']})")


def snapshot_import_cmd(args):
    from workstation import cluster_sync
    path = Path(args.file).expanduser().resolve() if args.file else None
    if not path:
        snaps = cluster_sync.list_snapshots()
        if not snaps:
            raise SystemExit('未找到本地快照文件。')
        path = cluster_sync.snapshots_dir() / snaps[0]['filename']
    res = cluster_sync.restore_snapshot(
        path,
        restore_db=not args.heal_missing,
        restore_projects=True,
        only_missing_files=args.heal_missing,
    )
    print(f"已恢复快照 {res['snapshot']}: 恢复文件 {res['restored_files_total']} 个, 跨平台路径重映射 {res['remapped_paths_count']} 处")


def nas_sync_cmd(args):
    script_path = Path.home() / '.config' / 'walking-workstation' / 'sync_to_fnos.sh'
    key_path = Path.home() / '.ssh' / 'id_ed25519_nas_sync.pub'
    log_path = Path.home() / 'Library' / 'Logs' / 'nas_backup_daily.log'
    if args.key:
        if key_path.is_file():
            nas_target = os.environ.get('ROVE_NAS_HOST', 'admin@nas.local')
            print("=== NAS 专用授权公钥 (~/.ssh/id_ed25519_nas_sync.pub) ===")
            print(key_path.read_text().strip())
            print("\n一键导入至 NAS 命令：")
            print(f"ssh-copy-id -i {key_path} {nas_target}")
        else:
            print("公钥文件不存在，请先执行生成")
        return

    if args.status:
        if log_path.is_file():
            print("=== 最近同步日志 (~/Library/Logs/nas_backup_daily.log) ===")
            lines = log_path.read_text(errors='replace').splitlines()
            for line in lines[-25:]:
                print(line)
        else:
            print("尚未产生同步日志")
        return

    if not script_path.is_file():
        print(f"错误: 备份脚本 {script_path} 不存在")
        sys.exit(1)

    print("正在执行同步至飞牛 NAS...")
    subprocess.run(['/bin/bash', str(script_path)], check=True)


def pve_sync_cmd(args):
    from workstation import cluster_sync
    res = cluster_sync.push_snapshot_to_peer(include_projects=not args.db_only)
    print(f"PVE 节点双盘同步完成 ({res.get('transport')}): {res['snapshot']['filename']} ({res['snapshot']['size_formatted']})")
    if res.get('remote_path'):
        print(f"  └─ 盘 1 (主盘 NVMe): {res.get('remote_path')}")
    if res.get('secondary_path'):
        print(f"  └─ 盘 2 (备盘 独立存储池): {res.get('secondary_path')}")


def pve_restore_cmd(args):
    from workstation import cluster_sync
    res = cluster_sync.pull_and_restore_from_peer(
        restore_db=not args.heal_missing,
        restore_projects=True,
        only_missing_files=args.heal_missing,
    )
    print(f"已从 PVE 节点拉取并恢复: {res['snapshot']} (恢复文件 {res['restored_files_total']} 个)")


def pve_deploy_cmd(args):
    import tarfile
    import tempfile
    from workstation import cluster_sync
    host = cluster_sync.validate_ssh_host(args.host or 'pve-home')
    ctid = (args.ctid or '').strip()
    if ctid and not str(ctid).isdigit():
        raise ValueError('无效的 PVE CTID')
    print(f"正在打包当前通用工作台模版并部署至 PVE ({host}" + (f" CT {ctid}" if ctid else "") + ")...")
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        tmp_tar = Path(tmp.name)
    try:
        with tarfile.open(tmp_tar, 'w:gz') as tar:
            cluster_sync._add_directory_to_tar(tar, db.ROOT, 'walking-workstation')
        remote_cmd = (
            "mkdir -p /opt/walking-workstation && "
            "tar -xzf - --strip-components=1 -C /opt/walking-workstation && "
            "bash /opt/walking-workstation/deploy/pve/install_pve_node.sh"
        )
        ssh_cmd = (
            ['ssh', '-o', 'BatchMode=yes', '--', host, 'pct', 'exec', str(int(ctid)), '--', 'sh', '-c', remote_cmd]
            if ctid
            else ['ssh', '-o', 'BatchMode=yes', '--', host, remote_cmd]
        )
        with open(tmp_tar, 'rb') as f_in:
            subprocess.run(ssh_cmd, stdin=f_in, check=True)
    finally:
        tmp_tar.unlink(missing_ok=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Rove Service, Cluster & Storage Management')
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('install', help='安装或重启本机守护服务 (macOS LaunchAgent / Linux systemd)')
    subparsers.add_parser('password', help='重置访问密码并撤销已有登录')
    subparsers.add_parser('backup', help='创建 SQLite 数据库在线快照')
    subparsers.add_parser('status', help='检查服务运行状态')
    subparsers.add_parser('storage', help='查看 SQLite 存储与事件占用报告')

    prune_parser = subparsers.add_parser('prune', help='执行数据生命周期修剪与数据库 VACUUM 瘦身')
    prune_parser.add_argument('--days', type=int, default=14, help='保留最近 N 天的会话过程日志 (默认 14 天)')
    prune_parser.add_argument('--dry-run', action='store_true', help='仅评估待清理项，不实际修改数据库')
    prune_parser.add_argument('--no-compact', action='store_true', help='跳过过程事件 (delta/blob) 精简')
    prune_parser.add_argument('--prune-sessions', action='store_true', help='深度清理超过 N 天的未置顶旧会话')
    prune_parser.add_argument('--no-vacuum', action='store_true', help='修剪后跳过 SQLite VACUUM 磁盘回收')
    prune_parser.add_argument('--backup', action='store_true', help='修剪前自动创建安全备份快照')

    snap_exp = subparsers.add_parser('snapshot-export', help='创建包含数据库、自定义 Agent 与项目源码的全量自愈快照')
    snap_exp.add_argument('--db-only', action='store_true', help='仅打包数据库与配置，不含项目源码')
    snap_exp.add_argument('--label', default='cli', help='快照标签')

    snap_imp = subparsers.add_parser('snapshot-import', help='从快照文件恢复工作台状态与项目文件（支持跨平台路径自动映射）')
    snap_imp.add_argument('file', nargs='?', default='', help='快照 .tar.gz 路径（留空则使用最新快照）')
    snap_imp.add_argument('--heal-missing', action='store_true', help='仅自愈恢复缺失的项目文件，不覆盖现有数据库或已修改文件')

    pve_sync_p = subparsers.add_parser('pve-sync', help='将本机最新快照推送同步至 PVE 通用备用节点')
    pve_sync_p.add_argument('--db-only', action='store_true', help='仅同步数据库与配置')

    pve_rest_p = subparsers.add_parser('pve-restore', help='从 PVE 通用备用节点拉取快照并恢复至本机')
    pve_rest_p.add_argument('--heal-missing', action='store_true', help='仅自愈恢复缺失的项目文件')

    pve_dep_p = subparsers.add_parser('pve-deploy', help='通过 SSH 将当前工作台作为通用模版一键部署至 PVE 节点/LXC 容器')
    pve_dep_p.add_argument('--host', default='pve-home', help='PVE SSH 主机名或 IP (默认 pve-home)')
    pve_dep_p.add_argument('--ctid', default='', help='可选 PVE LXC 容器 ID (如 106/108)')

    nas_sync_p = subparsers.add_parser('nas-sync', help='触发或查看同步至飞牛 NAS (fnOS) 的状态与配置')
    nas_sync_p.add_argument('--status', action='store_true', help='查看最近一次同步日志')
    nas_sync_p.add_argument('--key', action='store_true', help='显示飞牛 NAS 授权所需的专用 SSH 公钥')

    args = parser.parse_args()
    if args.command == 'install': install()
    elif args.command == 'password': password()
    elif args.command == 'backup': backup()
    elif args.command == 'status':
        if sys.platform == 'darwin':
            subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'], check=True)
        else:
            subprocess.run(['systemctl', 'status', 'walking-workstation.service'], check=True)
    elif args.command == 'storage': storage()
    elif args.command == 'prune': prune_cmd(args)
    elif args.command == 'snapshot-export': snapshot_export_cmd(args)
    elif args.command == 'snapshot-import': snapshot_import_cmd(args)
    elif args.command == 'pve-sync': pve_sync_cmd(args)
    elif args.command == 'pve-restore': pve_restore_cmd(args)
    elif args.command == 'pve-deploy': pve_deploy_cmd(args)
    elif args.command == 'nas-sync': nas_sync_cmd(args)

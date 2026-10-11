"""Remove the superseded QQ runtimes, preserving account and chat data privately."""
from pathlib import Path
import json
import os
import shutil
import subprocess

assert os.geteuid() == 0
archive = Path('/opt/llbot/private/retired-state')
archive.mkdir(mode=0o700)
units = ['napcat-native-health.timer', 'napcat-native-health.service', 'napcat-native.service']
subprocess.run(['systemctl', 'disable', '--now', *units], check=True)
preserved = []
for source, name in [
    ('/opt/qq-ui-bridge-staging/private', 'qq-ui-private'),
    ('/opt/napcat-native/runtime', 'napcat-native-runtime'),
    ('/opt/napcat-native/home', 'napcat-native-home'),
    ('/opt/napcat-native/config-candidate', 'napcat-native-config'),
    ('/opt/napcat', 'napcat-account-data'),
    ('/etc/napcat', 'napcat-secrets'),
]:
    source = Path(source)
    if source.exists():
        assert not (archive / name).exists()
        shutil.move(source, archive / name)
        preserved.append(str(source))
removed = []
for source in ['/opt/qq-ui-bridge-staging', '/opt/napcat-native']:
    source = Path(source)
    if source.exists():
        shutil.rmtree(source)
        removed.append(str(source))
for source in [
    '/etc/systemd/system/napcat-native.service',
    '/etc/systemd/system/napcat-native-health.service',
    '/etc/systemd/system/napcat-native-health.timer',
    '/etc/systemd/system/timers.target.wants/napcat-native-health.timer',
    '/etc/systemd/system/qq-watchdog.service',
    '/etc/systemd/system/qq-watchdog.timer',
    '/etc/systemd/system/napcat-watch.service',
    '/etc/systemd/system/napcat.service.bak-pre-account-20260828T094123Z',
    '/usr/local/libexec/napcat-native-health',
    '/usr/local/libexec/napcat-account-health',
]:
    source = Path(source)
    if source.exists() or source.is_symlink():
        source.unlink()
        removed.append(str(source))
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['docker', 'network', 'rm', 'qq-ui-validation'], check=True)
old_images = [
    'qq-ui-official:pinned-v2-20260907', 'qq-ui-official:pinned-20260907',
    'qq-ui-official:validation-20260907', 'qq-ui-official:3.2.31-51102',
    'qq-ui-desktop:20260907-atspi', 'snowluma-native-replacement:r4-20260905',
    'snowluma-native-replacement:r3-20260905', 'snowluma-native-replacement:r2-20260905',
    'snowluma-native-replacement:20260905', 'snowluma-wrapper-abi:msf-20260905',
    'snowluma-wrapper-abi:direct-nocap-20260905', 'snowluma-wrapper-abi:direct-20260905',
    'snowluma-wrapper-abi:20260905',
]
for name in old_images:
    if subprocess.run(['docker', 'image', 'inspect', name], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run(['docker', 'image', 'rm', name], check=True, stdout=subprocess.DEVNULL)
        removed.append(name)
result = {'removed': removed, 'data_moved_to_private_archive': preserved,
          'archive': str(archive), 'docker_account_volumes_retained': True}
(archive / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))

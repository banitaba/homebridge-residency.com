#!/usr/bin/env python3
"""Root-installed, forced-command receiver for a static site (host deploy standard).

Server standard (same for every app on the host):
  code      /opt/apps/<app>/releases/<UTC>-<sha12>, /opt/apps/<app>/current -> release (nginx root)
  receiver  /usr/local/sbin/deploy-<app>   (this file; CI never updates it)
  ssh       <app>-deploy@host, forced command /usr/local/bin/<app>-ci-ssh
  logs      /var/log/apps/<app>-deploy.log

Only APP and PUBLIC_URL differ between static sites. Install by hand after every change:
  sudo install -o root -g root -m 0755 scripts/deployment/deploy-server.py /usr/local/sbin/deploy-<app>

CI runs:  ssh <app>-deploy@host "deploy <commit sha> <artifact sha256>" < release.tar.gz
Artifact: `git archive` of the repository (no .git) plus release.json.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sys
import tarfile
import tempfile
import time
import urllib.request

APP = 'homebridge-residency'
PUBLIC_URL = 'https://homebridge-residency.com/'
BASE = Path('/opt/apps') / APP
CURRENT = BASE / 'current'
RELEASES = BASE / 'releases'
LOG = Path('/var/log/apps') / f'{APP}-deploy.log'
KEEP_RELEASES = 3
MAX_ARCHIVE = 1024**3
MAX_EXPANDED = 2 * 1024**3


def command(value):
    match = re.fullmatch(r'deploy ([0-9a-f]{40}) ([0-9a-f]{64})', value)
    if not match:
        raise ValueError('Only deploy <commit SHA> <artifact SHA256> is permitted')
    return match.groups()


def log(message):
    print(message, flush=True)
    if os.geteuid() == 0:
        LOG.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with open(LOG, 'a') as handle:
            handle.write(f'{stamp} {message}\n')


def receive(archive, expected):
    total = 0
    digest = hashlib.sha256()
    signal.alarm(900)
    with archive.open('wb') as out:
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise ValueError('Artifact exceeds upload limit')
            digest.update(chunk)
            out.write(chunk)
    signal.alarm(0)
    if digest.hexdigest() != expected:
        raise ValueError('Artifact checksum mismatch')


def extract(archive, target):
    """Regular files and directories only; no dotfiles except .well-known; modes reset."""
    with tarfile.open(archive, 'r:gz') as tar:
        members, seen, expanded = [], set(), 0
        for member in tar:
            expanded += member.size
            if len(members) >= 100000 or expanded > MAX_EXPANDED:
                raise ValueError('Artifact exceeds unpacking limits')
            path = PurePosixPath(member.name)
            name = str(path)
            if name == '.':
                continue
            if (name in seen or path.is_absolute() or '..' in path.parts
                    or not (member.isdir() or member.isfile())):
                raise ValueError('Unsafe archive member')
            if any(part.startswith('.') and part != '.well-known' for part in path.parts):
                raise ValueError(f'Hidden files are not published: {name}')
            seen.add(name)
            members.append(member)
        tar.extractall(target, members=members, filter='data')
    for path in [target, *target.rglob('*')]:
        if os.geteuid() == 0:
            os.chown(path, 0, 0, follow_symlinks=False)
        path.chmod(0o755 if path.is_dir() else 0o644)


def switch(target):
    temporary = BASE / '.current-next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT)


def healthy(url, attempts=5):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def retain(active, previous):
    releases = sorted((p for p in RELEASES.iterdir() if p.is_dir()), reverse=True)
    keep = set(releases[:KEEP_RELEASES]) | {active, previous}
    for path in releases:
        if path not in keep:
            shutil.rmtree(path)


def deploy(sha, expected):
    os.umask(0o022)
    lock = open(f'/run/lock/{APP}-deploy.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if shutil.disk_usage(BASE).free < 3 * 1024**3:
        raise RuntimeError('At least 3 GiB free disk space is required')
    previous = CURRENT.resolve(strict=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    release = RELEASES / f'{stamp}-{sha[:12]}'
    with tempfile.TemporaryDirectory(prefix=f'{APP}-artifact-', dir='/var/tmp') as work:
        archive = Path(work) / 'release.tar.gz'
        receive(archive, expected)
        release.mkdir(mode=0o755)
        try:
            extract(archive, release)
            metadata = json.loads((release / 'release.json').read_text())
            if metadata.get('commit') != sha:
                raise ValueError('Artifact provenance mismatch')
            if not (release / 'index.html').is_file():
                raise ValueError('Incomplete release artifact: index.html')
        except BaseException:
            shutil.rmtree(release)
            raise
    switch(release)
    if not healthy(PUBLIC_URL):
        switch(previous)
        log(f'{release.name} failed the public check; switched back to {previous.name}')
        raise RuntimeError('public site check failed')
    (BASE / 'active-release.json').write_text(json.dumps({
        'app': APP, 'commit': sha, 'artifact_sha256': expected, 'release': str(release),
        'previous_release': str(previous), 'deployed_at': stamp, 'workflow': 'ci-deploy.yml'}, indent=2) + '\n')
    retain(release, previous)
    log(f'deployed {sha} as {release.name}')


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Use the installed restricted sudo entrypoint')
    if len(sys.argv) != 2:
        raise ValueError('Expected one forced SSH command')
    sha, expected = command(sys.argv[1])
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def upload_timeout(signum, frame):
        raise TimeoutError('Artifact upload timed out')
    signal.signal(signal.SIGALRM, upload_timeout)
    deploy(sha, expected)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Deployment failed: ' + str(error), file=sys.stderr)
        try:
            log('FAILED: ' + str(error))
        except Exception:
            pass
        sys.exit(1)

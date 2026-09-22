#!/usr/bin/env python3
"""Persistent user installation, desktop integration, and login maintenance."""
import ast
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

SOURCE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'thesolution-pm'
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
APP = DATA / 'app'
SETTINGS = CONFIG / 'goinfre/automation.json'
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'thesolution-pm' / socket.gethostname()
BIN = Path(os.environ.get('THESOLUTION_BIN_DIR', Path.home() / '.local/bin'))
FILES = ('bootstrap.py', 'goinfre.py', 'packages.conf', 'check_links.py', 'requirements.txt', 'package.json')
DEFAULTS = {'enabled': True, 'auto_update': True, 'self_update': True, 'interval_hours': 24}
REMOTE = 'https://raw.githubusercontent.com/H0MZ0/theSolution/'


def read_settings():
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text())}
    except FileNotFoundError:
        return DEFAULTS.copy()


def save_settings(settings):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(SETTINGS, json.dumps(settings, indent=2))


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)


def version(directory):
    try:
        return tuple(int(n) for n in json.loads((directory / 'package.json').read_text())['version'].split('.'))
    except (OSError, ValueError, KeyError):
        return (0, 0, 0)


def desktop_arg(value):
    # Exec quoting, then Desktop Entry string escaping; percent signs are field codes.
    value = ''.join('\\' + c if c in '\\"`$' else c for c in str(value))
    return '"' + value.replace('\\', '\\\\').replace('%', '%%') + '"'


def setup(source=SOURCE, start=True):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Keep a newer self-updated runtime if an older npm installation is launched.
        if source != APP and version(source) >= version(APP):
            stage = Path(tempfile.mkdtemp(prefix='app-stage-', dir=DATA))
            backup = DATA / 'app-previous'
            try:
                for name in FILES:
                    shutil.copy2(source / name, stage / name)
                shutil.rmtree(backup, ignore_errors=True)
                if APP.exists():
                    APP.rename(backup)
                try:
                    stage.rename(APP)
                except Exception:
                    if backup.exists():
                        backup.rename(APP)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
        BIN.mkdir(parents=True, exist_ok=True)
        launcher = BIN / 'thesolution-pm'
        # Resolve Python on each machine; the original machine's venv may not exist.
        script = '#!/bin/sh\nexec "${GOINFRE_PYTHON:-python3}" ' + shlex.quote(str(APP / 'bootstrap.py')) + ' "$@"\n'
        atomic_write(launcher, script)
        launcher.chmod(0o755)
        # Avoid overwriting an npm-owned symlink when npm prefix is ~/.local.
        alias = BIN / 'theSolution'
        if not alias.is_symlink():
            atomic_write(alias, script)
            alias.chmod(0o755)
        applications = DATA.parent / 'applications'
        desktop = '[Desktop Entry]\nType=Application\nName=theSolution\nComment=Manage goinfre applications\nExec=' + desktop_arg(launcher) + '\nTerminal=true\nIcon=system-software-install\nCategories=Settings;PackageManager;\n'
        atomic_write(applications / 'thesolution.desktop', desktop)
        settings = read_settings()
        save_settings(settings)
        autostart = CONFIG / 'autostart/thesolution-maintenance.desktop'
        if settings['enabled']:
            atomic_write(autostart, '[Desktop Entry]\nType=Application\nName=theSolution maintenance\nExec=' + desktop_arg(launcher) + ' --background\nTerminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n')
        else:
            autostart.unlink(missing_ok=True)
        # Replace the prior project's restore entry to avoid duplicate workers.
        old = CONFIG / 'autostart/goinfre-auto.desktop'
        if old.exists() and ('goinfre.py' in old.read_text() or 'thesolution' in old.read_text()):
            old.unlink()
        if shutil.which('update-desktop-database'):
            subprocess.run(['update-desktop-database', str(applications)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if start and settings['enabled'] and not os.environ.get('THESOLUTION_NO_START'):
        start_background()


def start_background():
    if os.environ.get('THESOLUTION_NO_START'):
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    log = CACHE / 'maintenance.log'
    if log.exists() and log.stat().st_size > 1024 * 1024:
        log.replace(CACHE / 'maintenance.previous.log')
    with log.open('a') as output:
        subprocess.Popen([sys.executable, str(APP / 'bootstrap.py'), '--background'],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         start_new_session=True, close_fds=True)


def request_text(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'thesolution-pm'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8')


def update_self():
    # Fetch every file from one commit, never a mixture of changing main revisions.
    commit = json.loads(request_text('https://api.github.com/repos/H0MZ0/theSolution/commits/main'))['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise RuntimeError('Invalid upstream commit ID')
    manifest = json.loads(request_text(REMOTE + commit + '/package.json'))
    incoming = tuple(int(n) for n in manifest['version'].split('.'))
    if incoming <= version(APP):
        print('[OK] Manager is current.', flush=True)
        return
    with tempfile.TemporaryDirectory(prefix='update-', dir=DATA) as temporary:
        stage = Path(temporary)
        for name in FILES:
            text = request_text(REMOTE + commit + '/' + name)
            if name.endswith('.py'):
                ast.parse(text)
            if name == 'package.json':
                json.loads(text)
            (stage / name).write_text(text)
        # Use the new bootstrap to apply any new integration logic.
        subprocess.run([sys.executable, str(stage / 'bootstrap.py'), '--setup', '--no-start'], check=True)
    print('[OK] Updated theSolution to ' + manifest['version'], flush=True)


def core_args(settings):
    args = ['--auto']
    if settings['auto_update']:
        args.append('--update')
    if settings.get('catalog'):
        args += ['--config', settings['catalog']]
    return args


def background():
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / 'maintenance.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        next_run = 0
        while read_settings()['enabled']:
            settings = read_settings()
            if time.monotonic() >= next_run:
                print(time.strftime('[%Y-%m-%d %H:%M:%S] Checking updates and missing apps'), flush=True)
                if settings['self_update']:
                    try:
                        update_self()
                    except Exception as error:
                        print(f'[WARN] Manager update unavailable; keeping current copy: {error}', flush=True)
                result = subprocess.run([sys.executable, str(APP / 'goinfre.py'), *core_args(settings)])
                # Retry offline/interrupted application updates after 15 minutes.
                next_run = time.monotonic() + (max(1, settings['interval_hours']) * 3600 if result.returncode == 0 else 900)
            time.sleep(60)
    return 0


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11+ is required')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('The catalog requires Linux x86-64')
    args = sys.argv[1:]
    if '--version' in args or '-v' in args:
        print('.'.join(map(str, max(version(SOURCE), version(APP)))))
        return 0
    if '--setup' in args:
        setup(start='--no-start' not in args)
        print(f'Installed: {BIN / "thesolution-pm"}\nDesktop launcher and login maintenance configured.')
        return 0
    if '--background' in args:
        return background()
    switches = {'--disable-autostart': ('enabled', False), '--enable-autostart': ('enabled', True),
                '--disable-auto-update': ('auto_update', False), '--enable-auto-update': ('auto_update', True),
                '--disable-self-update': ('self_update', False), '--enable-self-update': ('self_update', True)}
    for flag, (key, value) in switches.items():
        if flag in args:
            settings = read_settings()
            settings[key] = value
            save_settings(settings)
            setup(start=value)
            print(f'{key} = {value}')
            return 0
    if '--status' in args:
        print(json.dumps(read_settings(), indent=2))
        print(f'Log: {CACHE / "maintenance.log"}')
        return 0
    if '--export-profile' in args or '--import-profile' in args:
        flag = '--export-profile' if '--export-profile' in args else '--import-profile'
        index = args.index(flag)
        if index + 1 >= len(args):
            raise RuntimeError(f'{flag} needs a file path')
        profile = Path(args[index + 1]).expanduser().resolve()
        state = CONFIG / 'goinfre/state.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        with (state.parent / 'packages.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            desired = json.loads(state.read_text()).get('desired', []) if state.exists() else []
            if flag == '--export-profile':
                atomic_write(profile, json.dumps({'desired': desired}, indent=2))
                print(f'Application list exported to {profile}')
                return 0
            incoming = json.loads(profile.read_text()).get('desired')
            if not isinstance(incoming, list) or not all(isinstance(n, str) and re.fullmatch(r'[A-Za-z0-9_-]+', n) for n in incoming):
                raise RuntimeError('Profile must contain a list of application names')
            atomic_write(state, json.dumps({'desired': sorted(set(desired + incoming))}, indent=2))
        setup()
        print('Profile imported. Login maintenance will restore these applications; use --update to restore now.')
        return 0
    if '--help' in args or '-h' in args:
        print('theSolution: --setup, --auto, --update, --update-self, --check-links, --status\n'
              'Automation: --enable/disable-autostart, --enable/disable-auto-update, --enable/disable-self-update\n'
              'Catalog options: --config PATH, --install-root PATH\n'
              'Different accounts: --export-profile FILE, --import-profile FILE\n'
              'Desktop launcher opens the terminal interface. Login maintenance restores and updates apps every 24 hours.')
        return 0
    if '--check-links' in args:
        return subprocess.call([sys.executable, str(SOURCE / 'check_links.py'), *[a for a in args if a != '--check-links']])
    if SOURCE != APP and version(APP) > version(SOURCE):
        return subprocess.call([sys.executable, str(APP / 'bootstrap.py'), *args])
    if '--config' in args:
        index = args.index('--config')
        if index + 1 >= len(args):
            raise RuntimeError('--config needs a path')
        catalog = Path(args[index + 1]).expanduser().resolve()
        if not catalog.is_file():
            raise RuntimeError(f'Catalog does not exist: {catalog}')
        settings = read_settings()
        settings['catalog'] = str(catalog)
        save_settings(settings)
    if '--install-root' in args:
        index = args.index('--install-root')
        if index + 1 >= len(args):
            raise RuntimeError('--install-root needs a path')
        # Persist before starting the worker so it restores to the selected location.
        sys.path.insert(0, str(SOURCE))
        import goinfre
        goinfre._write_config_path(Path(args[index + 1]).expanduser().resolve())
    if not APP.exists() or SOURCE != APP:
        setup()
    elif read_settings()['enabled']:
        start_background()
    if '--update-self' in args:
        update_self()
        return 0
    if '--config' not in args and read_settings().get('catalog'):
        args += ['--config', read_settings()['catalog']]
    python = sys.executable
    if not any(flag in args for flag in ('--auto', '-a', '--update')):
        # Host-specific environments avoid broken cross-machine Python symlinks.
        venv = DATA / 'venvs' / socket.gethostname() / f'python{sys.version_info.major}.{sys.version_info.minor}'
        python = str(venv / 'bin/python')
        requirement = (APP / 'requirements.txt').read_text().strip().split('==')[1]
        probe = 'import importlib.metadata, sys; sys.exit(importlib.metadata.version("textual") != ' + repr(requirement) + ')'
        venv.parent.mkdir(parents=True, exist_ok=True)
        with (venv.parent / 'setup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not Path(python).exists() or subprocess.run([python, '-c', probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                print('Preparing Python environment...', flush=True)
                subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
                subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(APP / 'requirements.txt')], check=True)
    return subprocess.call([python, str(APP / 'goinfre.py'), *args])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'theSolution: {error}', file=sys.stderr)
        raise SystemExit(1)

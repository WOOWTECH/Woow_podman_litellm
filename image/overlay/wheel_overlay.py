#!/usr/bin/env python3
"""以離線、雜湊鎖定的 wheel 取代映像內已安裝的 Python 套件（商用驗收 X-FIX-CVE）。

為什麼不直接 pip／uv install：
  * 基底映像沒有 pip／uv（LiteLLM 基底連 pip 都沒有），build 期也不保證能連 PyPI。
  * wheel 已隨 repo 提交，並在 overlay.lock.json 以 sha256 鎖定；本腳本逐一驗雜湊、
    驗 zip 成員路徑安全、只取代鎖定清單內的套件，完全離線、可重現。

用法：
  wheel_overlay.py apply  --lock overlay.lock.json --wheels DIR [--site-packages DIR]
  wheel_overlay.py verify --lock overlay.lock.json [--site-packages DIR]

apply：
  1. 驗每個 wheel 的 sha256 與檔名；驗 zip 成員（不得絕對路徑、不得 ..、不得 symlink）
  2. 刪掉該套件舊的 *.dist-info 與 RECORD 列出的舊檔（含空目錄、__pycache__）
  3. 解壓新 wheel、寫 INSTALLER、把擁有者與權限對齊 site-packages 目錄
  任何一步失敗就非零結束（Docker build 因此失敗），不會產出「看起來改了其實沒改」的映像。
verify：
  逐一確認安裝版本＝鎖定版本、該套件只剩一個 dist-info、可以 import，
  且有編譯擴充的套件實際載入的是編譯版（不是純 Python 後備）。
"""
import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import platform
import re
import shutil
import stat
import sys
import zipfile


def die(message):
    print('wheel_overlay: ERROR: ' + message, file=sys.stderr)
    raise SystemExit(1)


def norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def load_lock(path):
    lock = json.loads(pathlib.Path(path).read_text())
    if not isinstance(lock, dict) or not isinstance(lock.get('packages'), list) or not lock['packages']:
        die('lock 格式錯誤：需要 {"packages": [...]}')
    seen = set()
    for pkg in lock['packages']:
        for key in ('name', 'version', 'wheel', 'sha256', 'machine', 'module'):
            if not isinstance(pkg.get(key), str) or not pkg[key]:
                die('lock 欄位缺漏：%s %s' % (pkg.get('name'), key))
        if not re.fullmatch('[0-9a-f]{64}', pkg['sha256']):
            die('sha256 格式錯誤：' + pkg['name'])
        if norm(pkg['name']) in seen:
            die('lock 內套件重複：' + pkg['name'])
        seen.add(norm(pkg['name']))
        if pkg['wheel'] != os.path.basename(pkg['wheel']) or not pkg['wheel'].endswith('.whl'):
            die('wheel 檔名不安全：' + pkg['wheel'])
    return lock


def site_packages(explicit, lock):
    if explicit:
        path = pathlib.Path(explicit)
    else:
        # 以鎖定清單第一個套件目前實際載入的位置推 site-packages
        spec = importlib.util.find_spec(lock['packages'][0]['module'])
        if spec is None or not spec.origin:
            die('找不到 %s 目前的安裝位置' % lock['packages'][0]['module'])
        path = pathlib.Path(spec.origin).resolve().parent.parent
    if not path.is_dir():
        die('site-packages 不存在：%s' % path)
    return path


def safe_members(zf, wheel):
    for info in zf.infolist():
        name = info.filename
        parts = pathlib.PurePosixPath(name).parts
        if name.startswith('/') or '..' in parts or '\\' in name or (parts and ':' in parts[0]):
            die('%s 含不安全路徑：%s' % (wheel, name))
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            die('%s 含 symlink：%s' % (wheel, name))
        if parts and parts[0].endswith('.data'):
            die('%s 含 .data 目錄（本工具不處理 scripts／data）：%s' % (wheel, name))
        yield info


def remove_old(sp, pkg):
    """刪除舊安裝：依 RECORD 刪檔，再清掉 dist-info 與空目錄。"""
    target = norm(pkg['name'])
    removed = 0
    for dist_info in sorted(sp.glob('*.dist-info')):
        stem = dist_info.name[:-len('.dist-info')]
        if norm(stem.rsplit('-', 1)[0]) != target:
            continue
        record = dist_info / 'RECORD'
        dirs = set()
        if record.is_file():
            for line in record.read_text().splitlines():
                rel = line.split(',', 1)[0]
                if not rel or rel.startswith('/') or '..' in pathlib.PurePosixPath(rel).parts:
                    continue
                path = sp / rel
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed += 1
                    dirs.add(path.parent)
        shutil.rmtree(dist_info)
        for directory in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
            cache = directory / '__pycache__'
            if cache.is_dir():
                shutil.rmtree(cache)
            while directory != sp and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                directory = directory.parent
    # 頂層套件目錄可能還留下 __pycache__ 或 RECORD 沒列到的編譯檔
    top = sp / pkg['module'].split('.')[0]
    if top.is_dir():
        shutil.rmtree(top)
    return removed


def apply(args):
    lock = load_lock(args.lock)
    wheels = pathlib.Path(args.wheels)
    sp = site_packages(args.site_packages, lock)
    st = sp.stat()
    machine = platform.machine().lower().replace('amd64', 'x86_64')
    for pkg in lock['packages']:
        if pkg['machine'] not in ('any', machine):
            die('%s 的鎖定 wheel 只支援 %s，目前是 %s' % (pkg['name'], pkg['machine'], machine))
        path = wheels / pkg['wheel']
        if not path.is_file():
            die('找不到 wheel：' + str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != pkg['sha256']:
            die('%s sha256 不符（預期 %s，實際 %s）' % (pkg['wheel'], pkg['sha256'], digest))
    for pkg in lock['packages']:
        path = wheels / pkg['wheel']
        with zipfile.ZipFile(path) as zf:
            infos = list(safe_members(zf, pkg['wheel']))
            dist_infos = {pathlib.PurePosixPath(i.filename).parts[0] for i in infos
                          if pathlib.PurePosixPath(i.filename).parts[0].endswith('.dist-info')}
            if len(dist_infos) != 1:
                die('%s 需要恰好一個 dist-info' % pkg['wheel'])
            metadata = zf.read(next(iter(dist_infos)) + '/METADATA').decode('utf-8', 'replace')
            match = re.search(r'^Version: (.+)$', metadata, re.M)
            if not match or match.group(1).strip() != pkg['version']:
                die('%s METADATA 版本與 lock 不符' % pkg['wheel'])
            removed = remove_old(sp, pkg)
            for info in infos:
                dest = sp / info.filename
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
            (sp / next(iter(dist_infos)) / 'INSTALLER').write_text('wheel_overlay\n')
        # 擁有者／權限對齊 site-packages（以 root 身分 build 時才有意義）
        for name in {pathlib.PurePosixPath(i.filename).parts[0] for i in infos}:
            for root, dirs, files in os.walk(sp / name):
                for entry in [root] + [os.path.join(root, f) for f in files]:
                    try:
                        os.chown(entry, st.st_uid, st.st_gid)
                    except (PermissionError, OSError):
                        pass
                os.chmod(root, 0o755)
        print('wheel_overlay: %s %s 已套用（刪除舊檔 %d 個）' % (pkg['name'], pkg['version'], removed))
    print('wheel_overlay: apply OK（%d 個套件）' % len(lock['packages']))


def verify(args):
    lock = load_lock(args.lock)
    sp = site_packages(args.site_packages, lock)
    sp_resolved = sp.resolve()
    for pkg in lock['packages']:
        dists = [d for d in importlib.metadata.distributions(path=[str(sp)])
                 if norm(d.metadata['Name'] or '') == norm(pkg['name'])]
        if len(dists) != 1:
            die('%s 應恰有一個安裝，實際 %d 個' % (pkg['name'], len(dists)))
        if dists[0].version != pkg['version']:
            die('%s 版本 %s ≠ 鎖定 %s' % (pkg['name'], dists[0].version, pkg['version']))
        module = importlib.import_module(pkg['module'])
        origin = pathlib.Path(module.__file__).resolve()
        if sp_resolved not in origin.parents:
            die('%s 實際載入自 %s，不在 %s' % (pkg['module'], origin, sp_resolved))
        for ext in pkg.get('compiled_modules', []):
            mod = importlib.import_module(ext)
            if not str(getattr(mod, '__file__', '')).endswith(('.so', '.pyd')):
                die('%s 沒有載入編譯擴充（落到純 Python 後備？）' % ext)
        print('wheel_overlay: verify %s %s OK' % (pkg['name'], pkg['version']))
    print('wheel_overlay: verify OK（%d 個套件）' % len(lock['packages']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('apply', 'verify'):
        p = sub.add_parser(name)
        p.add_argument('--lock', required=True)
        p.add_argument('--site-packages')
        if name == 'apply':
            p.add_argument('--wheels', required=True)
    args = parser.parse_args(argv)
    {'apply': apply, 'verify': verify}[args.cmd](args)


if __name__ == '__main__':
    main()

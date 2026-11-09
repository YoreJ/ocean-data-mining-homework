"""Scan staged files and Git history without displaying credential values.

Local credential files are read only to obtain exact values to exclude from the
public repository. They are never copied into scan reports or Git objects.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{35,})'),
    'api_key': re.compile(rb'sk-(?:ant-|proj-)?[A-Za-z0-9_-]{25,}'),
    'bearer_value': re.compile(rb'(?i)Authorization[\"\s:]+Bearer\s+[A-Za-z0-9_./+=-]{25,}'),
}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def known_credentials():
    values = set()
    for key, value in os.environ.items():
        if any(word in key.upper() for word in ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD')) and len(value) >= 12:
            values.add(value.encode())
    for path in (Path.home()/'.claude/settings.json', Path.home()/'.codex/auth.json', ROOT/'model_config.local.json'):
        if not path.is_file():
            continue
        try:
            node = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        def visit(item):
            if isinstance(item, dict):
                for key, value in item.items():
                    if isinstance(value, str) and any(word in key.lower() for word in ('token', 'secret', 'password', 'api_key')):
                        if len(value) >= 12 and not key.endswith('_env'):
                            values.add(value.encode())
                    elif isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(item, list):
                for value in item:
                    visit(value)
        visit(node)
    return values


def main():
    secrets = known_credentials()
    violations = []
    files = git('ls-files', '-z').decode().split('\0')
    files = [name for name in files if name]
    for name in files:
        if name == 'model_config.local.json' or '.private_build/' in name or name.endswith(('.key', '.pem')):
            violations.append({'scope': 'index', 'file': name, 'reason': 'private_file_name'})
        data = git('show', ':'+name)
        reasons = [key for key, pattern in PATTERNS.items() if pattern.search(data)]
        if any(value in data for value in secrets):
            reasons.append('exact_local_credential')
        if reasons:
            violations.append({'scope': 'index', 'file': name, 'reasons': reasons})
    commits = git('rev-list', '--all').decode().splitlines()
    blobs = {}
    if commits:
        for line in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, _, name = line.partition(' ')
            if git('cat-file', '-t', oid).strip() == b'blob':
                blobs[oid] = name
        for oid, name in blobs.items():
            data = git('cat-file', 'blob', oid)
            reasons = [key for key, pattern in PATTERNS.items() if pattern.search(data)]
            if any(value in data for value in secrets):
                reasons.append('exact_local_credential')
            if reasons:
                violations.append({'scope': 'history', 'object': oid, 'file': name, 'reasons': reasons})
    print(json.dumps({'staged_files': len(files), 'commits_scanned': len(commits),
                      'history_blobs_scanned': len(blobs), 'credential_values_loaded': bool(secrets),
                      'violations': violations, 'credential_values_disclosed': False}, ensure_ascii=False, indent=2))
    return bool(violations)


if __name__ == '__main__':
    sys.exit(main())

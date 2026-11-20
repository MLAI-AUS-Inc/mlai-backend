#!/usr/bin/env python3
"""Check the feature-flag register against the flags the code reads (plan step F15).

Several prevention and policy flags sat default-off with no owner and no end
date; that is how already-built fixes stayed inert. ``flag-register.json`` at
the repository root lists every flag-shaped setting the code reads with its
default, an owning team and an expiry date. This checker fails when:

- the code reads a flag-shaped name that has no register row;
- a register row has no owner, or no expiry (an ISO date, or ``permanent``
  together with a written reason);
- a register row names a flag the code no longer reads (delete the row with
  the flag);
- the register's default disagrees with the single literal default the code
  declares for that name.

A row past its expiry date is reported as a warning: the owner should remove
the flag or move the date with a reason.

What counts as a read: any string literal in production code that is exactly a
flag-shaped name (``os.getenv("X")``, a helper call, a tuple of names read in a
loop, a pydantic ``validation_alias``), an undecorated field of a configured
settings class, a module-level assignment in a configured settings module, and
``settings.X`` attribute access. Names that are shaped like flags but are not
settings (failure codes, values written into a child process environment) sit
in the register's ``not_flags`` list with a reason.

The same file is kept in mlai-backend and content-factory; the scan scope and
name pattern live in each repository's register, so the script itself is
identical. ``--write`` adds rows for unregistered names with an empty owner
and expiry for the author to fill in; it never invents an owner or a date.
"""
from __future__ import annotations

import argparse
import ast
from datetime import date
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
REGISTER = 'flag-register.json'
PERMANENT = 'permanent'
NO_DEFAULT = '(unset)'
VARIES = '(varies)'


def _literal_default(node):
    """Normalise a literal default to the register's string form, or None."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if value is None:
            return NO_DEFAULT
        if isinstance(value, (int, float, str)):
            return str(value)
    if isinstance(node, (ast.Tuple, ast.List)) and not node.elts:
        return ''
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        inner = _literal_default(node.operand)
        return None if inner is None else '-' + inner
    return None


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


_ENV_WRITERS = {'setenv', 'delenv', 'putenv', 'unsetenv', 'setdefault'}


def _is_env_read(node, name):
    """os.getenv / os.environ.get / environ.get, or a local helper such as _env_int("X", 3)."""
    if name in _ENV_WRITERS:
        return False
    if name == 'get' and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        return (isinstance(owner, ast.Name) and owner.id == 'environ') or (
            isinstance(owner, ast.Attribute) and owner.attr == 'environ')
    return 'env' in name.lower()


class _Scanner(ast.NodeVisitor):
    def __init__(self, pattern, path, record, *, settings_module=False, settings_classes=(), inert=None):
        self.pattern = pattern
        self.inert = inert if inert is not None else []
        self.path = path
        self.record = record
        self.settings_module = settings_module
        self.settings_classes = set(settings_classes)
        self.consumed = set()

    def _note(self, name, default=None, *, declared=False):
        self.record(name, self.path, default, declared)

    def visit_Call(self, node):
        name = _call_name(node)
        args = list(node.args)
        env_read = _is_env_read(node, name)
        if name == 'getattr' and len(args) >= 2:
            # getattr(settings, "X", fallback): a read, but the fallback is not the declared default.
            args = args[1:]
        if (env_read or name == 'getattr') and args and isinstance(args[0], ast.Constant) \
                and isinstance(args[0].value, str) and self.pattern.fullmatch(args[0].value):
            default = args[1] if len(args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == 'default':
                    default = keyword.value
            # A read with no literal default (``getenv("X") or FALLBACK``) declares nothing checkable.
            literal = _literal_default(default) if default is not None else None
            self._note(args[0].value, literal, declared=env_read)
            self.consumed.add(id(args[0]))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if id(node) not in self.consumed and isinstance(node.value, str) and self.pattern.fullmatch(node.value):
            self._note(node.value)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == 'settings' and self.pattern.fullmatch(node.attr):
            self._note(node.attr)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        if node.name in self.settings_classes:
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    self._settings_field(statement)
        self.generic_visit(node)

    def _settings_field(self, statement):
        """A pydantic-settings field reads its validation_alias (or alias), else its own name.

        ``Field(env=...)`` is a pydantic v1 keyword that v2 ignores: the field still
        reads its own upper-cased name. The ``env=`` name is reported as inert.
        """
        value = statement.value
        keywords = value.keywords if isinstance(value, ast.Call) else []
        default = value
        if isinstance(value, ast.Call) and _call_name(value) == 'Field':
            default = next((k.value for k in keywords if k.arg == 'default'), value.args[0] if value.args else None)
        literal = _literal_default(default)
        own_name = statement.target.id.upper()
        aliases = [k for k in keywords if k.arg in {'validation_alias', 'alias'}]
        for keyword in aliases:
            for item in ast.walk(keyword.value):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    self.consumed.add(id(item))
                    if self.pattern.fullmatch(item.value):
                        self._note(item.value, literal, declared=True)
        if not aliases and self.pattern.fullmatch(own_name):
            self._note(own_name, literal, declared=True)
        for keyword in keywords:
            if keyword.arg == 'env' and isinstance(keyword.value, ast.Constant) \
                    and isinstance(keyword.value.value, str):
                self.consumed.add(id(keyword.value))
                if keyword.value.value != own_name and not aliases:
                    self.inert.append(f'{self.path}: Field(env="{keyword.value.value}") is ignored by '
                                      f'pydantic v2; the setting reads {own_name}')

    def visit_Module(self, node):
        if self.settings_module:
            for statement in node.body:
                targets = statement.targets if isinstance(statement, ast.Assign) else (
                    [statement.target] if isinstance(statement, ast.AnnAssign) else [])
                for target in targets:
                    if isinstance(target, ast.Name) and self.pattern.fullmatch(target.id):
                        self._note(target.id)
        self.generic_visit(node)


def _tracked_python(root):
    try:
        output = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '*.py'],
                                         cwd=root, text=True, stderr=subprocess.DEVNULL)
        return sorted(set(output.splitlines()))
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(path.relative_to(root)) for path in Path(root).rglob('*.py'))


def _excluded(path, scan):
    roots = [item.strip('/') for item in scan.get('roots') or ['']]
    if not any(not item or path == item or path.startswith(item + '/') for item in roots):
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in scan.get('exclude') or [])


def scan_reads(root, scan, inert_out=None):
    """Map each flag-shaped name the code reads to its read sites and declared defaults."""
    pattern = re.compile(scan['pattern'])
    settings_modules = set(scan.get('settings_modules') or [])
    settings_classes = {}
    for entry in scan.get('settings_classes') or []:
        settings_classes.setdefault(entry['file'], []).append(entry['class'])
    reads = {}
    inert = []

    def record(name, path, default, declared):
        row = reads.setdefault(name, {'files': set(), 'defaults': set()})
        row['files'].add(path)
        if declared and default is not None:
            row['defaults'].add(default)

    for path in _tracked_python(root):
        if _excluded(path, scan):
            continue
        try:
            source = (Path(root) / path).read_text(encoding='utf-8')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                tree = ast.parse(source, filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        _Scanner(pattern, path, record, settings_module=path in settings_modules,
                 settings_classes=settings_classes.get(path, ()), inert=inert).visit(tree)
    if inert_out is not None:
        inert_out.extend(inert)
    return reads


def code_default(read):
    defaults = read['defaults']
    if not defaults:
        return None
    return next(iter(defaults)) if len(defaults) == 1 else VARIES


def _valid_expiry(value):
    if value == PERMANENT:
        return True
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value))


def check(register, reads, *, today=None):
    """Return (errors, warnings). Pure over a loaded register and a scan result."""
    today = today or date.today()
    errors, notes = [], []
    rows = register.get('flags')
    if not isinstance(rows, list):
        return ['Register has no "flags" list'], notes
    not_flags = {}
    for item in register.get('not_flags') or []:
        name = item.get('name') if isinstance(item, dict) else None
        if not name or not str(item.get('reason') or '').strip():
            errors.append(f'not_flags entry {name or item!r} needs a name and a reason')
            continue
        not_flags[name] = item
    seen = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get('name') or '').strip():
            errors.append(f'Register row {index} has no name')
            continue
        name = row['name']
        if name in seen:
            errors.append(f'{name}: listed twice in the register')
            continue
        seen[name] = row
        if name in not_flags:
            errors.append(f'{name}: listed both as a flag and under not_flags')
        if not str(row.get('owner') or '').strip():
            errors.append(f'{name}: no owner (name a team or role, "platform" when unknown)')
        expiry = str(row.get('expiry') or '').strip()
        if not expiry:
            errors.append(f'{name}: no expiry (an ISO date, or "permanent" with a reason)')
        elif not _valid_expiry(expiry):
            errors.append(f'{name}: expiry "{expiry}" is not an ISO date or "permanent"')
        elif expiry == PERMANENT and not str(row.get('reason') or '').strip():
            errors.append(f'{name}: a permanent flag needs a reason')
        elif expiry != PERMANENT and date.fromisoformat(expiry) < today:
            notes.append(f'{name}: expired on {expiry}; remove the flag or move the date with a reason')
        if 'default' not in row or not isinstance(row.get('default'), str):
            errors.append(f'{name}: no default (write "{NO_DEFAULT}" when the code has none)')
    for name in sorted(reads):
        if name in not_flags:
            continue
        if name not in seen:
            files = ', '.join(sorted(reads[name]['files'])[:3])
            errors.append(f'{name}: read in code ({files}) but missing from the register')
            continue
        declared = code_default(reads[name])
        registered = seen[name].get('default')
        if declared not in (None, VARIES) and isinstance(registered, str) and registered != declared:
            errors.append(f'{name}: register default "{registered}" but the code declares "{declared}"')
    for name in sorted(set(seen) - set(reads)):
        errors.append(f'{name}: in the register but no longer read in code; delete the row')
    for name in sorted(set(not_flags) - set(reads)):
        errors.append(f'{name}: listed under not_flags but no longer appears in code; delete the entry')
    return errors, notes


def write_missing(register, reads):
    """Append rows for unregistered names with an empty owner and expiry for the author to fill."""
    known = {row.get('name') for row in register.get('flags') or []} | {
        item.get('name') for item in register.get('not_flags') or []}
    added = []
    for name in sorted(set(reads) - known):
        declared = code_default(reads[name])
        register.setdefault('flags', []).append({
            'name': name, 'default': declared if declared is not None else NO_DEFAULT,
            'owner': '', 'expiry': '', 'reason': ''})
        added.append(name)
    register['flags'].sort(key=lambda row: row.get('name') or '')
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--register', type=Path)
    parser.add_argument('--today', help='ISO date override (tests)')
    parser.add_argument('--write', action='store_true',
                        help='Add unregistered names with an empty owner and expiry, then check')
    args = parser.parse_args(argv)
    register_path = args.register or (args.root / REGISTER)
    try:
        register = json.loads(register_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'Flag register {register_path} could not be read: {exc}', file=sys.stderr)
        return 1
    scan = register.get('scan')
    if not isinstance(scan, dict) or not scan.get('pattern'):
        print('Flag register has no scan.pattern', file=sys.stderr)
        return 1
    inert = []
    reads = scan_reads(args.root, scan, inert)
    if args.write:
        added = write_missing(register, reads)
        register_path.write_text(json.dumps(register, indent=2) + '\n', encoding='utf-8')
        print(f'Added {len(added)} row(s); fill in owner and expiry before committing.')
    today = date.fromisoformat(args.today) if args.today else None
    errors, notes = check(register, reads, today=today)
    for note in notes + inert:
        print(f'::warning title=Flag register::{note}')
    print(f'Flag register: {len(register.get("flags") or [])} flags, {len(reads)} flag-shaped names read in code, '
          f'{len(errors)} problem(s), {len(notes)} expired.')
    if errors:
        print('Fix the register (' + register_path.name + '):')
        for error in errors:
            print('  - ' + error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

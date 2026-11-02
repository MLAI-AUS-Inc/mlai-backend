#!/usr/bin/env python3
"""Read-only AST witnesses; this is NOT an authority or completeness proof.

Explicit target-model chains and run-named save receivers are inventoried.
Dynamic aliases, queryset aliases and upstream helper guards need manual review.
Stable identity is qualified function + AST statement SHA + duplicate ordinal.
"""
import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path

def inventory(root):
 root = Path(root).resolve()
 fields = {'status', 'current_step', 'approval_state', 'run_request', 'error', 'resume_available',
           'created_at', 'updated_at', 'last_event_emitted_at', 'reconciled_at'}
 models = {'ContentFactoryRun', 'ContentFactoryRunStep', 'ContentFactoryRunStepAttempt'}
 results = []
 for base in ('content_factory', 'integrations'):
  for path in sorted((root / base).rglob('*.py')):
   if any(p in {'migrations', 'tests', 'test_fixtures', '__pycache__'} for p in path.parts) or path.name.startswith('test'):
    continue
   try:
    source = path.read_text(); source_lines = source.splitlines(); tree = ast.parse(source)
   except (OSError, SyntaxError, UnicodeError):
    continue
   parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
   aliases = set(models)
   for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom):
     aliases.update(a.asname or a.name for a in node.names if a.name in models)
   def enclosing(node):
    scopes = []
    nearest = None
    while node in parents:
     node = parents[node]
     if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
      scopes.append(node.name)
      if nearest is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
       nearest = node
    return '.'.join(reversed(scopes)) or '<module>', nearest
   # Flow-insensitive bounded alias propagation deliberately over-approximates.
   # Manual review resolves model/data aliases; this is witness generation only.
   inferred = {}
   def scope_node(node):
    while node in parents:
     node = parents[node]
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): return node
    return tree
   def mentions_model(expr, known):
    return any(isinstance(x, ast.Name) and x.id in aliases | known
               or isinstance(x, ast.Attribute) and x.attr in models for x in ast.walk(expr))
   scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))]
   for owner in scopes:
    known = set()
    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
     for arg in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs):
      if arg.arg in {"run", "runs"} or arg.arg.endswith(("_run", "_runs")):
       known.add(arg.arg)
      if arg.annotation is not None and mentions_model(arg.annotation, set()): known.add(arg.arg)
    owned_nodes = [n for n in ast.walk(owner) if scope_node(n) is owner]
    for _ in range(12):
     before = len(known)
     for item in owned_nodes:
      value = item.value if isinstance(item, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) else item.iter if isinstance(item, (ast.For, ast.comprehension)) else None
      if value is None or not mentions_model(value, known): continue
      targets = item.targets if isinstance(item, ast.Assign) else [item.target]
      for target in targets:
       if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr in {'get_or_create', 'update_or_create'}:
        targets_names = [target.elts[0]] if target.elts else []
       else: targets_names = list(ast.walk(target))
       known.update(x.id for x in targets_names if isinstance(x, ast.Name))
     if len(known) == before: break
    inferred[owner] = known
   occurrences = Counter()
   for node in ast.walk(tree):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute): continue
    method = node.func.attr
    chain = list(ast.walk(node.func.value))
    owner_scope = scope_node(node)
    known = inferred.get(owner_scope, set())
    direct_root = any(isinstance(x, ast.Name) and x.id in aliases or isinstance(x, ast.Attribute) and x.attr in models for x in chain)
    rooted = direct_root or any(isinstance(x, ast.Name) and x.id in known for x in chain)
    written = [k.arg for k in node.keywords if k.arg in fields]
    kind = None
    if rooted and method in {'update', 'bulk_update', 'bulk_create', 'create', 'update_or_create', 'get_or_create', 'delete'}:
     if method != 'update' or written: kind = 'model_' + method
    elif method == 'save' and isinstance(node.func.value, ast.Name) and ('run' in node.func.value.id or node.func.value.id in known):
     written = [c.value for kw in node.keywords if kw.arg == 'update_fields' for c in ast.walk(kw.value)
                if isinstance(c, ast.Constant) and c.value in fields]
     if written or node.func.value.id in known and not any(k.arg == 'update_fields' for k in node.keywords): kind = 'inferred_run_save' if node.func.value.id in known else 'run_named_save'
    if kind is None: continue
    function, owner = enclosing(node)
    owner_text = "\n".join(source_lines[owner.lineno - 1:owner.end_lineno]) if owner else source
    statement = ast.dump(node, annotate_fields=True, include_attributes=False)
    digest = hashlib.sha256(statement.encode()).hexdigest()
    key = (function, digest); occurrences[key] += 1
    relative = path.relative_to(root).as_posix()
    results.append({'identity': f'{relative}:{function}:{digest}:{occurrences[key]}',
        'path': relative, 'function': function, 'line': node.lineno, 'kind': kind,
        'fields': written, 'statement_sha256': digest, 'duplicate_ordinal': occurrences[key],
        'direct_model_chain': direct_root, 'inferred_receivers': sorted({x.id for x in chain if isinstance(x, ast.Name) and x.id in known}),
        'local_guard_mentioned': any(s in owner_text for s in ('authoritative_run', 'event_owned', 'owner_receipt',
                                   'current_execution_ticket', 'owns_backend_facts')),
        'function_sha256': hashlib.sha256(ast.dump(owner, include_attributes=False).encode()).hexdigest() if owner else hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest(),
        'classification': 'needs_manual_domain_and_guard_review', 'test_refs': []})
 payload = {'schema_version': 3, 'scope': __doc__, 'count': len(results), 'sites': results}
 return payload


def main():
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument("--root", type=Path, default=Path.cwd())
 parser.add_argument("--output", type=Path)
 args = parser.parse_args()
 raw = json.dumps(inventory(args.root), indent=2) + "\n"
 if args.output:
  args.output.write_text(raw)
 else:
  print(raw, end="")


if __name__ == "__main__":
 main()

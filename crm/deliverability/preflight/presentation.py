"""Keep internal service diagnostics out of staff-facing check results."""

import copy


def result_dto(result, *, owner=False):
    value = copy.deepcopy(result or {})
    if not owner:
        for row in value.get('items') or []:
            row.pop('owner_detail', None)
        seed = value.get('seed_test') or {}
        seed['results'] = [{key: row.get(key) for key in ('provider', 'folder')}
            for row in seed.get('results') or []]
    return value

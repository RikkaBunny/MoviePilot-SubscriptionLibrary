"""Keep subscription membership while suppressing re-download of capacity-evicted media."""
from __future__ import annotations

import json
from pathlib import Path

from .filesystem import PathPolicy
from .models import Scope, Subscription


def scope_from_key(key: str) -> Scope:
    return Scope(**json.loads(key))


def capture_evictions(previous: list[dict], current: list[dict], subscriptions: list[Subscription],
                      fs: PathPolicy, archived: dict) -> dict:
    result = {key: (None if eps is None else list(eps)) for key, eps in archived.items()}
    alive = {(item['scope'], tuple(item['episodes'] or [])) for item in current}
    members = {}
    for sub in subscriptions:
        members.setdefault(sub.scope.key, []).append(sub)
    for item in previous:
        key, episodes = item['scope'], item['episodes']
        if key not in members or (key, tuple(episodes or [])) in alive:
            continue
        if not fs.allowed(item['path']):
            continue
        if Path(item['path']).exists() or Path(item['path']).is_symlink():
            continue
        values = frozenset(episodes) if episodes is not None else None
        if not any(sub.accepts(values) for sub in members[key]):
            continue
        if episodes is None or scope_from_key(key).kind == 'movie':
            result[key] = None
        elif key not in result:
            result[key] = sorted(set(episodes))
        elif result[key] is not None:
            result[key] = sorted(set(result[key]) | set(episodes))
    return result


def blocked(scope: Scope, episodes: set[int] | frozenset[int] | None, archived: dict) -> bool:
    if scope.key not in archived:
        return False
    removed = archived[scope.key]
    # Unknown/all-season resources could fetch evicted episodes again; keep them blocked.
    return removed is None or episodes is None or bool(set(episodes) & set(removed))

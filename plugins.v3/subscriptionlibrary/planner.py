"""Build a preview from current subscriptions and stable host history snapshots."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .filesystem import PathPolicy
from .models import Claim, Inventory, Plan, Scope, VIDEO, numbers


def record_claims(row: dict) -> list[Claim]:
    if any(row.get(field) not in (None, '', 'local') for field in ('src_storage', 'dest_storage')):
        raise ValueError('Only local storage is supported')
    kind = row.get('type')
    seasons = numbers(row.get('seasons'), 'S')
    if kind in ('movie', '电影'):
        seasons = frozenset({0})
    return [Claim(Scope.from_dict(row, season), numbers(row.get('episodes'), 'E') or None)
            for season in seasons]


def record_paths(row: dict) -> list[str]:
    """Only source/destination file paths with an explicit history relationship."""
    paths = []
    for field in ('src', 'dest'):
        if field == 'dest' and not row.get('status'):
            continue
        value = row.get(field)
        if value and row.get(field + '_storage') in (None, '', 'local'):
            paths.append(str(value))
    return paths


def artifacts(inventory: Inventory, fs: PathPolicy) -> list[dict]:
    """Persist organized target facts before a capacity cleaner removes its histories."""
    result = []
    for row in inventory.transfers:
        if not row.get('status') or not row.get('dest'):
            continue
        try:
            claims = record_claims(row)
            path = str(row['dest'])
            if Path(path).suffix.lower() not in VIDEO or not fs.stamp(path):
                continue
            for claim in claims:
                result.append({'path': path, 'scope': claim.scope.key,
                               'episodes': sorted(claim.episodes) if claim.episodes is not None else None})
        except (ValueError, OSError, TypeError):
            continue
    return result


class Planner:
    def __init__(self, fs: PathPolicy):
        self.fs = fs

    def build(self, inventory: Inventory, cancelled: set[Scope]) -> Plan:
        if inventory.errors:
            raise RuntimeError('; '.join(inventory.errors))
        subscriptions = defaultdict(list)
        for sub in inventory.subscriptions:
            subscriptions[sub.scope].append(sub)
        scopes = set(subscriptions) | cancelled
        plan = Plan(inventory.fingerprint, sorted(s.key for s in scopes))
        path_claims = defaultdict(set)
        protected_paths = set()
        row_claims = {}
        by_torrent = defaultdict(list)

        def warning(row, message):
            plan.warnings.append(message)
            for scope in cancelled:
                # Missing season/storage can be repaired later; keep the matching intent queued.
                if (not row.get('media_id') or
                    (str(row.get('media_id')) == scope.media_id and row.get('media_source') == scope.source)):
                    if scope.key not in plan.unresolved_scopes:
                        plan.unresolved_scopes.append(scope.key)

        def keep(claim):
            members = subscriptions.get(claim.scope, [])
            if members:
                return any(sub.accepts(claim.episodes) for sub in members)
            # No blanket purge of unregistered media or automatically ended subscriptions.
            return claim.scope not in cancelled

        for row in inventory.transfers:
            try:
                claims = record_claims(row)
                if not claims:
                    raise ValueError('No season')
                row_claims[int(row['id'])] = claims
                for path in record_paths(row):
                    path_claims[path].update(claims)
            except (ValueError, TypeError, KeyError):
                protected_paths.update(record_paths(row))
                warning(row, f"整理记录 {row.get('id')} 身份、季号或本地存储关系不明确，保留")

        owners = {}
        for row in inventory.downloads:
            key = str(row.get('downloader') or '') + ':' + str(row.get('download_hash') or '')
            by_torrent[key].append(row)
            try:
                claims = record_claims(row)
                if not claims:
                    raise ValueError('No season')
                owners.setdefault(key, []).extend(claims)
            except (ValueError, TypeError):
                owners.setdefault(key, []).append(None)
                warning(row, f"下载记录 {row.get('id')} 身份或季号不明确，保留关联任务")

        file_claims = {}
        for key, torrent in inventory.torrents.items():
            entries = owners.get(key, [None])
            full_cancel = all(owner is not None and owner.scope in cancelled
                              and owner.scope not in subscriptions for owner in entries)
            for file in torrent.files:
                claims = set(path_claims.get(file.path, set()))
                if None in entries:
                    protected_paths.add(file.path)
                for owner in entries:
                    if owner is None:
                        continue
                    if owner.scope.kind == 'movie':
                        claims.add(owner)
                    elif full_cancel or file.season is None or file.season == owner.scope.season:
                        # File-level parser evidence replaces the whole-pack episode range.
                        claims.add(Claim(owner.scope, file.episodes))
                if not claims:
                    protected_paths.add(file.path)
                path_claims[file.path].update(claims)
                file_claims[key, file.index] = claims

        wanted_inodes = set()
        for path, claims in path_claims.items():
            if not claims or any(keep(claim) for claim in claims):
                protected_paths.add(path)
        for path in protected_paths:
            if self.fs.allowed(path):
                stamp = self.fs.stamp(path)
                if stamp:
                    wanted_inodes.add(stamp[:2])

        selected = set()
        for path, claims in path_claims.items():
            if path in protected_paths or not claims or any(keep(claim) for claim in claims):
                continue
            if not self.fs.allowed(path):
                raise ValueError(f'清理路径未授权：{path}')
            selected.add(path)

        # Add subtitles/NFO with an exact stem relationship. Never remove arbitrary neighbours.
        for path in list(selected):
            if Path(path).suffix.lower() in VIDEO:
                selected.update(self.fs.sidecars(path))
        for path in sorted(selected):
            if path in protected_paths:
                continue
            stamp = self.fs.stamp(path)
            if stamp and stamp[:2] not in wanted_inodes:
                plan.files[path] = stamp

        # All hard links in the managed namespace must go to release disk space.
        plan.files.update(self.fs.aliases({s[:2] for s in plan.files.values()} - wanted_inodes))
        for key, torrent in inventory.torrents.items():
            entries = owners.get(key, [None])
            full_cancel = all(owner is not None and owner.scope in cancelled
                              and owner.scope not in subscriptions for owner in entries)
            videos = [f for f in torrent.files if Path(f.path).suffix.lower() in VIDEO]
            unwanted = [f for f in torrent.files if file_claims.get((key, f.index))
                        and all(not keep(c) for c in file_claims[key, f.index])
                        and f.path not in protected_paths]
            safe_files = True
            for file in torrent.files:
                if not self.fs.allowed(file.path):
                    safe_files = False
                    break
                stamp = self.fs.stamp(file.path)
                if file.path in protected_paths or (stamp and stamp[:2] in wanted_inodes):
                    safe_files = False
                    break
            all_videos_selected = bool(videos) and all(f in unwanted for f in videos)
            if (full_cancel or all_videos_selected) and safe_files and torrent.files:
                plan.drops.append(key)
                for file in torrent.files:
                    stamp = self.fs.stamp(file.path)
                    if stamp:
                        plan.files[file.path] = stamp
            else:
                ids = [f.index for f in unwanted if f.priority != 0]
                if ids:
                    if torrent.provider != 'qbittorrent':
                        raise RuntimeError('This downloader has no selective-file adapter')
                    plan.priorities[key] = ids

        # Partial files from qB share the same proven torrent-file relationship.
        for path in list(selected):
            partial = path + '.!qB'
            if partial in protected_paths or not self.fs.allowed(partial):
                continue
            stamp = self.fs.stamp(partial)
            if stamp and stamp[:2] not in wanted_inodes:
                plan.files[partial] = stamp

        for row in inventory.transfers:
            claims = row_claims.get(int(row['id']), [])
            if claims and all(not keep(c) for c in claims):
                paths = record_paths(row)
                if all(p not in protected_paths and self.fs.allowed(p)
                       and (self.fs.stamp(p) is None or p in plan.files) for p in paths):
                    plan.transfer_ids.append(int(row['id']))
        for key, rows in by_torrent.items():
            entries = owners.get(key, [None])
            if entries and all(c is not None and not keep(c) for c in entries):
                if key in plan.drops or key not in inventory.torrents:
                    plan.download_ids.extend(int(row['id']) for row in rows)
        plan.drops.sort()
        plan.transfer_ids.sort()
        plan.download_ids.sort()
        for row in inventory.transfers:
            claims = row_claims.get(int(row['id']), [])
            if (row.get('status') and row.get('dest') and claims
                    and all(c.scope in cancelled and c.scope not in subscriptions for c in claims)):
                for claim in claims:
                    plan.files.update(self.fs.cancel_metadata(claim.scope, str(row['dest']),
                                                             set(plan.files), set(subscriptions)))
        plan.files.update(self.fs.cancel_empty_metadata(cancelled - set(subscriptions),
                                                       set(plan.files), set(subscriptions)))
        source_paths = {f.path for torrent in inventory.torrents.values() for f in torrent.files}
        source_paths.update(p + '.!qB' for p in list(source_paths))
        plan.mutable_sources = sorted(source_paths & set(plan.files))
        return plan

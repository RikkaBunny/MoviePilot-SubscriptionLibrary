"""Retention rules. This module has no MoviePilot or network dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any

VIDEO = frozenset({'.mkv', '.mp4', '.avi', '.m2ts', '.ts', '.mov', '.webm', '.wmv', '.mpg', '.mpeg'})
SIDECAR = frozenset({'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.nfo', '.jpg', '.jpeg', '.png'})


def plain(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if hasattr(value, 'to_dict'):
        return dict(value.to_dict())
    raise ValueError('Unsupported host snapshot')


def scalar(value: Any) -> str:
    return str(getattr(value, 'value', value) or '').strip()


def numbers(value: Any, prefix: str) -> frozenset[int]:
    """Parse a whole canonical S/E field; never guess from arbitrary titles."""
    text = scalar(value).upper().replace(' ', '')
    if not text:
        return frozenset()
    if not re.fullmatch(rf'{prefix}?\d+(?:-{prefix}?\d+)?(?:[,+]{prefix}?\d+(?:-{prefix}?\d+)?)*', text):
        return frozenset()
    result = set()
    for part in re.split(r'[,+]', text):
        bounds = [int(x) for x in re.findall(r'\d+', part)]
        lo, hi = bounds[0], bounds[-1]
        if lo < 0 or hi < lo or hi - lo > 10000:
            return frozenset()
        result.update(range(lo, hi + 1))
    return frozenset(result)


@dataclass(frozen=True, order=True)
class Scope:
    source: str
    media_id: str
    kind: str
    season: int = 0
    episode_group: str = ''

    @property
    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    @classmethod
    def from_dict(cls, row: dict, season: int | None = None) -> Scope:
        source, media_id = scalar(row.get('media_source')), scalar(row.get('media_id'))
        kind = {'电影': 'movie', '电视剧': 'tv', 'movie': 'movie', 'tv': 'tv'}.get(scalar(row.get('type')))
        if not source or not media_id or media_id == '0' or not kind:
            raise ValueError('Missing stable movie/TV identity')
        if kind == 'movie':
            season = 0
        elif season is None:
            raw = row.get('season')
            seasons = numbers(row.get('seasons'), 'S')
            if raw is not None:
                season = int(raw)
            elif len(seasons) == 1:
                season = next(iter(seasons))
            else:
                raise ValueError('Ambiguous season')
        if season is None or season < 0:
            raise ValueError('Invalid season')
        return cls(source, media_id, kind, season, scalar(row.get('episode_group')))


@dataclass(frozen=True)
class Subscription:
    id: int
    scope: Scope
    start: int = 1
    end: int | None = None
    name: str = ''

    @classmethod
    def from_dict(cls, row: dict) -> Subscription:
        scope = Scope.from_dict(row)
        start = max(1, int(row.get('start_episode') or 1))
        # A changing metadata total is not an explicit instruction to delete files.
        end = int(row.get('total_episode') or 0) if row.get('manual_total_episode') else None
        if end is not None and (end < start or end <= 0):
            raise ValueError('Invalid manual episode range')
        return cls(int(row['id']), scope, start, end, scalar(row.get('name')))

    def accepts(self, episodes: frozenset[int] | None) -> bool:
        if self.scope.kind == 'movie' or episodes is None:
            return True
        return any(e >= self.start and (self.end is None or e <= self.end) for e in episodes)


@dataclass(frozen=True)
class Claim:
    scope: Scope
    episodes: frozenset[int] | None = None


@dataclass(frozen=True)
class TorrentFile:
    index: int
    path: str
    season: int | None = None
    episodes: frozenset[int] | None = None
    priority: int | None = None


@dataclass(frozen=True)
class Torrent:
    downloader: str
    hash: str
    files: tuple[TorrentFile, ...]
    provider: str = 'qbittorrent'

    @property
    def key(self) -> str:
        return self.downloader + ':' + self.hash


@dataclass
class Inventory:
    subscriptions: list[Subscription]
    downloads: list[dict]
    transfers: list[dict]
    torrents: dict[str, Torrent] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        items = [(s.id, s.scope.key, s.start, s.end) for s in self.subscriptions]
        return hashlib.sha256(json.dumps(sorted(items), ensure_ascii=False).encode()).hexdigest()


@dataclass
class Plan:
    fingerprint: str
    scopes: list[str]
    files: dict[str, tuple[int, int, int, int, int]] = field(default_factory=dict)
    drops: list[str] = field(default_factory=list)
    priorities: dict[str, list[int]] = field(default_factory=dict)
    transfer_ids: list[int] = field(default_factory=list)
    download_ids: list[int] = field(default_factory=list)
    mutable_sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved_scopes: list[str] = field(default_factory=list)

    @property
    def has_actions(self) -> bool:
        return bool(self.files or self.drops or self.priorities or self.transfer_ids or self.download_ids)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Plan:
        result = cls(**value)
        result.files = {p: tuple(s) for p, s in result.files.items()}
        return result

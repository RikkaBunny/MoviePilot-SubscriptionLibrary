"""MoviePilot v3 boundary: SDK queries/services and authenticated public history APIs.

All host-version-specific calls stay here. There are no SQL queries or database writes.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import time

import requests
from app.sdk import queries
from app.sdk.config import settings
from app.sdk.media import MetaInfo, resolve_media_identity
from app.sdk.services import DownloaderHelper, MediaServerHelper

from .models import Inventory, Scope, Subscription, Torrent, TorrentFile, plain, scalar


class MPHost:
    def __init__(self, plugin):
        self.plugin = plugin
        self.downloaders = DownloaderHelper()

    @staticmethod
    def _list(query) -> list[dict]:
        results, page = [], 1
        while True:
            data = query(page={'page': page, 'count': 200})
            items = [plain(item) for item in data.items]
            if not items and data.has_next:
                raise RuntimeError('Host returned an incomplete query page')
            results.extend(items)
            if not data.has_next:
                return results
            page += 1

    @staticmethod
    def normalize(row: dict) -> dict:
        row = dict(row)
        source, media_id = resolve_media_identity(media=row)
        row['media_source'], row['media_id'] = scalar(source), scalar(media_id)
        return row

    def subscriptions(self) -> list[Subscription]:
        values = []
        for row in self._list(queries.list_subscriptions):
            # Music subscriptions are outside this plugin's deletion scope.
            if scalar(row.get('type')) not in ('电影', '电视剧', 'movie', 'tv'):
                continue
            values.append(Subscription.from_dict(self.normalize(row)))
        return values

    def inventory(self, include_torrents: bool = True) -> Inventory:
        inventory = Inventory(self.subscriptions(),
                              [self.normalize(r) for r in self._list(queries.list_download_history)],
                              [self.normalize(r) for r in self._list(queries.list_transfer_history)])
        if not include_torrents:
            return inventory
        groups = defaultdict(set)
        for row in inventory.downloads + inventory.transfers:
            if row.get('downloader') and row.get('download_hash'):
                groups[str(row['downloader'])].add(str(row['download_hash']))
        for name, hashes in groups.items():
            service = self.downloaders.get_service(name)
            if not service or service.type != 'qbittorrent':
                inventory.errors.append(f'下载器 {name} 不可用或没有兼容适配器')
                continue
            # Validate the native provider response before treating absent tasks as removed.
            raw, error = service.instance.get_torrents(ids=list(hashes))
            if error or raw is None:
                inventory.errors.append(f'下载器 {name} 查询失败，未生成删除计划')
                continue
            try:
                tasks = self.plugin.chain.list_torrents(hashs=list(hashes), downloader=name, include_all_tags=True)
                if tasks is None:
                    raise RuntimeError('Downloader projection unavailable')
                raw_by_hash = {str(item.get('hash')): item for item in raw}
                for task in tasks:
                    files = self.plugin.chain.torrent_files(str(task.hash), downloader=name)
                    if not files:
                        raise RuntimeError('Torrent file manifest unavailable')
                    rows = []
                    bases = [Path(task.save_path)] if task.save_path else []
                    incomplete = raw_by_hash.get(str(task.hash), {}).get('download_path')
                    if incomplete:
                        # The host provider owns path mapping; no private credentials are copied.
                        mapped = service.module.normalize_return_path(Path(incomplete), name)
                        bases.append(Path(mapped))
                    if not bases or any(not p.is_absolute() for p in bases):
                        raise RuntimeError('Torrent save-path projection unavailable')
                    for file in files:
                        if Path(file.name).is_absolute() or '..' in Path(file.name).parts:
                            raise RuntimeError('Unsafe torrent manifest path')
                        candidates = [base / file.name for base in bases]
                        path = next((p for p in candidates if p.exists() or Path(str(p) + '.!qB').exists()), candidates[0])
                        meta = MetaInfo(Path(file.name).name)
                        eps = frozenset(meta.episode_list or []) or None
                        # Default anime season=1 is not proof that a filename explicitly names a season.
                        match = re.search(r'(?i)S(\d+)(?:E|\b)', file.name)
                        season = int(match[1]) if match else None
                        rows.append(TorrentFile(int(file.id), str(path), season, eps, file.priority))
                    torrent = Torrent(name, str(task.hash), tuple(rows), service.type)
                    inventory.torrents[torrent.key] = torrent
            except Exception:
                # Do not put provider exceptions/connection strings into a public-facing report.
                inventory.errors.append(f'下载器 {name} 文件清单读取失败，清理已延后')
        return inventory

    def set_priorities(self, key: str, ids: list[int]) -> None:
        name, hash_value = key.split(':', 1)
        service = self.downloaders.get_service(name, type_filter='qbittorrent')
        if not service:
            raise RuntimeError('Downloader unavailable')
        rows, error = service.instance.get_torrents(ids=[hash_value])
        if error or rows is None:
            raise RuntimeError('Downloader unavailable')
        if not rows:
            return
        if not service.instance.set_files(torrent_hash=hash_value, file_ids=ids, priority=0):
            raise RuntimeError('Selective download update failed')
        manifest = self.plugin.chain.torrent_files(hash_value, downloader=name)
        actual = {int(f.id): f.priority for f in manifest or []}
        if any(actual.get(index) != 0 for index in ids):
            raise RuntimeError('Selective download update was not confirmed')

    def task_running(self, key: str) -> bool:
        name, hash_value = key.split(':', 1)
        service = self.downloaders.get_service(name, type_filter='qbittorrent')
        if not service:
            raise RuntimeError('Downloader unavailable')
        rows, error = service.instance.get_torrents(ids=[hash_value])
        if error or rows is None:
            raise RuntimeError('Downloader unavailable')
        return bool(rows and scalar(rows[0].get('state')).lower() not in
                    {'stoppeddl', 'stoppedup', 'pauseddl', 'pausedup'})

    def pause(self, key: str) -> None:
        name, hash_value = key.split(':', 1)
        if not self.task_running(key):
            return
        if not self.plugin.chain.stop_torrents(hashs=[hash_value], downloader=name):
            raise RuntimeError('Torrent pause failed')
        for _ in range(10):
            if not self.task_running(key):
                return
            time.sleep(0.2)
        raise RuntimeError('Torrent pause not confirmed')

    def resume(self, key: str) -> None:
        name, hash_value = key.split(':', 1)
        service = self.downloaders.get_service(name, type_filter='qbittorrent')
        if not service:
            raise RuntimeError('Downloader unavailable while restoring pause state')
        rows, error = service.instance.get_torrents(ids=[hash_value])
        if error or rows is None:
            raise RuntimeError('Downloader unavailable while restoring pause state')
        if not rows:
            return
        if not self.plugin.chain.start_torrents(hashs=[hash_value], downloader=name):
            raise RuntimeError('Retained torrent could not be resumed')

    def drop(self, key: str) -> None:
        name, hash_value = key.split(':', 1)
        service = self.downloaders.get_service(name, type_filter='qbittorrent')
        if not service:
            raise RuntimeError('Downloader unavailable')
        rows, error = service.instance.get_torrents(ids=[hash_value])
        if error or rows is None:
            raise RuntimeError('Downloader state unavailable')
        if not rows:
            return
        if not self.plugin.chain.remove_torrents(hashs=[hash_value], delete_file=True, downloader=name):
            raise RuntimeError('Torrent removal failed')
        rows, error = service.instance.get_torrents(ids=[hash_value])
        if error or rows is None or rows:
            raise RuntimeError('Torrent removal not yet confirmed; retry deferred')

    @staticmethod
    def _delete_history(kind: str, record_id: int) -> None:
        # Use the local HTTP API: the SDK query boundary deliberately exposes no host writes.
        port = int(settings.PORT)
        base = f'http://127.0.0.1:{port}{settings.API_V1_STR}'
        route = '/history/' + kind
        if kind == 'transfer':
            route += '?deletesrc=false&deletedest=false'
        result = requests.delete(base + route, json={'id': record_id},
                                 headers={'X-API-KEY': str(settings.API_TOKEN)}, timeout=30,
                                 allow_redirects=False)
        result.raise_for_status()
        payload = result.json()
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise RuntimeError('Host history deletion was rejected')
        query = queries.get_transfer_history if kind == 'transfer' else queries.get_download_history
        if query(record_id) is not None:
            raise RuntimeError('Host history record was retained')

    def delete_transfer(self, record_id: int) -> None:
        if queries.get_transfer_history(record_id) is not None:
            self._delete_history('transfer', record_id)

    def delete_download(self, record_id: int) -> None:
        if queries.get_download_history(record_id) is not None:
            self._delete_history('download', record_id)

    @staticmethod
    def refresh() -> None:
        for service in MediaServerHelper().get_services().values():
            refresh = getattr(service.instance, 'refresh_root_library', None)
            if callable(refresh) and refresh() is not True:
                raise RuntimeError('Media-server refresh not acknowledged')

    @staticmethod
    def context_claim(context, subscription: Subscription | None = None):
        info = plain(context.mediainfo) if not isinstance(context, dict) else context.get('mediainfo') or {}
        meta = context.meta_info if not isinstance(context, dict) else context.get('meta_info') or {}
        source, media_id = resolve_media_identity(media=info)
        info = {**info, 'media_source': scalar(source), 'media_id': scalar(media_id)}
        seasons = getattr(meta, 'season_list', None) if not isinstance(meta, dict) else meta.get('season_list')
        episodes = getattr(meta, 'episode_list', None) if not isinstance(meta, dict) else meta.get('episode_list')
        if subscription:
            # The subscription is the authority for episode-group mapping and target season.
            candidate = Scope.from_dict(info, subscription.scope.season)
            if (candidate.source, candidate.media_id, candidate.kind) != (
                    subscription.scope.source, subscription.scope.media_id, subscription.scope.kind):
                return None, None
            scope = subscription.scope
        else:
            if info.get('type') in ('电视剧', 'tv') and (not seasons or len(seasons) != 1):
                return None, None
            scope = Scope.from_dict(info, next(iter(seasons)) if seasons else 0)
        return scope, frozenset(episodes) if episodes else None

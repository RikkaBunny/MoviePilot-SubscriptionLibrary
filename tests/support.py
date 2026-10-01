"""Small boundary doubles; production code always imports the real MoviePilot SDK."""
from copy import deepcopy
from enum import Enum
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, **attrs):
    value = ModuleType(name)
    value.__dict__.update(attrs)
    sys.modules[name] = value
    return value


class PluginBase:
    def __init__(self):
        self.data, self.config = {}, {}

    def get_data(self, key):
        return deepcopy(self.data.get(key))

    def save_data(self, key, value):
        self.data[key] = deepcopy(value)

    def get_config(self):
        return deepcopy(self.config)

    def update_config(self, value):
        self.config = deepcopy(value)


class Meta:
    def __init__(self, name):
        self.episode_list = [int(x) for x in re.findall(r'E(\d+)', name)]


def identity(media):
    return media.get('media_source') or ('themoviedb' if media.get('tmdbid') else None), \
           media.get('media_id') or media.get('tmdbid')


class ChainType(Enum):
    SubscribeCompletionCheck = 'completion'
    ResourceSelection = 'selection'
    ResourceDownload = 'download'


class EventType(Enum):
    SubscribeDeleted = 'deleted'
    SubscribeAdded = 'added'
    SubscribeModified = 'modified'
    TransferComplete = 'transferred'


for name in ('app', 'app.sdk', 'app.schemas'):
    module(name)
sdk_queries = module('app.sdk.queries')
for name in ('list_subscriptions', 'list_download_history', 'list_transfer_history',
             'get_download_history', 'get_transfer_history'):
    setattr(sdk_queries, name, Mock())
module('app.sdk.config', settings=SimpleNamespace(PORT=3001, API_V1_STR='/api/v1', API_TOKEN='test-placeholder'))
module('app.sdk.media', MetaInfo=Meta, resolve_media_identity=identity)
module('app.sdk.services', DownloaderHelper=Mock, MediaServerHelper=Mock)
module('app.sdk.events', Event=SimpleNamespace,
       eventmanager=SimpleNamespace(register=lambda *args, **kwargs: lambda fn: fn))
module('app.sdk.logging', logger=Mock())
module('app.sdk.plugin', _PluginBase=PluginBase)
module('app.sdk.scheduler', start_scheduler_job=Mock())
module('app.schemas.types', ChainEventType=ChainType, EventType=EventType)
http = module('requests', delete=Mock())
spec = importlib.util.spec_from_file_location('subscriptionlibrary', ROOT / 'plugins.v3/subscriptionlibrary/__init__.py',
                                            submodule_search_locations=[str(ROOT / 'plugins.v3/subscriptionlibrary')])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from subscriptionlibrary.models import Inventory, Scope, Subscription

SCOPE = Scope('themoviedb', '100', 'tv', 1)


def record(record_id=1, scope=SCOPE, episodes='E01', **values):
    return {'id': record_id, 'media_source': scope.source, 'media_id': scope.media_id,
            'type': '电视剧' if scope.kind == 'tv' else '电影', 'seasons': f'S{scope.season:02d}',
            'episodes': episodes, 'episode_group': scope.episode_group, **values}


def event(data, kind=None):
    return SimpleNamespace(event_data=data, event_type=kind)


class FakeHost:
    def __init__(self, inventory):
        self.inv = inventory
        self.calls = []
        self.running = {key: True for key in inventory.torrents}
        self.on_pause = None
        self.fail_drop = False
        self.fail_transfer = False
        self.inventory_calls = 0

    def inventory(self, include_torrents=True):
        self.inventory_calls += 1
        return deepcopy(self.inv)

    def subscriptions(self):
        return list(self.inv.subscriptions)

    def task_running(self, key):
        return self.running.get(key, False)

    def pause(self, key):
        self.calls.append(('pause', key))
        self.running[key] = False
        if self.on_pause:
            self.on_pause()

    def resume(self, key):
        self.calls.append(('resume', key))
        if key in self.inv.torrents:
            self.running[key] = True

    def set_priorities(self, key, ids):
        self.calls.append(('priority', key, ids))

    def drop(self, key):
        self.calls.append(('drop', key))
        if self.fail_drop:
            raise RuntimeError('Downloader unavailable')
        self.inv.torrents.pop(key, None)

    def delete_transfer(self, record_id):
        self.calls.append(('transfer', record_id))
        if self.fail_transfer:
            raise RuntimeError('History unavailable')
        self.inv.transfers = [r for r in self.inv.transfers if r['id'] != record_id]

    def delete_download(self, record_id):
        self.calls.append(('download', record_id))
        self.inv.downloads = [r for r in self.inv.downloads if r['id'] != record_id]

    def refresh(self):
        self.calls.append(('refresh',))

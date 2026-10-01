"""Isolated QA with real files and persisted state, without touching a live MP database.

Downloader/history operations use fixture adapters. This does not certify those live APIs.
Can also exercise the installed pure rule/executor modules inside a MoviePilot container.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
from types import ModuleType
import sys


def modules(directory):
    name = 'subscriptionlibrary_qa'
    package = ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    return tuple(importlib.import_module(name + '.' + part)
                 for part in ('models', 'filesystem', 'controller', 'archive'))


def storage(path):
    def load():
        return json.loads(path.read_text()) if path.exists() else None

    def save(value):
        temporary = path.with_suffix('.new')
        temporary.write_text(json.dumps(value))
        os.replace(temporary, path)

    return load, save


class FixtureHost:
    def __init__(self, inventory):
        self.value = inventory
        self.calls = []
        self.fail_history = False

    def inventory(self, include_torrents=True):
        return deepcopy(self.value)

    def subscriptions(self):
        return list(self.value.subscriptions)

    def task_running(self, key):
        return key in self.value.torrents

    def pause(self, key):
        self.calls.append('pause')

    def resume(self, key):
        self.calls.append('resume')

    def set_priorities(self, key, ids):
        self.calls.append('priorities')

    def drop(self, key):
        self.calls.append('drop')
        self.value.torrents.pop(key, None)

    def delete_transfer(self, record_id):
        if self.fail_history:
            raise RuntimeError('Fixture history API failure')
        self.calls.append('transfer')
        self.value.transfers = [r for r in self.value.transfers if r['id'] != record_id]

    def delete_download(self, record_id):
        self.calls.append('download')
        self.value.downloads = [r for r in self.value.downloads if r['id'] != record_id]

    def refresh(self):
        self.calls.append('refresh')


def queue_worker(directory, state_path, lock_path, member_id, ready, start):
    model, files, controller, _ = modules(directory)
    load, save = storage(state_path)

    def delayed_load():
        value = load()
        time.sleep(0.03)
        return value

    ctl = controller.Controller(None, files.PathPolicy([str(state_path.parent)]),
                                delayed_load, save, lambda: True, lock_path=lock_path)
    ready.put(member_id)
    if not start.wait(15):
        raise RuntimeError('QA worker barrier timed out')
    ctl.enqueue_cancel(model.Scope('themoviedb', str(member_id), 'tv', 1))


def run(directory, parent=None):
    model, files, controller, archive = modules(directory)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix='subscriptionlibrary-qa-', dir=parent) as temporary:
        root = Path(temporary).resolve()
        policy = files.PathPolicy([str(root)])
        state_path, lock_path = root / 'state.json', root / 'state.lock'
        load, save = storage(state_path)
        scope = model.Scope('themoviedb', '900000001', 'tv', 1)
        sub = model.Subscription(900000001, scope, 1, 100, 'QA fixture')
        inventory = model.Inventory([sub], [], [], {})
        host = FixtureHost(inventory)

        def ctl():
            return controller.Controller(host, policy, load, save, lambda: True, lock_path=lock_path)

        def episode(number, identifier):
            name = f'QA.S01E{number:02}.mkv'
            source, target = root / 'download' / name, root / 'library' / name
            source.parent.mkdir(exist_ok=True)
            target.parent.mkdir(exist_ok=True)
            source.write_bytes(b'generated disposable QA content')
            os.link(source, target)
            subtitle = target.with_suffix('.zh.ass')
            subtitle.write_text('generated disposable QA subtitle')
            row = {'id': identifier, 'media_source': scope.source, 'media_id': scope.media_id,
                   'type': '电视剧', 'seasons': 'S01', 'episodes': f'E{number:02}',
                   'src_storage': 'local', 'dest_storage': 'local', 'status': True,
                   'src': str(source), 'dest': str(target), 'downloader': 'fixture-qB',
                   'download_hash': 'fixture-only'}
            host.value.transfers.append(row)
            return source, target, subtitle, model.TorrentFile(identifier, str(source), 1,
                                                              frozenset({number}), 1)

        old = episode(1, 1)
        current = episode(80, 2)
        task = model.Torrent('fixture-qB', 'fixture-only', (old[3], current[3]))
        host.value.torrents[task.key] = task
        host.value.downloads.append({**host.value.transfers[0], 'episodes': 'E01-E100'})
        unrelated = root / 'library' / 'unregistered.txt'
        unrelated.write_text('keep')
        ctl().run(dry_run=False)
        check('retained subscription preserves source, hardlink and subtitle',
              all(p.exists() for p in old[:3] + current[:3]))

        host.value.subscriptions = [model.Subscription(sub.id, scope, 80, 100)]
        ctl().run(dry_run=False)
        check('range edit deletes obsolete files and keeps desired episodes and mixed task',
              not any(p.exists() for p in old[:3]) and all(p.exists() for p in current[:3])
              and task.key in host.value.torrents and host.value.downloads
              and [r['id'] for r in host.value.transfers] == [2])

        for path in current[:3]:
            path.unlink()
        host.value.transfers.clear()
        host.value.downloads.clear()
        host.value.torrents.clear()
        archived = ctl().detect_before_download()
        check('capacity eviction archives old episode and allows next episode after reload',
              archive.blocked(scope, {80}, archived) and not archive.blocked(scope, {81}, archived)
              and host.value.subscriptions)
        ctl().restore([scope.key])
        check('explicit restore survives the next absence check',
              not archive.blocked(scope, {80}, ctl().detect_before_download()))

        next_episode = episode(81, 3)
        task = model.Torrent('fixture-qB', 'fixture-only', (next_episode[3],))
        host.value.torrents[task.key] = task
        host.value.downloads.append(deepcopy(host.value.transfers[0]))
        ctl().run(dry_run=False)
        host.value.subscriptions.clear()
        ctl().enqueue_cancel(scope)
        host.fail_history = True
        try:
            ctl().run(dry_run=False)
        except RuntimeError as error:
            check('failed history removal keeps the recovery journal',
                  str(error) == 'Fixture history API failure' and bool(load().get('pending')))
        else:
            raise AssertionError('Expected fixture history failure')
        host.fail_history = False
        ctl().run(dry_run=False)
        check('cancel retry after reload finishes deletion and acknowledges intent',
              not any(p.exists() for p in next_episode[:3]) and not host.value.transfers
              and not host.value.downloads and not host.value.torrents
              and not load().get('pending') and not load()['cancelled'])
        check('unregistered neighbouring file remains untouched', unrelated.read_text() == 'keep')

        queue_dir = root / 'worker-state'
        queue_dir.mkdir()
        queue_state, queue_lock = queue_dir / 'state.json', queue_dir / 'state.lock'
        context = multiprocessing.get_context('spawn')
        ready, start = context.Queue(), context.Event()
        workers = [context.Process(target=queue_worker,
                                  args=(directory, queue_state, queue_lock, n, ready, start))
                   for n in range(1, 5)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            for worker in workers:
                worker.join(15)
            check('four independent worker processes preserve every cancellation intent',
                  all(worker.exitcode == 0 for worker in workers)
                  and len(storage(queue_state)[0]()['cancelled']) == 4)
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
        check('filesystem fixture root is the sole allowed deletion namespace',
              not policy.allowed(root.parent / 'outside.mkv'))
    check('temporary QA workspace is removed', not root.exists())
    return {'result': 'PASS', 'checks': checks,
            'boundary': 'Real temporary filesystem and state; downloader/history adapters are fixtures.'}


def native_sdk_checks():
    """Real host event models, called in an isolated process with fixture state only."""
    from types import SimpleNamespace
    from app.plugins.subscriptionlibrary import SubscriptionLibrary
    from app.plugins.subscriptionlibrary.models import Scope, Subscription
    from app.sdk.events import (Event, SubscribeCompletionCheckContractData,
                                ResourceDownloadContractData, ContextSnapshot)
    from app.schemas.types import ChainEventType
    from app.sdk.media import MetaInfo

    # Do not instantiate host persistence/service helpers or register a live plugin instance.
    plugin = object.__new__(SubscriptionLibrary)
    plugin._enabled = True
    plugin._config = {'keep_completed': True, 'archive_guard': True}
    scope = Scope('themoviedb', '900000001', 'tv', 1)
    subscription = Subscription(900000001, scope)
    plugin._controller = SimpleNamespace(
        host=SimpleNamespace(subscriptions=lambda: [subscription]),
        detect_before_download=lambda: {scope.key: [80]})
    data = SubscribeCompletionCheckContractData(subscribe={
        'id': subscription.id, 'name': 'QA', 'type': '电视剧', 'season': 1,
        'media_source': scope.source, 'media_id': scope.media_id})
    plugin.keep_subscription(Event(ChainEventType.SubscribeCompletionCheck, data))
    if not data.cancel:
        raise AssertionError('Native completion contract veto')
    checks = ['native completion contract veto']
    for episode, expected in [(80, True), (81, False)]:
        context = ContextSnapshot(
            mediainfo={'media_source': scope.source, 'media_id': scope.media_id, 'type': '电视剧'},
            meta_info={'type': '电视剧', 'begin_season': 1, 'episode_list': [episode]})
        data = ResourceDownloadContractData(context=context,
                                            origin='Subscribe|{"id":900000001}')
        plugin.suppress_evicted_download(Event(ChainEventType.ResourceDownload, data))
        if data.cancel is not expected:
            raise AssertionError(f'Native archive gate episode {episode}')
        checks.append(f'native archive gate episode {episode}')
    if MetaInfo('QA.S01E80.mkv').episode_list != [80]:
        raise AssertionError('Native filename parser')
    checks.append('native filename parser')
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'plugins.v3/subscriptionlibrary')
    parser.add_argument('--workspace-parent', type=Path)
    parser.add_argument('--native-sdk', action='store_true',
                        help='Also check real MoviePilot event models (inside the host container only)')
    args = parser.parse_args()
    report = run(args.plugin_dir.resolve(), args.workspace_parent)
    if args.native_sdk:
        report['native_sdk_checks'] = native_sdk_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))

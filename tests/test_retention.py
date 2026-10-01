from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from support import SCOPE, FakeHost, record
from subscriptionlibrary.archive import blocked, capture_evictions
from subscriptionlibrary.controller import Controller, StalePlan
from subscriptionlibrary.filesystem import PathPolicy
from subscriptionlibrary.models import Claim, Inventory, Plan, Scope, Subscription, Torrent, TorrentFile, numbers
from subscriptionlibrary.planner import Planner, artifacts


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fs = PathPolicy([str(self.root)])

    def file(self, name, content=b'video'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)

    def inventory(self, start=1, end=None, cancelled=False):
        src = self.file('download/Show.S01E01.mkv')
        dst = self.file('library/Show.S01E01.mkv')
        torrent = Torrent('qB', 'hash', (TorrentFile(0, src, 1, frozenset({1}), 1),))
        inv = Inventory([] if cancelled else [Subscription(1, SCOPE, start, end)],
                        [record(downloader='qB', download_hash='hash')],
                        [record(status=True, src=src, dest=dst, downloader='qB', download_hash='hash')],
                        {torrent.key: torrent})
        return inv, src, dst

    def controller(self, inv):
        host = FakeHost(inv)
        store = {}
        def save(value):
            store.clear()
            store.update(deepcopy(value))
        ctl = Controller(host, self.fs, lambda: deepcopy(store), save, lambda: True)
        return ctl, host, store


class RetentionTests(Files):
    def test_completed_and_paused_membership_keeps_files(self):
        inv, src, dst = self.inventory()
        plan = Planner(self.fs).build(inv, set())
        self.assertFalse(plan.has_actions)
        self.assertTrue(Path(src).exists() and Path(dst).exists())

    def test_cancel_removes_both_files_and_records(self):
        inv, src, dst = self.inventory(cancelled=True)
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertEqual(plan.drops, ['qB:hash'])
        self.assertEqual(set(plan.files), {src, dst})
        self.assertEqual(plan.download_ids, [1])
        self.assertEqual(plan.transfer_ids, [1])

    def test_unregistered_media_is_not_blanket_purged(self):
        inv, _, _ = self.inventory(cancelled=True)
        self.assertFalse(Planner(self.fs).build(inv, set()).has_actions)

    def test_episode_start_change_prunes_old_with_new_retained(self):
        inv, src, dst = self.inventory(start=80)
        src2 = self.file('download/Show.S01E80.mkv')
        dst2 = self.file('library/Show.S01E80.mkv')
        inv.transfers.append(record(2, episodes='E80', status=True, src=src2, dest=dst2))
        inv.torrents['qB:hash'] = Torrent('qB', 'hash', (
            TorrentFile(0, src, 1, frozenset({1}), 1), TorrentFile(1, src2, 1, frozenset({80}), 1)))
        inv.downloads[0]['episodes'] = 'E01-E100'
        plan = Planner(self.fs).build(inv, set())
        self.assertEqual(plan.priorities, {'qB:hash': [0]})
        self.assertEqual(plan.drops, [])
        self.assertEqual(set(plan.files), {src, dst})
        self.assertEqual(plan.transfer_ids, [1])
        self.assertEqual(plan.download_ids, [])

    def test_manual_end_is_deletion_intent_metadata_total_is_not(self):
        row = {'id': 1, 'media_source': SCOPE.source, 'media_id': SCOPE.media_id,
               'type': '电视剧', 'season': 1, 'total_episode': 26}
        automatic = Subscription.from_dict(row)
        manual = Subscription.from_dict({**row, 'manual_total_episode': True})
        self.assertTrue(automatic.accepts(frozenset({30})))
        self.assertFalse(manual.accepts(frozenset({30})))

    def test_multiple_subscriptions_union_ranges_and_reused_ids(self):
        inv, _, _ = self.inventory(start=80)
        inv.subscriptions.append(Subscription(2, SCOPE, 1, 10))
        self.assertFalse(Planner(self.fs).build(inv, {SCOPE}).has_actions)

    def test_unknown_episode_in_retained_pack_is_kept(self):
        inv, src, _ = self.inventory(start=80)
        inv.transfers[0]['episodes'] = ''
        inv.torrents['qB:hash'] = Torrent('qB', 'hash', (TorrentFile(0, src),))
        inv.downloads[0]['episodes'] = ''
        self.assertFalse(Planner(self.fs).build(inv, set()).has_actions)

    def test_cancel_owned_wrong_season_pack_but_protect_shared_claim(self):
        inv, src, _ = self.inventory(cancelled=True)
        inv.torrents['qB:hash'] = Torrent('qB', 'hash', (TorrentFile(0, src, 4, frozenset({1}), 1),))
        self.assertEqual(Planner(self.fs).build(inv, {SCOPE}).drops, ['qB:hash'])
        other = Scope(SCOPE.source, SCOPE.media_id, 'tv', 4)
        inv.subscriptions.append(Subscription(2, other))
        inv.transfers.append(record(2, other, status=True, dest=src))
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertEqual(plan.drops, [])
        self.assertNotIn(src, plan.files)

    def test_exact_sidecars_and_all_hardlinks_removed(self):
        inv, src, dst = self.inventory(cancelled=True)
        alias = self.root / 'other/show.mkv'
        alias.parent.mkdir()
        os.link(dst, alias)
        sub = self.file('library/Show.S01E01.zh.ass')
        neighbour = self.file('library/Show.S01E010.zh.ass')
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertIn(str(alias), plan.files)
        self.assertIn(sub, plan.files)
        self.assertNotIn(neighbour, plan.files)

    def test_needed_hardlink_protects_all_aliases(self):
        inv, src, dst = self.inventory(cancelled=True)
        other = Scope('themoviedb', '200', 'tv', 1)
        alias = self.root / 'library/other.mkv'
        os.link(dst, alias)
        inv.subscriptions.append(Subscription(2, other))
        inv.transfers.append(record(2, other, status=True, dest=str(alias)))
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertNotIn(dst, plan.files)
        self.assertNotIn(str(alias), plan.files)
        self.assertIn(src, plan.files)
        self.assertNotIn(1, plan.transfer_ids)

    def test_failed_transfer_does_not_delete_destination(self):
        inv, src, dst = self.inventory(cancelled=True)
        inv.transfers[0]['status'] = False
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertIn(src, plan.files)
        self.assertNotIn(dst, plan.files)

    def test_remote_storage_history_is_retained(self):
        inv, _, dst = self.inventory(cancelled=True)
        inv.transfers[0]['dest_storage'] = 'alist'
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertNotIn(dst, plan.files)
        self.assertNotIn(1, plan.transfer_ids)
        self.assertTrue(plan.warnings)

    def test_unfinished_partial_source_is_planned(self):
        inv, src, _ = self.inventory(cancelled=True)
        Path(src).unlink()
        partial = self.file('download/Show.S01E01.mkv.!qB')
        plan = Planner(self.fs).build(inv, {SCOPE})
        self.assertIn(partial, plan.files)
        self.assertIn(partial, plan.mutable_sources)

    def test_outside_paths_and_unavailable_provider_abort(self):
        inv, _, _ = self.inventory(cancelled=True)
        inv.transfers[0]['dest'] = '/outside/Show.mkv'
        with self.assertRaises(ValueError):
            Planner(self.fs).build(inv, {SCOPE})
        inv.errors.append('Unavailable')
        with self.assertRaises(RuntimeError):
            Planner(self.fs).build(inv, {SCOPE})

    def test_cross_range_multiepisode_file_is_kept(self):
        inv, src, _ = self.inventory(start=2)
        inv.transfers[0]['episodes'] = 'E01-E02'
        inv.torrents['qB:hash'] = Torrent('qB', 'hash', (TorrentFile(0, src, 1, frozenset({1, 2})),))
        self.assertFalse(Planner(self.fs).build(inv, set()).has_actions)


class FilePolicyTests(Files):
    def test_rejects_broad_roots_traversal_and_parent_symlinks(self):
        for value in ('/', '/media', '/config', 'relative', str(self.root / '..')):
            with self.assertRaises(ValueError):
                PathPolicy([value])
        real = self.root / 'real'
        real.mkdir()
        (self.root / 'alias').symlink_to(real, target_is_directory=True)
        self.assertFalse(self.fs.allowed(self.root / 'alias/movie.mkv'))
        self.assertFalse(self.fs.allowed(self.root / '../movie.mkv'))

    def test_leaf_symlink_never_follows_target(self):
        target = self.file('real.mkv')
        link = self.root / 'link.mkv'
        link.symlink_to(target)
        self.fs.unlink(str(link), self.fs.stamp(link))
        self.assertFalse(link.is_symlink())
        self.assertTrue(Path(target).exists())

    def test_replaced_file_is_not_deleted_and_retry_is_idempotent(self):
        path = self.file('old.mkv')
        stamp = self.fs.stamp(path)
        Path(path).write_bytes(b'new content')
        with self.assertRaises(RuntimeError):
            self.fs.unlink(path, stamp)
        self.fs.unlink(path, self.fs.stamp(path))
        self.fs.unlink(path, stamp)


class ArchiveTests(Files):
    def test_capacity_eviction_survives_history_removal_and_keeps_new_episodes(self):
        inv, _, dst = self.inventory()
        before = artifacts(inv, self.fs)
        Path(dst).unlink()
        after = capture_evictions(before, [], inv.subscriptions, self.fs, {})
        self.assertTrue(blocked(SCOPE, {1}, after))
        self.assertFalse(blocked(SCOPE, {2}, after))
        self.assertTrue(blocked(SCOPE, None, after))
        self.assertTrue(blocked(SCOPE, {1, 2}, after))
        self.assertTrue(blocked(SCOPE, {1}, json.loads(json.dumps(after))))

    def test_alternate_version_and_intentional_range_pruning_not_archived(self):
        inv, _, dst = self.inventory()
        before = artifacts(inv, self.fs)
        Path(dst).unlink()
        alternate = [{**before[0], 'path': '/different/existing'}]
        self.assertEqual(capture_evictions(before, alternate, inv.subscriptions, self.fs, {}), {})
        inv.subscriptions = [Subscription(1, SCOPE, 80)]
        self.assertEqual(capture_evictions(before, [], inv.subscriptions, self.fs, {}), {})

    def test_restore_clears_prior_absence_and_hot_gate_is_cheap(self):
        inv, _, dst = self.inventory()
        ctl, host, store = self.controller(inv)
        ctl.run()
        calls = host.inventory_calls
        ctl.detect_before_download()
        self.assertEqual(host.inventory_calls, calls)
        Path(dst).unlink()
        host.inv.transfers = []
        self.assertTrue(blocked(SCOPE, {1}, ctl.detect_before_download()))
        ctl.restore([SCOPE.key])
        self.assertFalse(blocked(SCOPE, {1}, ctl.detect_before_download()))

    def test_mount_baseline_change_defers_absence_detection(self):
        inv, _, _ = self.inventory()
        ctl, _, store = self.controller(inv)
        ctl.run()
        store['roots'][str(self.root)] = [0, 0]
        with self.assertRaises(RuntimeError):
            ctl.detect_before_download()


class ExecutorTests(Files):
    def test_reload_instances_do_not_overwrite_concurrent_cancel_intents(self):
        inv, _, _ = self.inventory()
        store = {}
        def load():
            snapshot = deepcopy(store)
            time.sleep(0.01)
            return snapshot
        def save(value):
            store.clear()
            store.update(deepcopy(value))
        one = Controller(FakeHost(inv), self.fs, load, save, lambda: True, lock_path=self.root / 'state.lock')
        two = Controller(FakeHost(inv), self.fs, load, save, lambda: True, lock_path=self.root / 'state.lock')
        other = Scope('themoviedb', '200', 'tv', 1)
        threads = [threading.Thread(target=one.enqueue_cancel, args=(SCOPE,)),
                   threading.Thread(target=two.enqueue_cancel, args=(other,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(set(store['cancelled']), {SCOPE.key, other.key})

    def test_periodic_reconciliation_recovers_missed_cancel_event(self):
        inv, src, dst = self.inventory()
        ctl, host, store = self.controller(inv)
        ctl.run()
        host.inv.subscriptions = []
        ctl.run(dry_run=False)
        self.assertFalse(Path(src).exists() or Path(dst).exists())
        self.assertEqual(store['members'], [])
        self.assertEqual(store['cancelled'], {})

    def test_ambiguous_cancel_intent_stays_queued_for_repair(self):
        inv, src, _ = self.inventory(cancelled=True)
        inv.downloads[0]['seasons'] = ''
        ctl, _, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        ctl.run(dry_run=False)
        self.assertTrue(Path(src).exists())
        self.assertIn(SCOPE.key, store['cancelled'])
        self.assertIn(SCOPE.key, store['preview']['unresolved_scopes'])

    def test_dry_run_then_real_cancel_execution_order(self):
        inv, src, dst = self.inventory(cancelled=True)
        ctl, host, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        ctl.run()
        self.assertTrue(Path(src).exists() and Path(dst).exists())
        self.assertFalse(host.calls)
        ctl.run(dry_run=False)
        self.assertFalse(Path(src).exists() or Path(dst).exists())
        self.assertEqual([c[0] for c in host.calls], ['pause', 'drop', 'transfer', 'download', 'refresh'])
        self.assertEqual(store['cancelled'], {})
        self.assertNotIn('pending', store)

    def test_subscribe_changed_after_pause_aborts_and_restores_running_state(self):
        inv, src, dst = self.inventory(cancelled=True)
        ctl, host, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        host.on_pause = lambda: host.inv.subscriptions.append(Subscription(1, SCOPE))
        with self.assertRaises(StalePlan):
            ctl.run(dry_run=False)
        self.assertTrue(Path(src).exists() and Path(dst).exists())
        self.assertTrue(host.running['qB:hash'])
        self.assertNotIn('pending', store)
        self.assertIn(SCOPE.key, store['cancelled'])

    def test_library_replacement_after_preview_is_not_deleted(self):
        inv, src, dst = self.inventory(cancelled=True)
        ctl, host, _ = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        host.on_pause = lambda: Path(dst).write_bytes(b'replaced')
        with self.assertRaises(StalePlan):
            ctl.run(dry_run=False)
        self.assertTrue(Path(src).exists() and Path(dst).exists())

    def test_failure_is_journalled_and_retry_finishes_after_restart(self):
        inv, src, dst = self.inventory(cancelled=True)
        ctl, host, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        host.fail_transfer = True
        with self.assertRaises(RuntimeError):
            ctl.run(dry_run=False)
        self.assertFalse(Path(src).exists() or Path(dst).exists())
        self.assertIn('pending', store)
        self.assertIn(SCOPE.key, store['cancelled'])
        host.fail_transfer = False
        restarted = Controller(host, self.fs, lambda: deepcopy(store), ctl.save, lambda: True)
        restarted.run(dry_run=False)
        self.assertEqual(store['cancelled'], {})
        self.assertNotIn('pending', store)
        self.assertNotIn('last_error', store)

    def test_failed_drop_stays_paused_until_success_and_resubscribe_recovers(self):
        inv, src, _ = self.inventory(cancelled=True)
        ctl, host, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        host.fail_drop = True
        with self.assertRaises(RuntimeError):
            ctl.run(dry_run=False)
        self.assertTrue(Path(src).exists())
        self.assertFalse(host.running['qB:hash'])
        self.assertTrue(store['resume']['qB:hash'])
        host.inv.subscriptions = [Subscription(7, SCOPE)]
        ctl.run(dry_run=False)
        self.assertTrue(host.running['qB:hash'])
        self.assertTrue(Path(src).exists())

    def test_file_budget_and_live_disable_stop_execution(self):
        inv, src, _ = self.inventory(cancelled=True)
        ctl, host, store = self.controller(inv)
        ctl.enqueue_cancel(SCOPE)
        ctl.max_files = 1
        with self.assertRaises(RuntimeError):
            ctl.run(dry_run=False)
        self.assertTrue(Path(src).exists())
        ctl.max_files = 100
        ctl.active = lambda: False
        with self.assertRaises(RuntimeError):
            ctl.run(dry_run=False)
        self.assertFalse(host.calls)

    def test_plan_roundtrip_persists_stamps(self):
        inv, _, _ = self.inventory(cancelled=True)
        original = Planner(self.fs).build(inv, {SCOPE})
        self.assertEqual(Plan.from_dict(json.loads(json.dumps(original.to_dict()))).to_dict(), original.to_dict())

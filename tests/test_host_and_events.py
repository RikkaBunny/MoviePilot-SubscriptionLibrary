from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from support import SCOPE, plugin, sdk_queries, http, event, record
from subscriptionlibrary.host import MPHost
from subscriptionlibrary.models import Scope, Subscription


def context(episodes):
    return {'media_info': {'media_source': SCOPE.source, 'media_id': SCOPE.media_id, 'type': '电视剧'},
            'meta_info': {'season_list': [1], 'episode_list': episodes}}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.p = plugin.SubscriptionLibrary()
        self.p._enabled = True
        self.p._config = self.p.defaults()
        self.p._controller = Mock()
        self.p._controller.host.subscriptions.return_value = [Subscription(1, SCOPE)]
        self.p._controller.detect_before_download.return_value = {SCOPE.key: [1, 2]}

    def test_completion_veto_mutates_host_payload_dict_and_object(self):
        for data in ({'subscribe': {'type': '电视剧'}}, SimpleNamespace(subscribe=SimpleNamespace(type='电视剧'))):
            self.p.keep_subscription(event(data))
            self.assertTrue(self.p._get(data, 'cancel'))
        self.p._enabled = False
        data = {'subscribe': {'type': '电视剧'}}
        self.p.keep_subscription(event(data))
        self.assertNotIn('cancel', data)

    def test_completed_movies_retained_music_outside_scope(self):
        for kind, expected in [('电影', True), ('音乐', False)]:
            data = {'subscribe': {'type': kind}}
            self.p.keep_subscription(event(data))
            self.assertEqual(data.get('cancel', False), expected)

    def test_selection_respects_prior_plugin_result_and_keeps_new_episode(self):
        new = context([3])
        data = {'origin': 'Subscribe|{"id":1}', 'contexts': [context([99])], 'updated': True,
                'updated_contexts': [context([1]), new]}
        self.p.suppress_evicted_selection(event(data))
        self.assertEqual(data['updated_contexts'], [new])

    def test_whole_pack_blocked_new_single_episode_allowed(self):
        for eps, expected in [(None, True), ([1, 3], True), ([3], False)]:
            data = {'origin': 'Subscribe|{"id":1}', 'context': context(eps)}
            self.p.suppress_evicted_download(event(data))
            self.assertEqual(data.get('cancel', False), expected)

    def test_canonical_runtime_context_allows_unarchived_download(self):
        candidate = SimpleNamespace(
            media_info=SimpleNamespace(to_dict=lambda: context([3])['media_info']),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[3]))
        data = SimpleNamespace(origin='Subscribe|{"id":1}', context=candidate, cancel=False)
        self.p.suppress_evicted_download(event(data))
        self.assertFalse(data.cancel)
        candidate.meta_info.episode_list = [1]
        self.p.suppress_evicted_download(event(data))
        self.assertTrue(data.cancel)

    def test_canonical_media_field_precedes_legacy_alias(self):
        candidate = {**context([3]), 'mediainfo': {'type': '音乐'}}
        claim, episodes = MPHost.context_claim(candidate, Subscription(1, SCOPE))
        self.assertEqual((claim, episodes), (SCOPE, frozenset({3})))
        candidate['mediainfo'] = candidate.pop('media_info')
        claim, episodes = MPHost.context_claim(candidate, Subscription(1, SCOPE))
        self.assertEqual((claim, episodes), (SCOPE, frozenset({3})))

    def test_selected_episodes_override_whole_pack_only_when_explicit(self):
        data = {'origin': 'Subscribe|{"id":1}', 'context': context(None), 'episodes': [3]}
        self.p.suppress_evicted_download(event(data))
        self.assertNotIn('cancel', data)

    def test_manual_download_unaffected_and_prior_cancel_preserved(self):
        data = {'origin': 'Manual', 'context': context([1])}
        self.p.suppress_evicted_download(event(data))
        self.assertNotIn('cancel', data)
        self.p._controller.detect_before_download.assert_not_called()
        data = {'origin': 'Subscribe|{"id":1}', 'context': context([3]), 'cancel': True, 'source': 'other'}
        self.p.suppress_evicted_download(event(data))
        self.assertEqual(data['source'], 'other')

    def test_cancelled_or_invalid_subscription_stops_queued_download_even_without_archive_guard(self):
        self.p._config['archive_guard'] = False
        for origin in ('Subscribe|{"id":99}', 'Subscribe|bad-json'):
            data = {'origin': origin, 'context': context([3])}
            self.p.suppress_evicted_download(event(data))
            self.assertTrue(data['cancel'])
            selection = {'origin': origin, 'contexts': [context([3])]}
            self.p.suppress_evicted_selection(event(selection))
            self.assertEqual(selection['updated_contexts'], [])

    def test_archive_failure_stops_automatic_selection_and_download(self):
        self.p._controller.detect_before_download.side_effect = RuntimeError('storage failure')
        data = {'origin': 'Subscribe|{"id":1}', 'contexts': [context([3])]}
        self.p.suppress_evicted_selection(event(data))
        self.assertEqual(data['updated_contexts'], [])
        data = {'origin': 'Subscribe|{"id":1}', 'context': context([3])}
        self.p.suppress_evicted_download(event(data))
        self.assertTrue(data['cancel'])

    def test_cancel_event_is_durable_intent_not_immediate_filesystem_delete(self):
        data = {'subscribe_id': 1, 'subscribe_info': {**record(), 'season': 1}, 'idempotency_key': 'one'}
        self.p.subscription_deleted(event(data))
        self.p._controller.enqueue_cancel.assert_called_once_with(SCOPE)
        self.p._controller.run.assert_not_called()

    def test_subscribe_added_restores_archives(self):
        self.p.membership_changed(event({'subscribe_id': 1}, plugin.EventType.SubscribeAdded))
        self.p._controller.restore.assert_called_once_with([SCOPE.key])

    def test_defaults_cannot_delete_or_leave_a_mutating_get_endpoint(self):
        p = plugin.SubscriptionLibrary()
        self.assertFalse(p.defaults()['enabled'])
        self.assertTrue(p.defaults()['dry_run'])
        self.assertEqual(p.defaults()['managed_roots'], '')
        self.assertEqual([(r['path'], r['auth']) for r in p.get_api()], [('/status', 'bear')])

    def test_page_uses_human_names_and_does_not_dump_internal_state(self):
        self.p.save_data('state', {'labels': {SCOPE.key: 'Example Show'}, 'archived': {SCOPE.key: [1]},
                                  'preview': {'fingerprint': 'secret-internal-hash', 'files': {}, 'drops': [],
                                              'warnings': ['Season missing; retained']}})
        page = str(self.p.get_page())
        self.assertIn('Example Show', page)
        self.assertIn('Season missing; retained', page)
        self.assertNotIn('secret-internal-hash', page)
        form, _ = self.p.get_form()
        self.assertIn('Example Show', str(form))


class HostTests(unittest.TestCase):
    def setUp(self):
        self.p = SimpleNamespace(chain=Mock())
        self.host = MPHost(self.p)
        self.provider = Mock()
        self.provider.get_torrents.return_value = ([], False)
        self.service = SimpleNamespace(type='qbittorrent', instance=self.provider, module=Mock())
        self.host.downloaders.get_service.return_value = self.service
        for query in vars(sdk_queries).values():
            if isinstance(query, Mock):
                query.reset_mock(return_value=True, side_effect=True)
        sdk_queries.list_subscriptions.return_value = SimpleNamespace(items=[], has_next=False)
        sdk_queries.list_download_history.return_value = SimpleNamespace(items=[], has_next=False)
        sdk_queries.list_transfer_history.return_value = SimpleNamespace(items=[], has_next=False)
        http.delete.reset_mock(return_value=True, side_effect=True)

    def origin_record(self, **changes):
        target = {'id': 25, 'media_source': SCOPE.source, 'media_id': SCOPE.media_id,
                  'type': '电视剧', 'season': 1, 'episode_group': None, **changes}
        return record(seasons='', note={'source': 'Subscribe|' + json.dumps(target)})

    def test_native_origin_proves_missing_anime_season(self):
        row = self.origin_record(season=4)
        self.assertEqual(MPHost.normalize(row)['seasons'], 'S04')
        self.assertEqual(row['seasons'], '')

    def test_origin_identity_must_match_source_id_type_and_group(self):
        for change in ({'media_id': 'other'}, {'media_source': 'bangumi'},
                       {'type': '电影'}, {'episode_group': 'different'}):
            with self.subTest(change=change):
                self.assertEqual(MPHost.normalize(self.origin_record(**change))['seasons'], '')

    def test_missing_or_invalid_origin_is_retained_without_guessing(self):
        for value in (None, {}, {'source': 'Manual'}, {'source': 'Subscribe|bad'},
                      {'source': 'Subscribe|[]'}):
            self.assertEqual(MPHost.normalize(record(seasons='', note=value))['seasons'], '')

    def test_origin_requires_valid_subscription_id_and_explicit_season(self):
        for change in ({'id': 0}, {'id': '25'}, {'id': None}, {'season': None},
                       {'season': True}, {'season': -1}, {'season': '1'}):
            self.assertEqual(MPHost.normalize(self.origin_record(**change))['seasons'], '')

    def test_explicit_history_season_is_never_replaced_by_origin(self):
        row = self.origin_record()
        row['seasons'] = 'S02'
        self.assertEqual(MPHost.normalize(row)['seasons'], 'S02')

    def test_origin_proof_survives_subscription_cancellation(self):
        self.assertFalse(self.host.subscriptions())
        sdk_queries.list_download_history.return_value.items = [self.origin_record()]
        self.assertEqual(self.host.inventory(include_torrents=False).downloads[0]['seasons'], 'S01')

    def test_native_organized_target_proves_missing_season_and_episodes(self):
        row = record(seasons='', episodes='', status=True, dest_storage='local',
                     dest='/library/Show - S04E80 - Episode.mkv')
        result = MPHost.normalize(row)
        self.assertEqual((result['seasons'], result['episodes']), ('S04', 'E80'))
        row['dest'] = '/library/Show - S04E80-E81.mkv'
        self.assertEqual(MPHost.normalize(row)['episodes'], 'E80-E81')

    def test_target_season_conflict_or_invalid_existing_field_is_not_overridden(self):
        for seasons in ('S02', 'unparsed'):
            row = record(seasons=seasons, episodes='', status=True, dest='/library/Show.S01E01.mkv')
            result = MPHost.normalize(row)
            self.assertEqual((result['seasons'], result['episodes']), (seasons, ''))

    def test_target_marker_requires_success_local_video_and_explicit_single_marker(self):
        base = record(seasons='', episodes='', status=True, dest='/library/Show.S01E01.mkv')
        for changes in ({'status': False}, {'dest_storage': 'rclone'}, {'dest': '/library/Show01.mkv'},
                        {'dest': '/library/Show.S01E01.nfo'}, {'dest': '/library/S01E01-S02E02.mkv'},
                        {'dest': '/library/S01E00.mkv'}, {'dest': '/library/S01E03-E01.mkv'},
                        {'dest': None, 'src': '/downloads/Show.S01E01.mkv'}):
            result = MPHost.normalize({**base, **changes})
            self.assertEqual((result['seasons'], result['episodes']), ('', ''))

    def test_target_marker_preserves_existing_episode_fact(self):
        row = record(seasons='S01', episodes='E03', status=True, dest='/library/Show.S01E02.mkv')
        self.assertEqual(MPHost.normalize(row)['episodes'], 'E03')

    def test_pagination_and_truncated_page_refused(self):
        query = Mock(side_effect=[SimpleNamespace(items=[{'id': 1}], has_next=True),
                                  SimpleNamespace(items=[{'id': 2}], has_next=False)])
        self.assertEqual(MPHost._list(query), [{'id': 1}, {'id': 2}])
        self.assertEqual(query.call_args.kwargs['page']['page'], 2)
        with self.assertRaises(RuntimeError):
            MPHost._list(Mock(return_value=SimpleNamespace(items=[], has_next=True)))

    def test_provider_unavailable_is_not_treated_as_empty_tasks(self):
        sdk_queries.list_download_history.return_value.items = [record(downloader='qB', download_hash='hash')]
        self.provider.get_torrents.return_value = ([], True)
        inv = self.host.inventory()
        self.assertTrue(inv.errors)
        self.p.chain.list_torrents.assert_not_called()

    def test_drop_requires_verified_absence_and_requests_source_deletion(self):
        self.provider.get_torrents.side_effect = [([{'hash': 'hash'}], False), ([{'hash': 'hash'}], False)]
        self.p.chain.remove_torrents.return_value = True
        with self.assertRaises(RuntimeError):
            self.host.drop('qB:hash')
        self.p.chain.remove_torrents.assert_called_once_with(hashs=['hash'], delete_file=True, downloader='qB')

    def test_manifest_must_confirm_every_selected_priority(self):
        self.provider.get_torrents.return_value = ([{'hash': 'hash'}], False)
        self.provider.set_files.return_value = True
        self.p.chain.torrent_files.return_value = [SimpleNamespace(id=0, priority=1)]
        with self.assertRaises(RuntimeError):
            self.host.set_priorities('qB:hash', [0])
        self.p.chain.torrent_files.return_value = []
        with self.assertRaises(RuntimeError):
            self.host.set_priorities('qB:hash', [0])

    def test_missing_task_resume_is_noop(self):
        self.host.resume('qB:hash')
        self.p.chain.start_torrents.assert_not_called()

    def test_history_api_success_and_record_absence_both_required(self):
        response = Mock()
        response.json.return_value = {'success': True}
        http.delete.return_value = response
        sdk_queries.get_transfer_history.return_value = {'id': 1}
        with self.assertRaises(RuntimeError):
            self.host._delete_history('transfer', 1)
        sdk_queries.get_transfer_history.return_value = None
        self.host._delete_history('transfer', 1)
        args, kwargs = http.delete.call_args
        self.assertEqual(args[0], 'http://127.0.0.1:3001/api/v1/history/transfer?deletesrc=false&deletedest=false')
        self.assertEqual(kwargs['json'], {'id': 1})
        self.assertFalse(kwargs['allow_redirects'])
        response.json.return_value = {'success': False}
        with self.assertRaises(RuntimeError):
            self.host._delete_history('transfer', 1)

    def test_separate_media_sources_and_episode_groups_are_not_conflated(self):
        sub = Subscription(1, Scope('bangumi', '100', 'tv', 4, 'group'))
        scope, _ = MPHost.context_claim(context([1]), sub)
        self.assertIsNone(scope)
        sub = Subscription(1, Scope(SCOPE.source, SCOPE.media_id, 'tv', 4, 'group'))
        scope, _ = MPHost.context_claim(context([1]), sub)
        self.assertEqual(scope, sub.scope)

    def test_incomplete_save_path_uses_native_mapping(self):
        sdk_queries.list_download_history.return_value.items = [record(downloader='qB', download_hash='hash')]
        self.provider.get_torrents.return_value = ([{'hash': 'hash', 'download_path': '/remote/incomplete'}], False)
        self.service.module.normalize_return_path.return_value = '/local/incomplete'
        self.p.chain.list_torrents.return_value = [SimpleNamespace(hash='hash', save_path='/local/complete')]
        self.p.chain.torrent_files.return_value = [SimpleNamespace(id=0, name='Show.S01E01.mkv', priority=1)]
        with patch('subscriptionlibrary.host.Path.exists', return_value=False):
            inv = self.host.inventory()
        self.assertFalse(inv.errors)
        self.service.module.normalize_return_path.assert_called_once()
        self.assertEqual(inv.torrents['qB:hash'].files[0].episodes, frozenset({1}))


if __name__ == '__main__':
    unittest.main()

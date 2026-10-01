"""Subscription Library: subscriptions are durable membership, cancellation is deletion intent."""
from __future__ import annotations

import fcntl
import json
import threading

from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.scheduler import start_scheduler_job
from app.schemas.types import ChainEventType, EventType

from .archive import blocked, scope_from_key
from .controller import Controller
from .filesystem import PathPolicy
from .host import MPHost
from .models import Scope, Subscription, plain


class SubscriptionLibrary(_PluginBase):
    plugin_name = '订阅媒体库'
    plugin_desc = '保留已完成订阅；取消订阅联动清理；集数范围同步；容量清理后防重复下载。'
    plugin_icon = 'Moviepilot_A.jpg'
    plugin_version = '0.1.0'
    plugin_author = 'RikkaBunny'
    author_url = 'https://github.com/RikkaBunny'
    plugin_config_prefix = 'subscriptionlibrary_'
    plugin_order = 90
    auth_level = 2

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._config = {}
        self._controller = None
        self._config_error = None
        self._lifecycle_lock = threading.RLock()

    def init_plugin(self, config=None):
        with self._lifecycle_lock:
            self._config = {**self.defaults(), **(config or {})}
            self._enabled = bool(self._config['enabled'])
            self._config_error = None
            self._controller = None
            if not self._enabled:
                return
            try:
                roots = [line.strip() for line in self._config['managed_roots'].splitlines() if line.strip()]
                fs = PathPolicy(roots)
                self._controller = Controller(MPHost(self), fs, lambda: self.get_data('state'),
                                              lambda state: self.save_data('state', state), self._deletion_active,
                                              bool(self._config['archive_guard']), int(self._config['max_files']),
                                              self.get_data_path() / 'state.lock')
                if self._config.get('reset_root_baseline'):
                    self._controller.reset_baseline()
                if self._config.get('restore_scopes'):
                    self._controller.restore(list(self._config['restore_scopes']))
                run_once = bool(self._config.get('run_once'))
                self._config.update(run_once=False, reset_root_baseline=False, restore_scopes=[])
                self.update_config(self._config)
                if run_once:
                    self.run()
            except (ValueError, TypeError, OSError) as error:
                self._config_error = str(error)
                logger.error(f'{self.plugin_name}：配置未就绪，文件清理不运行')

    @staticmethod
    def defaults():
        return {'enabled': False, 'keep_completed': True, 'dry_run': True, 'archive_guard': True,
                'managed_roots': '', 'interval_minutes': 2, 'max_files': 10000, 'run_once': False,
                'restore_scopes': [], 'reset_root_baseline': False}

    def _deletion_active(self):
        live = self.get_config() or self._config
        return self._enabled and bool(live.get('enabled')) and not bool(live.get('dry_run', True))

    def get_state(self):
        return self._enabled

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return [{'path': '/status', 'endpoint': self.status, 'methods': ['GET'], 'auth': 'bear',
                 'summary': '订阅媒体库状态与最近预览'}]

    def status(self):
        state = self.get_data('state') or {}
        return {'success': True, 'data': {'enabled': self._enabled, 'dry_run': self._config.get('dry_run', True),
                                        'configuration_error': self._config_error, 'state': state}}

    def get_service(self):
        if not self._enabled or self._controller is None:
            return []
        minutes = min(60, max(1, int(self._config.get('interval_minutes') or 2)))
        return [{'id': 'reconcile', 'name': '订阅媒体库同步与归档检查', 'trigger': 'interval',
                 'func': self.run, 'kwargs': {'minutes': minutes}}]

    def stop_service(self):
        self._enabled = False

    def _schedule(self):
        try:
            start_scheduler_job(f'{self.__class__.__name__}_reconcile')
        except Exception:
            # The durable queue remains available for the next normal host service cycle.
            logger.info(f'{self.plugin_name}：变更已记录，将在下次巡检处理')

    def run(self):
        if not self._enabled or self._controller is None:
            return
        # The host owns job overlap; this lock also protects hot reload / multiple worker processes.
        path = self.get_data_path() / 'reconcile.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                report = self._controller.run(dry_run=not self._deletion_active())
                if report.get('warnings'):
                    logger.warning(f'{self.plugin_name}：预览有 {len(report["warnings"])} 项待核对')
            except Exception:
                logger.error(f'{self.plugin_name}：同步未完成，详情页可查看原因及重试计划')

    @staticmethod
    def _get(data, key, default=None):
        return data.get(key, default) if isinstance(data, dict) else getattr(data, key, default)

    @staticmethod
    def _set(data, **values):
        for key, value in values.items():
            if isinstance(data, dict):
                data[key] = value
            else:
                setattr(data, key, value)

    @eventmanager.register(ChainEventType.SubscribeCompletionCheck, priority=99)
    def keep_subscription(self, event: Event):
        if not self._enabled or not self._config.get('keep_completed', True):
            return
        data = event.event_data
        row = self._get(data, 'subscribe')
        if row is None:
            return
        if self._get(row, 'type') not in ('电影', '电视剧', 'movie', 'tv'):
            return
        # The host's pre-completion veto also preserves an unrecognised desired identity;
        # the separate cleaner will refuse that identity until it is resolved.
        self._set(data, cancel=True, source=self.plugin_name,
                  reason='下载完成后保留订阅作为媒体清单；取消订阅时再删除')

    @eventmanager.register(EventType.SubscribeDeleted)
    def subscription_deleted(self, event: Event):
        if not self._enabled or self._controller is None:
            return
        payload = plain(event.event_data)
        scope = Scope.from_dict(MPHost.normalize(payload.get('subscribe_info') or {}))
        # Handler failure propagates to the durable host event dispatcher for retry.
        self._controller.enqueue_cancel(scope)
        self._schedule()

    @eventmanager.register([EventType.SubscribeAdded, EventType.SubscribeModified, EventType.TransferComplete])
    def membership_changed(self, event: Event):
        if not self._enabled or self._controller is None:
            return
        if event.event_type == EventType.SubscribeAdded:
            payload = plain(event.event_data)
            sub_id = payload.get('subscribe_id')
            for sub in self._controller.host.subscriptions():
                if sub.id == sub_id:
                    # A new explicit subscription opts this media back into automatic downloads.
                    self._controller.restore([sub.scope.key])
        self._schedule()

    def _origin_subscription(self, origin):
        if not isinstance(origin, str) or (origin != 'Subscribe' and not origin.startswith('Subscribe|')):
            return False, None
        if '|' in origin:
            try:
                sub_id = int(json.loads(origin.split('|', 1)[1])['id'])
                sub = next((s for s in self._controller.host.subscriptions() if s.id == sub_id), None)
                if sub is None:
                    raise LookupError('订阅已取消，自动下载中止')
                return True, sub
            except (ValueError, TypeError, KeyError):
                raise LookupError('订阅来源无效，自动下载中止')
        return True, None

    @eventmanager.register(ChainEventType.ResourceSelection, priority=99)
    def suppress_evicted_selection(self, event: Event):
        if not self._enabled or self._controller is None:
            return
        data = event.event_data
        contexts = self._get(data, 'updated_contexts') if self._get(data, 'updated') else self._get(data, 'contexts')
        contexts = list(contexts or [])
        try:
            automatic, sub = self._origin_subscription(self._get(data, 'origin'))
            if not automatic:
                return
            archive = self._controller.detect_before_download() if self._config.get('archive_guard') else {}
            kept = []
            for ctx in contexts:
                scope, eps = MPHost.context_claim(ctx, sub)
                if scope is None or not blocked(scope, eps, archive):
                    kept.append(ctx)
            if len(kept) != len(contexts):
                self._set(data, updated=True, updated_contexts=kept, source=self.plugin_name)
        except Exception:
            # A failed archive lookup must not silently restart a just-evicted whole library.
            self._set(data, updated=True, updated_contexts=[], source=self.plugin_name)
            logger.error(f'{self.plugin_name}：归档保护状态不可用，自动资源选择已暂缓')

    @eventmanager.register(ChainEventType.ResourceDownload, priority=99)
    def suppress_evicted_download(self, event: Event):
        if not self._enabled or self._controller is None:
            return
        data = event.event_data
        if self._get(data, 'cancel'):
            return
        try:
            automatic, sub = self._origin_subscription(self._get(data, 'origin'))
            if not automatic:
                return
            archive = self._controller.detect_before_download() if self._config.get('archive_guard') else {}
            scope, eps = MPHost.context_claim(self._get(data, 'context'), sub)
            chosen = self._get(data, 'episodes')
            if chosen:
                eps = frozenset(chosen)
            if scope is not None and blocked(scope, eps, archive):
                self._set(data, cancel=True, source=self.plugin_name, reason='容量清理归档中；恢复下载后可重新获取')
        except Exception:
            self._set(data, cancel=True, source=self.plugin_name, reason='归档保护状态不可用，自动下载暂缓')

    def get_form(self):
        state = self.get_data('state') or {}
        choices = []
        for key, eps in state.get('archived', {}).items():
            scope = scope_from_key(key)
            label = f'{scope.source}:{scope.media_id} S{scope.season:02d} · ' + ('全部' if eps is None else f'{len(eps)} 集')
            choices.append({'title': label, 'value': key})
        def field(component, model, label, **props):
            return {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                {'component': component, 'props': {'model': model, 'label': label, **props}}]}
        form = [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                 'content': [{'component': 'div', 'text': '订阅是保留清单。完成后保留；取消时清理；容量清理后记住已清理集数，避免立即补下载。默认演练，仅生成预览。'}]},
                {'component': 'VRow', 'content': [
                    field('VSwitch', 'enabled', '启用插件'),
                    field('VSwitch', 'keep_completed', '下载完成后保留订阅'),
                    field('VSwitch', 'dry_run', '演练：预览清理，不删除'),
                    field('VSwitch', 'archive_guard', '容量清理后防止重复下载'),
                    field('VTextField', 'interval_minutes', '巡检间隔（分钟）', type='number', min=1, max=60),
                    field('VTextField', 'max_files', '单轮文件上限', type='number', min=1),
                    field('VTextarea', 'managed_roots', '管理的媒体和下载目录（每行一个）', rows=5,
                          placeholder='/media/downloads\n/media/tv\n/media/movies'),
                    field('VSelect', 'restore_scopes', '恢复这些媒体的自动下载（保存后生效）', items=choices, multiple=True, chips=True),
                    field('VSwitch', 'run_once', '保存后立即同步一次'),
                    field('VSwitch', 'reset_root_baseline', '核对挂载后重新建立目录基线')]},
                {'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal'},
                 'content': [{'component': 'div', 'text': '关闭演练后，取消订阅及缩小手动集数范围会永久删除对应下载任务、下载源和媒体文件。归档记录需通过上方恢复下载，或取消后重新订阅。'}]}]
        return form, self.defaults()

    def get_page(self):
        state = self.get_data('state') or {}
        preview = state.get('preview') or {}
        text = f'模式：{"演练" if self._config.get("dry_run", True) else "正式"}；' \
               f'最近预览：{state.get("last_preview", "尚未运行")}；' \
               f'任务：{len(preview.get("drops", []))}；文件：{len(preview.get("files", {}))}；' \
               f'归档媒体：{len(state.get("archived", {}))}；待处理取消：{len(state.get("cancelled", {}))}'
        rows = [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'}, 'text': text}]
        error = self._config_error or state.get('last_error')
        if error:
            rows.append({'component': 'VAlert', 'props': {'type': 'error'}, 'text': error})
        rows.append({'component': 'pre', 'text': json.dumps({'preview': preview, 'archived': state.get('archived', {}),
                                                          'pending': state.get('pending')}, ensure_ascii=False, indent=2)})
        return rows

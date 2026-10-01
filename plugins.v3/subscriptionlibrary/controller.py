"""Durable intent queue and idempotent executor using host-owned plugin storage."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import threading
from pathlib import Path

from .archive import capture_evictions, scope_from_key
from .filesystem import PathPolicy
from .models import Inventory, Plan, Scope
from .planner import Planner, artifacts


class StalePlan(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Controller:
    _process_lock = threading.RLock()

    def __init__(self, host, fs: PathPolicy, load, save, active, archive_enabled=True, max_files=10000,
                 lock_path: Path | None = None):
        self.host, self.fs = host, fs
        self.load, self.save, self.active = load, save, active
        self.archive_enabled, self.max_files = archive_enabled, max_files
        self.lock = self._process_lock
        self.lock_path = lock_path

    @contextmanager
    def locked(self):
        # Shared across reload instances, plus an advisory lock across host workers.
        with self.lock:
            if self.lock_path is None:
                yield
                return
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def state(self) -> dict:
        state = self.load() or {'schema': 1, 'cancelled': {}, 'archived': {}, 'artifacts': []}
        if state.get('schema') != 1:
            raise RuntimeError('Unsupported plugin state schema; refuse to overwrite')
        return state

    def enqueue_cancel(self, scope: Scope) -> None:
        with self.locked():
            state = self.state()
            state['cancelled'][scope.key] = now()
            self.save(state)

    def restore(self, keys: list[str]) -> None:
        with self.locked():
            state = self.state()
            for key in keys:
                state['archived'].pop(key, None)
            # Reset old absence observations too; otherwise the next guard rearchives them.
            state['artifacts'] = [r for r in state.get('artifacts', []) if r['scope'] not in keys]
            state['last_restore'] = now()
            self.save(state)

    def reset_baseline(self) -> None:
        with self.locked():
            state = self.state()
            state.pop('roots', None)
            state['artifacts'] = []
            self.save(state)

    def _roots(self, state: dict) -> None:
        current = {}
        for root in self.fs.roots:
            if not root.is_dir() or root.is_symlink():
                raise RuntimeError('媒体目录不可用，清理及归档检测已延后')
            s = root.stat()
            current[str(root)] = [s.st_dev, s.st_ino]
        previous = state.get('roots')
        if previous and previous != current:
            raise RuntimeError('媒体目录基线改变，请核对挂载并重新建立基线')
        state['roots'] = current

    def observe(self, state: dict, inventory: Inventory) -> None:
        self._roots(state)
        current_members = sorted({sub.scope.key for sub in inventory.subscriptions})
        # The event queue is primary; snapshot comparison repairs a missed removal event
        # only for subscriptions this plugin previously observed. Never adopt all orphans.
        for key in set(state.get('members', [])) - set(current_members):
            state['cancelled'].setdefault(key, now())
        state['members'] = current_members
        labels = state.setdefault('labels', {})
        labels.update({sub.scope.key: sub.name for sub in inventory.subscriptions if sub.name})
        current = artifacts(inventory, self.fs)
        if self.archive_enabled:
            state['archived'] = capture_evictions(state.get('artifacts', []), current,
                                                   inventory.subscriptions, self.fs, state['archived'])
        state['artifacts'] = current
        state['last_observed'] = now()

    def detect_before_download(self) -> dict:
        """DiskCleaner removes history without an event: detect absence in the download gate too."""
        with self.locked():
            state = self.state()
            self._roots(state)
            previous = state.get('artifacts', [])
            # Usually only stat the recorded targets. Query histories on the first observation
            # or on actual absence, so each search candidate does not scan the whole database.
            if state.get('last_observed') and all(self.fs.allowed(r['path']) and
                    (Path(r['path']).exists() or Path(r['path']).is_symlink()) for r in previous):
                return state['archived']
            inventory = self.host.inventory(include_torrents=False)
            self.observe(state, inventory)
            self.save(state)
            return state['archived']

    def _guard(self, plan: Plan, state: dict) -> None:
        if not self.active():
            raise RuntimeError('插件已停用或切换为演练，清理中止')
        self._roots(state)
        current = Inventory(self.host.subscriptions(), [], []).fingerprint
        if current != plan.fingerprint:
            raise StalePlan('订阅发生变化，旧计划已撤销，将重新计算')

    def execute(self, plan: Plan, state: dict) -> None:
        if len(plan.files) > self.max_files:
            raise RuntimeError('计划超过单轮文件上限，请先核对预览')
        self._guard(plan, state)
        # Persist original running states before pausing. A restart must not lose resume intent.
        task_keys = sorted(set(plan.drops) | set(plan.priorities))
        resume = state.setdefault('resume', {})
        for key in task_keys:
            if key not in resume:
                resume[key] = self.host.task_running(key)
        state['pending'] = plan.to_dict()
        state['refresh_pending'] = True
        self.save(state)
        removed = set()
        stale = False
        try:
            for key in task_keys:
                self._guard(plan, state)
                self.host.pause(key)
            # An unfinished source may grow until the native downloader confirms it is stopped.
            for path in plan.mutable_sources:
                current = self.fs.stamp(path)
                expected = plan.files[path]
                if current and current[:2] != tuple(expected)[:2]:
                    raise StalePlan('下载源文件已被替换，旧计划撤销')
                if current:
                    plan.files[path] = current
            for path, expected in plan.files.items():
                if not self.fs.unchanged(path, expected):
                    raise StalePlan('媒体文件已被替换或修改，旧计划撤销')
            state['pending'] = plan.to_dict()
            self.save(state)
            for key, indexes in plan.priorities.items():
                self._guard(plan, state)
                self.host.set_priorities(key, indexes)
            for key in plan.drops:
                self._guard(plan, state)
                self.host.drop(key)
                removed.add(key)
            for path, expected in plan.files.items():
                self._guard(plan, state)
                self.fs.unlink(path, expected)
            self.fs.prune(list(plan.files))
            # Histories are recovery evidence and are removed only after file operations succeed.
            for record_id in plan.transfer_ids:
                self._guard(plan, state)
                self.host.delete_transfer(record_id)
            for record_id in plan.download_ids:
                self._guard(plan, state)
                self.host.delete_download(record_id)
            state.pop('pending', None)
            state['last_applied'] = now()
            self.save(state)
        except StalePlan:
            stale = True
            raise
        finally:
            for key, was_running in list(resume.items()):
                if was_running and (key not in plan.drops or stale):
                    self.host.resume(key)
                if key not in plan.drops or key in removed or stale:
                    resume.pop(key, None)
                # A failed cancellation stays paused, with its original state journalled.
            self.save(state)

    def run(self, dry_run: bool = True) -> dict:
        with self.locked():
            state = self.state()
            try:
                inventory = self.host.inventory()
                self.observe(state, inventory)
                cancelled = {scope_from_key(key) for key in state['cancelled']}
                plan = Planner(self.fs).build(inventory, cancelled)
                state['preview'] = plan.to_dict()
                state['last_preview'] = now()
                self.save(state)
                if not dry_run:
                    pending = state.get('pending')
                    if pending:
                        previous = Plan.from_dict(pending)
                        if previous.fingerprint == inventory.fingerprint:
                            plan = previous
                        else:
                            for key, was_running in list(state.get('resume', {}).items()):
                                if was_running:
                                    self.host.resume(key)
                                state['resume'].pop(key, None)
                                self.save(state)
                            state['previous_plan'] = pending
                            state.pop('pending', None)
                            self.save(state)
                    if plan.has_actions:
                        self.execute(plan, state)
                    # Queue is acknowledged only after all plan steps succeed, even on retry.
                    for scope in cancelled:
                        if scope.key in plan.unresolved_scopes:
                            continue
                        state['cancelled'].pop(scope.key, None)
                        if not any(s.scope == scope for s in inventory.subscriptions):
                            state['archived'].pop(scope.key, None)
                    if state.get('refresh_pending'):
                        self.host.refresh()
                        state.pop('refresh_pending', None)
                    # Don't classify intentional range/cancellation pruning as a capacity eviction.
                    state['artifacts'] = artifacts(self.host.inventory(include_torrents=False), self.fs)
                    state['last_success'] = now()
                state.pop('last_error', None)
                self.save(state)
                return plan.to_dict()
            except StalePlan as error:
                state['previous_plan'] = state.pop('pending', None)
                state['last_error'] = str(error)
                self.save(state)
                raise
            except Exception as error:
                state['last_error'] = str(error)
                self.save(state)
                raise

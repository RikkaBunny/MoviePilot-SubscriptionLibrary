"""Bounded local-file operations, independent of downloader credentials."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .models import SIDECAR


class PathPolicy:
    def __init__(self, roots: list[str], scan_limit: int = 250000):
        self.roots = tuple(Path(p) for p in roots if p.strip())
        self.scan_limit = scan_limit
        forbidden = {'/', '/config', '/app', '/etc', '/usr', '/var', '/home', '/root', '/media', '/mnt'}
        if not self.roots or any(not p.is_absolute() or str(p) in forbidden or '..' in p.parts for p in self.roots):
            raise ValueError('Configure specific absolute media/download directories')
        for root in self.roots:
            if root.is_symlink() or root.resolve() != root:
                raise ValueError('Managed roots must not traverse symlinks')

    def allowed(self, value: str | Path) -> bool:
        p = Path(value)
        if not p.is_absolute() or '..' in p.parts:
            return False
        return any(p != root and p.is_relative_to(root) for root in self.roots) and p.parent.resolve() == p.parent

    def stamp(self, value: str | Path) -> tuple[int, int, int, int, int] | None:
        p = Path(value)
        if not self.allowed(p):
            raise ValueError('Path outside managed roots or through a symlink')
        try:
            s = p.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(s.st_mode) and not stat.S_ISLNK(s.st_mode):
            raise ValueError('Only regular files and leaf symlinks may be removed')
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode

    def sidecars(self, value: str) -> list[str]:
        p = Path(value)
        if not self.allowed(p) or not p.parent.is_dir():
            return []
        return [str(item) for item in p.parent.iterdir()
                if item.suffix.lower() in SIDECAR and item.name.startswith(p.stem + '.') and self.allowed(item)]

    def aliases(self, inodes: set[tuple[int, int]]) -> dict[str, tuple]:
        """Find hard links only under explicitly managed roots, never follow directories."""
        found, visited, count = {}, set(), 0
        if not inodes:
            return found
        for root in self.roots:
            if not root.is_dir():
                raise ValueError('Managed root is unavailable; deletion deferred')
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=self._raise):
                dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
                for name in names:
                    p = Path(directory, name)
                    if p in visited:
                        continue
                    visited.add(p)
                    count += 1
                    if count > self.scan_limit:
                        raise ValueError('Hard-link scan limit exceeded; deletion deferred')
                    s = self.stamp(p)
                    if s and stat.S_ISREG(s[4]) and s[:2] in inodes:
                        found[str(p)] = s
        return found

    @staticmethod
    def _raise(error):
        raise error

    def unchanged(self, path: str, expected: tuple) -> bool:
        current = self.stamp(path)
        return current is None or current == tuple(expected)

    def unlink(self, path: str, expected: tuple) -> None:
        if not self.unchanged(path, expected):
            raise RuntimeError('File changed after preview; cleanup deferred')
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return

    def prune(self, paths: list[str]) -> None:
        for p in sorted({Path(p).parent for p in paths}, key=lambda x: len(x.parts), reverse=True):
            while self.allowed(p) and p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    break
                p = p.parent

"""Bounded local-file operations, independent of downloader credentials."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from xml.etree import ElementTree

from .models import SIDECAR, VIDEO


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

    def cancel_metadata(self, scope, destination: str, selected: set[str], active_scopes) -> dict[str, tuple]:
        """Standard scraper files require matching NFO identity and no remaining video.

        Called for whole cancellation only. Unknown files are never adopted. A
        surviving season/subscription or video keeps its shared series artwork.
        """
        target = Path(destination)
        if not self.allowed(target) or target.suffix.lower() not in VIDEO:
            return {}
        folder = target.parent
        nfo_name = 'tvshow.nfo' if scope.kind == 'tv' else 'movie.nfo'
        candidates = [folder / nfo_name]
        if scope.kind == 'tv':
            candidates.append(folder.parent / nfo_name)
        nfo = next((p for p in candidates if self._nfo_identity(p, scope)), None)
        if nfo is None:
            return {}
        series = nfo.parent
        same_media = lambda s: (s.source, s.media_id, s.kind) == (scope.source, scope.media_id, scope.kind)
        active = [s for s in active_scopes if same_media(s)]
        artwork = {'poster.jpg', 'poster.png', 'folder.jpg', 'backdrop.jpg', 'backdrop.png',
                   'fanart.jpg', 'fanart.png', 'clearart.png', 'clearlogo.png', 'logo.png',
                   'thumb.jpg', 'landscape.jpg', 'banner.jpg'}
        result = {}

        def collect(directory, names, season_art=False):
            for path in directory.iterdir():
                standard = path.name.lower() in names or (season_art and re.fullmatch(
                    r'season\d{1,3}-(poster|banner|landscape|thumb)\.(jpg|jpeg|png)', path.name.lower()))
                if standard and self.allowed(path) and not path.is_dir():
                    stamp = self.stamp(path)
                    if stamp:
                        result[str(path)] = stamp

        if (folder != series and folder.is_dir() and not any(s.season == scope.season for s in active)
                and not self._remaining_video(folder, selected)):
            collect(folder, artwork | {'season.nfo'})
        if not active and not self._remaining_video(series, selected):
            collect(series, artwork | {nfo_name, 'season.nfo'}, season_art=True)
        return result

    def _nfo_identity(self, path: Path, scope) -> bool:
        if not self.allowed(path) or not path.is_file() or path.is_symlink():
            return False
        before = self.stamp(path)
        if not before or before[2] > 2 * 1024 * 1024:
            return False
        source = {'themoviedb': 'tmdb'}.get(scope.source, scope.source)
        try:
            root = ElementTree.fromstring(path.read_bytes())
            expected_root = 'tvshow' if scope.kind == 'tv' else 'movie'
            if root.tag.rsplit('}', 1)[-1].lower() != expected_root:
                return False
            ids = set()
            # Cast/crew nodes also contain tmdbid. Only direct media identity
            # fields identify this NFO's movie or series.
            for node in root:
                tag = node.tag.rsplit('}', 1)[-1].lower()
                if ((tag == 'uniqueid' and node.get('type', '').lower() in {source, scope.source})
                        or tag == source + 'id'):
                    ids.add((node.text or '').strip())
            return ids == {scope.media_id} and self.unchanged(str(path), before)
        except (ElementTree.ParseError, OSError, ValueError):
            return False

    def cancel_empty_metadata(self, cancelled, selected: set[str], active_scopes) -> dict[str, tuple]:
        """Repair scraper-only directories after older versions removed their histories.

        Requires a current cancellation, matching NFO identity, no active subscription
        for that media, and no remaining video. It never adopts unidentified files.
        """
        eligible = [s for s in cancelled if not any(
            (a.source, a.media_id, a.kind) == (s.source, s.media_id, s.kind) for a in active_scopes)]
        if not eligible:
            return {}
        result, visited, count = {}, set(), 0
        for root in self.roots:
            for parent, dirs, names in os.walk(root, followlinks=False, onerror=self._raise):
                dirs[:] = [d for d in dirs if not Path(parent, d).is_symlink()]
                count += len(names)
                if count > self.scan_limit:
                    raise ValueError('Metadata scan limit exceeded; deletion deferred')
                for name in names:
                    if name.lower() not in {'tvshow.nfo', 'movie.nfo'}:
                        continue
                    nfo = Path(parent, name)
                    if str(nfo) in visited:
                        continue
                    visited.add(str(nfo))
                    for scope in eligible:
                        expected = 'tvshow.nfo' if scope.kind == 'tv' else 'movie.nfo'
                        if name.lower() != expected or not self._nfo_identity(nfo, scope):
                            continue
                        folder = nfo.parent
                        if self._remaining_video(folder, selected):
                            continue
                        result.update(self.cancel_metadata(scope, str(folder / 'metadata-proof.mkv'),
                                                           selected, active_scopes))
                        if scope.kind == 'tv':
                            for child in folder.iterdir():
                                if (child.is_dir() and not child.is_symlink()
                                        and re.fullmatch(r'(?i)(?:Season\s*\d{1,3}|S\d{1,3})', child.name)):
                                    result.update(self.cancel_metadata(scope, str(child / 'metadata-proof.mkv'),
                                                                       selected, active_scopes))
                        break
        return result

    def _remaining_video(self, directory: Path, selected: set[str]) -> bool:
        count = 0
        for parent, dirs, names in os.walk(directory, followlinks=False, onerror=self._raise):
            if any(Path(parent, d).is_symlink() for d in dirs):
                return True
            for name in names:
                count += 1
                if count > self.scan_limit:
                    raise ValueError('Metadata scan limit exceeded; deletion deferred')
                path = Path(parent, name)
                suffix = Path(path.name.removesuffix('.!qB')).suffix.lower()
                if suffix in VIDEO and str(path) not in selected:
                    return True
        return False

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

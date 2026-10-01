"""Build the native MP release asset, containing plugin files at the archive root."""
import hashlib
import json
from pathlib import Path
import zipfile


def build():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / 'package.v3.json').read_text())['SubscriptionLibrary']['version']
    output = root / 'dist' / f'subscriptionlibrary_v{version}.zip'
    output.parent.mkdir(exist_ok=True)
    source = root / 'plugins.v3/subscriptionlibrary'
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*.py')):
            if '__pycache__' not in path.parts:
                # Fixed metadata makes repeated builds reproducible for this source.
                info = zipfile.ZipInfo(str(path.relative_to(source)), date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo('LICENSE', date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, (root / 'LICENSE').read_bytes())
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{checksum}  {output.name}\n')
    print(output)


if __name__ == '__main__':
    build()

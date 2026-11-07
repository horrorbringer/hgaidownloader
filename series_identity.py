"""Bind download directories to a source, independently of its display title."""
import hashlib
import json
from pathlib import Path
from safe_paths import safe_child, sanitize_filename

MARKER = '.series-source.json'


def source_id(url: str) -> str:
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def folder_owner(folder: Path) -> str | None:
    marker = folder / MARKER
    if not marker.exists():
        return None
    data = json.loads(marker.read_text(encoding='utf-8'))
    return data['source_url']


def series_folder(root: Path, title: str, url: str, stored=None, *, create=True) -> Path:
    root = root.expanduser().resolve()
    if stored and stored.get('folder') and Path(stored['output_root']).resolve() == root:
        previous = Path(stored['folder']).resolve()
        if previous != root and previous.is_relative_to(root) and folder_owner(previous) == url:
            return previous
    name = f'{sanitize_filename(title)[:140]} [{source_id(url)[:16]}]'
    folder = safe_child(root, name)
    owner = folder_owner(folder)
    if owner is not None:
        if owner != url:
            raise ValueError('This download directory belongs to a different series')
        return folder
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Cannot adopt an existing directory without source ownership information')
    if not create:
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder / MARKER).open('x', encoding='utf-8') as output:
            json.dump({'version': 1, 'source_url': url, 'source_id': source_id(url)}, output)
    except FileExistsError:
        if folder_owner(folder) != url:
            raise ValueError('The download directory was claimed by another series')
    return folder

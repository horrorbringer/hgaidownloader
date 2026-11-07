"""Inventory and recycle only explicitly selected, inactive checkpoint files."""
import re
from dataclasses import dataclass
from pathlib import Path
from file_lifecycle import DeleteResult, delete_file, file_identity


def checkpoint_root(series: Path) -> Path:
    series = series.resolve()
    root = series / '.series-partials'
    if root.is_symlink() or root.is_junction() or not root.resolve().is_relative_to(series):
        raise ValueError('Checkpoint storage must stay inside the series directory')
    return root


def ensure_checkpoint_directory(series: Path, identifier: str) -> Path:
    if not re.fullmatch('[0-9a-f]{24}', identifier):
        raise ValueError('Invalid checkpoint identifier')
    root = checkpoint_root(series)
    root.mkdir(exist_ok=True)
    folder = root / identifier
    validate_checkpoint_directory(series, folder)
    folder.mkdir(exist_ok=True)
    validate_checkpoint_directory(series, folder)
    return folder


def validate_checkpoint_directory(series: Path, folder: Path):
    root = checkpoint_root(series)
    if (folder.parent != root or not re.fullmatch('[0-9a-f]{24}', folder.name)
            or folder.is_symlink() or folder.is_junction()
            or not folder.resolve().is_relative_to(root.resolve())):
        raise ValueError('Checkpoint directory must stay inside checkpoint storage')
    if folder.exists():
        for path in folder.iterdir():
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Checkpoint files cannot redirect outside checkpoint storage')


def selected_files(series: Path, identifiers) -> list[Path]:
    root = checkpoint_root(series)
    paths = []
    for identifier in identifiers:
        if not re.fullmatch('[0-9a-f]{24}', identifier):
            raise ValueError('Invalid checkpoint identifier')
        folder = root / identifier
        if folder.is_symlink() or folder.is_junction():
            raise ValueError('Checkpoint links cannot be cleaned up')
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                    paths.append(path)
    return paths


@dataclass(frozen=True)
class Checkpoint:
    identifier: str
    example: str
    size: int
    modified: float


def inventory(series: Path) -> list[Checkpoint]:
    root = checkpoint_root(series)
    if not root.is_dir():
        return []
    result = []
    for folder in root.iterdir():
        if not re.fullmatch('[0-9a-f]{24}', folder.name):
            continue
        files = selected_files(series, [folder.name])
        if files:
            stats = [path.stat() for path in files]
            result.append(Checkpoint(folder.name, files[0].name, sum(s.st_size for s in stats),
                                     max(s.st_mtime for s in stats)))
    return sorted(result, key=lambda row: row.modified, reverse=True)


def recycle_checkpoints(series: Path, paths, cancelled, identities=None):
    root = checkpoint_root(series)
    for path in paths:
        if cancelled.is_set():
            return
        # Revalidate after confirmation; the folder may have changed on disk.
        if path not in selected_files(series, [path.parent.name]):
            yield DeleteResult(path, 0, False, 'Checkpoint path changed or is no longer available')
            continue
        expected = identities[path] if identities is not None else file_identity(path)
        yield delete_file(path, recycle=True, expected=expected)
    for folder in {path.parent for path in paths}:
        if folder.parent == root and not folder.is_symlink() and not folder.is_junction():
            try:
                folder.rmdir()
            except OSError:
                pass

"""Find completed episode videos without relying on their current display label."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from file_lifecycle import FileIdentity, file_identity

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.ts'}
OUTPUT_EXTENSIONS = ('.mp4', '.mkv', '.webm')


def episode_number(path: Path) -> int | None:
    stem = Path(path).stem
    for pattern in (r'^0*(\d+)(?=[_.\s-]|$)', r'^(?:episode|ep|e)[.\s_-]*0*(\d+)(?=[_.\s-]|$)',
                    r'^s\d+e0*(\d+)(?=[_.\s-]|$)', r'^第\s*0*(\d+)\s*集'):
        match = re.match(pattern, stem, re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def completed_video(path: Path) -> bool:
    lowered = path.name.casefold()
    return (path.suffix.casefold() in VIDEO_EXTENSIONS and not path.is_symlink()
            and path.is_file() and not any(token in lowered for token in
                                          ('.invalid-', '.downloading.', '.part.', '.tmp.')))


def episode_candidates(folder: Path, number: int, label: str, registered_paths=()) -> list[Path]:
    registered_paths = {Path(path) for path in registered_paths}
    paths = sorted(registered_paths, key=str)
    if folder.is_dir():
        paths.extend(sorted(folder.iterdir(), key=lambda path: path.name.casefold()))
    exact_stem = f'{number:02d}_{label}'.casefold()
    unique = []
    seen = set()
    for path in paths:
        try:
            resolved = path.resolve()
            if resolved in seen or not completed_video(path):
                continue
            if path in registered_paths or episode_number(path) == number or path.stem.casefold() == exact_stem:
                seen.add(resolved)
                unique.append(path)
        except (OSError, ValueError):
            continue
    # Registered files take precedence; directory matches are stable and literal,
    # so brackets or other glob characters in filenames have no special meaning.
    return unique


def available_output_template(folder: Path, number: int, label: str) -> Path:
    base = f'{number:02d}_{label}'
    index = 0
    while True:
        stem = base if index == 0 else base + ('.recovered' if index == 1 else f'.recovered-{index}')
        if not any(os.path.lexists(folder / (stem + extension)) for extension in OUTPUT_EXTENSIONS):
            return folder / (stem + '.%(ext)s')
        index += 1


@dataclass(frozen=True)
class ExistingVideo:
    path: Path
    number: int | None
    verified: bool
    size: int
    status: str
    identity: FileIdentity | None = None


def inspect_folder(folder: Path, probe, cancelled=None) -> list[ExistingVideo]:
    rows = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if cancelled and cancelled.is_set():
            break
        try:
            if not completed_video(path):
                continue
            identity = file_identity(path)
            number = episode_number(path)
            verified = bool(number and probe(path))
            changed = file_identity(path) != identity
            verified = verified and not changed
            rows.append(ExistingVideo(path.resolve(), number, verified, identity.size,
                        'Changed during verification' if changed else 'Ready' if verified else
                        'No episode number' if number is None else 'Verification failed', identity))
        except (OSError, ValueError):
            rows.append(ExistingVideo(path, episode_number(path), False, 0, 'File unavailable'))
    return rows


def conflicting_claims(library, url: str) -> set[Path]:
    claimed = set()
    for source in library.all():
        if source['url'] == url:
            continue
        for record in source['episodes']:
            if record.get('file'):
                try:
                    claimed.add(Path(record['file']).resolve())
                except (OSError, ValueError):
                    pass
    return claimed


def registered_episode_paths(stored, number, url, conflicts):
    from series_identity import folder_owner
    for record in stored['episodes'] if stored else ():
        if record['number'] != number or not record.get('file'):
            continue
        path = Path(record['file'])
        try:
            if path.resolve() not in conflicts and folder_owner(path.parent) in (None, url):
                return [path]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return []

"""Portable filename rules and containment checks for untrusted page titles."""
import re
from pathlib import Path


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '_', name).strip().rstrip('. ')
    if not value:
        return 'series'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', value.split('.')[0]):
        value = '_' + value
    return value[:180].rstrip('. ')


def safe_child(root: Path, name: str) -> Path:
    root = root.expanduser().resolve()
    target = (root / sanitize_filename(name)).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError('The series directory must stay inside the save directory')
    return target

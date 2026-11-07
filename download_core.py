"""Isolated yt-dlp downloads with fragment checkpoints and cooperative cancellation."""
import multiprocessing
import hashlib
import queue
import time
import os
from pathlib import Path
from checkpoint_storage import ensure_checkpoint_directory, validate_checkpoint_directory


def publish_without_overwrite(completed: Path, destination: Path):
    """Publish atomically without replacing any file that appeared meanwhile."""
    if os.name == 'nt':
        # Windows rename fails atomically if the destination already exists.
        completed.rename(destination)
    else:
        os.link(completed, destination)
        completed.unlink()


def download_options(target, referer):
    template = str(target)
    placeholder = '%(ext)s'
    if not template.endswith(placeholder):
        raise ValueError('The output template must end in %(ext)s')
    template = template[:-len(placeholder)].replace('%', '%%') + placeholder
    return {
        'outtmpl': template, 'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4', 'concurrent_fragment_downloads': 8,
        'nocheckcertificate': False, 'continuedl': True, 'nopart': False,
        'hls_prefer_native': True, 'skip_unavailable_fragments': False,
        'quiet': True, 'noprogress': True, 'socket_timeout': 15,
        'http_headers': {'User-Agent': 'Mozilla/5.0', 'Referer': referer},
    }


def _download_process(url, options, messages, cancelled, series, checkpoint_dir):
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
    last_update = 0.0
    last_fragment = None

    def progress(data):
        nonlocal last_update, last_fragment
        if cancelled.is_set():
            raise DownloadCancelled('Download cancelled')
        now = time.monotonic()
        fragment = data.get('fragment_index')
        if now - last_update < .1 and fragment == last_fragment and data['status'] != 'finished':
            return
        last_update, last_fragment = now, fragment
        fields = ('status', 'total_bytes', 'total_bytes_estimate', 'downloaded_bytes',
                  'fragment_count', 'fragment_index', '_speed_str', '_eta_str')
        messages.put(('progress', {key: data[key] for key in fields if key in data}))

    options['progress_hooks'] = [progress]
    try:
        validate_checkpoint_directory(series, checkpoint_dir)
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
        messages.put(('done', None))
    except Exception as exc:
        messages.put(('error', str(exc)))
    finally:
        messages.close()
        messages.join_thread()


def download_with_progress(url, target, referer, cancel_event, on_progress):
    # A process boundary ensures cancelled yt-dlp fragment streams release their
    # Windows file handles before a retry, including exception traceback cycles.
    if cancel_event.is_set():
        raise RuntimeError('Download cancelled')
    target = Path(target)
    # Codec fallbacks and different manifests must never share fragment state.
    fingerprint = hashlib.sha256((str(target.absolute()) + '\0' + url).encode()).hexdigest()[:24]
    checkpoint_dir = ensure_checkpoint_directory(target.parent, fingerprint)
    cached_target = checkpoint_dir / target.name
    context = multiprocessing.get_context('spawn')
    messages = context.Queue()
    cancelled = context.Event()
    worker = context.Process(target=_download_process,
                             args=(url, download_options(cached_target, referer), messages, cancelled,
                                   target.parent, checkpoint_dir))
    worker.start()
    cancel_started = None
    try:
        while True:
            if cancel_event.is_set():
                cancelled.set()
                cancel_started = cancel_started or time.monotonic()
                if time.monotonic() - cancel_started > 20:
                    raise RuntimeError('Download cancelled')
            try:
                kind, data = messages.get(timeout=.1)
            except queue.Empty:
                if not worker.is_alive():
                    raise RuntimeError('Download cancelled' if cancelled.is_set() else
                                       'The download worker exited unexpectedly')
                continue
            if kind == 'progress':
                on_progress(data)
            elif kind == 'error':
                raise RuntimeError('Download cancelled' if cancel_event.is_set() else data)
            else:
                if cancel_event.is_set():
                    raise RuntimeError('Download cancelled')
                validate_checkpoint_directory(target.parent, checkpoint_dir)
                stem = cached_target.name.removesuffix('.%(ext)s')
                outputs = [checkpoint_dir / (stem + extension)
                           for extension in ('.mp4', '.mkv', '.webm')]
                completed = next((path for path in outputs if path.is_file()), None)
                if not completed:
                    raise RuntimeError('The download worker produced no supported media file')
                publish_without_overwrite(completed, target.with_name(completed.name))
                try:
                    checkpoint_dir.rmdir()
                except OSError:
                    pass
                return
    finally:
        cancelled.set()
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join()
        messages.close()
        messages.join_thread()
        worker.close()

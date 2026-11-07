"""Manual episode-grid responsiveness check (requires a desktop session)."""

import sys
import time
import tkinter as tk
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from series_gui_all import SeriesDownloaderApp


root = tk.Tk()
temporary = tempfile.TemporaryDirectory()
app = SeriesDownloaderApp(root, Path(temporary.name) / 'library.db')
for number in range(1, 1001):
    item = f"ep-{number}"
    app.episode_list.insert("", "end", iid=item,
                            values=("☐", f"Episode {number}", "Waiting", "—", "—", "—"))
    app.episode_order.append(item)
root.update()

started = time.perf_counter()
app.episode_list.selection_set("ep-500")
root.update()
print(f"Selection: {(time.perf_counter() - started) * 1000:.1f} ms")

started = time.perf_counter()
app.episode_search.set("500")
root.update()
print(f"Filter: {(time.perf_counter() - started) * 1000:.1f} ms")
root.destroy()
temporary.cleanup()

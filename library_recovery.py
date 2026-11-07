"""Startup recovery UI. Original databases are preserved unless rebuild is chosen."""
import tempfile
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
from library_store import LibraryStore, LibraryRecoveryRequired, library_diagnostics, rebuild_library


def open_library(root, path):
    try:
        return LibraryStore(path)
    except LibraryRecoveryRequired as error:
        return recover_library(root, Path(path), str(error))


def recover_library(root, path, error):
    result = []
    window = tk.Toplevel(root)
    window.title('Library recovery')
    window.geometry('720x380')
    window.transient(root)
    window.grab_set()
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text='The saved library could not be opened.',
              font=('Segoe UI', 13, 'bold')).pack(anchor=tk.W)
    ttk.Label(frame, text=str(path), wraplength=670).pack(anchor=tk.W, pady=8)
    details = scrolledtext.ScrolledText(frame, height=8, wrap=tk.WORD)
    details.pack(fill=tk.BOTH, expand=True)

    def report(text):
        details.configure(state='normal')
        details.delete('1.0', tk.END)
        details.insert(tk.END, text)
        details.configure(state='disabled')

    report(error)
    ttk.Label(frame, text='Continue uses a temporary library for this session. Rebuild preserves a backup first.',
              wraplength=670).pack(anchor=tk.W, pady=8)
    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X)

    def continue_session():
        temporary = tempfile.TemporaryDirectory(prefix='series-library-session-')
        store = LibraryStore(Path(temporary.name) / 'library.db')
        store.temporary_directory = temporary
        result.append(store)
        window.destroy()

    def rebuild():
        try:
            store, backup = rebuild_library(path)
        except Exception as exc:
            messagebox.showerror('Recovery failed', str(exc), parent=window)
            return
        store.recovery_backup = backup
        result.append(store)
        window.destroy()

    ttk.Button(buttons, text='Check diagnostics',
               command=lambda: report(library_diagnostics(path))).pack(side=tk.LEFT)
    ttk.Button(buttons, text='Back up and rebuild', command=rebuild).pack(side=tk.LEFT, padx=8)
    continue_button = ttk.Button(buttons, text='Continue this session', command=continue_session)
    continue_button.pack(side=tk.RIGHT)
    ttk.Button(buttons, text='Exit', command=window.destroy).pack(side=tk.RIGHT, padx=8)
    continue_button.focus_set()
    root.wait_window(window)
    if result:
        return result[0]
    root.destroy()
    raise SystemExit(0)

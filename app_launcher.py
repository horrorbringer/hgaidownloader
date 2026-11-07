"""Executable entry point. Dispatch frozen worker processes before opening Tk."""

import multiprocessing


def main() -> int:
    import argparse
    import logging
    import os
    import sys
    from app_runtime import configure_bundled_tools, prepare_user_data, user_data_dir
    from app_version import APP_VERSION

    parser = argparse.ArgumentParser(description="Series Downloader")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    parser.add_argument("--self-test", action="store_true", help="Run offline release diagnostics")
    parser.add_argument("--report", help="Write self-test results to this JSON file")
    args = parser.parse_args()
    configure_bundled_tools()
    log_dir = user_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        log_dir / "application.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
    # Windowed executables have no console streams; libraries may still use them.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    try:
        if args.self_test:
            from release_smoke import run_self_test
            return run_self_test(args.report)
        prepare_user_data()
        from series_gui_all import main as gui_main
        gui_main()
        return 0
    except Exception:
        logging.exception("Application startup failed")
        if not args.self_test:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Series Downloader", "The application could not start.\n\n"
                                 f"Details were saved to:\n{log_dir / 'application.log'}", parent=root)
            root.destroy()
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

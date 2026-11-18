"""Pressroom desktop app: the HTML UI in a native window, backed by the local engine."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid

import webview
from webview.dom import DOMEventHandler

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pressroom import engine  # type: ignore
else:
    from . import engine


def resource(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


class Api:
    def __init__(self):
        self._window: webview.Window | None = None
        self._files: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ files
    def pick_files(self):
        res = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                              file_types=("PDF files (*.pdf)", "All files (*.*)"))
        return [self._add(p) for p in (res or [])]

    def _add(self, path: str) -> dict:
        fid = uuid.uuid4().hex[:10]
        try:
            info = engine.analyze(path)
        except engine.EngineError as e:
            return {"id": fid, "name": os.path.basename(path), "path": path, "error": str(e)}
        except Exception as e:  # unexpected: still tell the user which file
            return {"id": fid, "name": os.path.basename(path), "path": path, "error": f"Couldn't read this file ({e.__class__.__name__})."}
        with self._lock:
            self._files[fid] = {"path": path, "result": None}
        info["id"] = fid
        return info

    def on_drop(self, e):
        files = e.get("dataTransfer", {}).get("files", [])
        paths = [f.get("pywebviewFullPath") for f in files]
        paths = [p for p in paths if p and p.lower().endswith(".pdf")]
        if not paths:
            return
        infos = [self._add(p) for p in paths]
        self._js(f"window.pressroom.added({json.dumps(infos)})")

    def remove(self, fid):
        with self._lock:
            self._files.pop(fid, None)
        return True

    # ------------------------------------------------------------ work
    def compress(self, fid, target_mb, floor_dpi):
        entry = self._files.get(fid)
        if not entry:
            return {"error": "This file is no longer in the list."}

        def progress(step, pct):
            self._js(f"window.pressroom.progress({json.dumps({'id': fid, 'step': step, 'pct': pct})})")

        try:
            r = engine.compress(entry["path"], float(target_mb), int(floor_dpi), progress)
        except engine.EngineError as e:
            return {"error": str(e)}
        except MemoryError:
            return {"error": "Ran out of memory on this file. Close other apps and try again."}
        except Exception as e:
            return {"error": f"Something went wrong while compressing ({e.__class__.__name__}: {e})."}
        r["saved_path"] = None
        r["target_mb"] = float(target_mb)
        if r["status"] != "nofit":
            r["saved_path"] = self._save(entry["path"], r["temp_output"], float(target_mb))
        entry["result"] = r
        return r

    def save_anyway(self, fid):
        entry = self._files.get(fid)
        if not entry or not entry["result"]:
            return None
        r = entry["result"]
        r["saved_path"] = self._save(entry["path"], r["temp_output"], r["target_mb"])
        return r["saved_path"]

    def _save(self, original, temp, target_mb):
        dest = engine.output_name(original, target_mb)
        try:
            shutil.copyfile(temp, dest)
        except OSError:
            # Original folder not writable (e.g. a read-only share): fall back to Documents\Pressroom.
            folder = os.path.join(os.path.expanduser("~"), "Documents", "Pressroom")
            os.makedirs(folder, exist_ok=True)
            dest = engine.output_name(os.path.join(folder, os.path.basename(original)), target_mb)
            shutil.copyfile(temp, dest)
        return dest

    def render(self, fid, which, page, width):
        entry = self._files.get(fid)
        if not entry:
            return None
        path = entry["path"]
        if which == "after" and entry["result"]:
            path = entry["result"]["temp_output"]
        png = engine.render_page(path, int(page), int(width))
        return "data:image/png;base64," + base64.b64encode(png).decode()

    def show_in_folder(self, path):
        if path and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return True

    # ------------------------------------------------------------ helpers
    def _js(self, code):
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass


def main():
    api = Api()
    window = webview.create_window("Pressroom", url=resource("ui", "index.html"), js_api=api,
                                   width=1360, height=900, min_size=(900, 640), background_color="#05080F")
    api._window = window

    def bind():
        window.dom.document.events.drop += DOMEventHandler(api.on_drop, True, True)

    storage = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Pressroom", "webview")
    webview.start(bind, private_mode=False, storage_path=storage, debug=bool(os.environ.get("PRESSROOM_DEBUG")))


if __name__ == "__main__":
    main()

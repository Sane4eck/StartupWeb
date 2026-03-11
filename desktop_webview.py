from __future__ import annotations

import threading
import time

import uvicorn
import webview


HOST = "127.0.0.1"
PORT = 8000


def _run_server() -> None:
    uvicorn.run("web_app:app", host=HOST, port=PORT, reload=False, log_level="warning")


if __name__ == "__main__":
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    time.sleep(1.0)
    webview.create_window("StartUp Web", f"http://{HOST}:{PORT}", width=1450, height=980)
    webview.start()

"""Drive the web dashboard with headless Chrome over CDP and check what renders.

Not part of the test suite. Needs the `web` extra (it uses `websockets`) and a
Chrome binary; talks to a server such as `scripts/web_smoke_server.py`.

    uv run --extra web python scripts/web_smoke_check.py --url http://127.0.0.1:8919 --pin 123456

Checks: login (skipped with --no-auth) -> front page lists the repo and
container -> repo page lists the fixture sessions -> session view renders the
transcript. Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any

from websockets.sync.client import connect

SESSION = "11111111-1111-4111-8111-111111111111"


class Page:
    def __init__(self, ws_url: str) -> None:
        self._connection = connect(ws_url, max_size=None)
        self._ws = self._connection.__enter__()
        self._id = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv(timeout=15))
            if message.get("id") == self._id:
                return message.get("result", {})

    def eval(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def text(self) -> str:
        return str(self.eval("document.body.innerText") or "")

    def wait_for(self, needle: str, timeout: float = 15.0) -> str:
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = self.text()
            if needle in text:
                return text
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {needle!r}; page says: {text[:300]!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8919")
    parser.add_argument("--pin", default="123456")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--chrome", default="/opt/google/chrome/chrome")
    parser.add_argument("--debug-port", type=int, default=9229)
    parser.add_argument("--screenshots", default="", help="directory for PNG screenshots")
    args = parser.parse_args()

    profile = tempfile.mkdtemp(prefix="jb-smoke-chrome-")
    chrome = subprocess.Popen(
        [
            args.chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={args.debug_port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []

    def check(name: str, step: Any) -> None:
        try:
            step()
            print(f"ok    {name}")
        except Exception as exc:
            failures.append(name)
            print(f"FAIL  {name}: {exc}")

    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.debug_port}/json") as r:
                    targets = [t for t in json.load(r) if t["type"] == "page"]
                if targets:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            print("FAIL  chrome did not start")
            return 1
        page = Page(targets[0]["webSocketDebuggerUrl"])
        page.call("Page.enable")

        def shot(name: str) -> None:
            if args.screenshots:
                import base64
                import pathlib

                data = page.call("Page.captureScreenshot", format="png")["data"]
                pathlib.Path(args.screenshots, f"{name}.png").write_bytes(base64.b64decode(data))

        def login() -> None:
            page.call("Page.navigate", url=args.url + "/")
            if args.no_auth:
                page.wait_for("JailBee")
                return
            page.wait_for("PIN")
            page.eval(
                "(()=>{const fill=(n,v)=>{const i=document.querySelector(`input[name=${n}]`);"
                "i.value=v;i.dispatchEvent(new Event('input',{bubbles:true}));};"
                f"fill('device','smoke test');fill('pin',{json.dumps(args.pin)});}})()"
            )
            time.sleep(0.5)  # let Vue enable the submit button
            page.eval("document.querySelector('button[type=submit]').click()")
            page.wait_for("Log out")

        def front() -> None:
            text = page.wait_for("main")  # the container row; the repo prefix is stripped
            assert "work" in text
            shot("front")

        def repo() -> None:
            page.eval("location.hash = '#/repos/work'")
            page.wait_for("Fix the login bug")
            shot("repo")

        def session() -> None:
            page.eval(f"location.hash = '#/repos/work/sessions/claude/{SESSION}'")
            page.wait_for("Fix the login bug")
            shot("session")

        check("login", login)
        check("front page lists repo and container", front)
        check("repo page lists sessions", repo)
        check("session view renders the transcript", session)
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

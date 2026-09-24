"""Patch downloader/app/services/notifier.py: after each Jellyfin refresh ping,
wait for the library scan to finish, then run the alt-title pass (debounced).
Usage: python3 patch_notifier.py path/to/notifier.py   (idempotent)"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "alt_titles" in s:
    sys.exit("already patched")

start = s.index("class JellyfinNotifier:")
end = s.index("def build_notifiers(")

NEW_CLASS = '''class JellyfinNotifier:
    # One alias worker for the whole process (all JellyfinNotifier instances
    # point at the same server in practice). Debounced: a burst of finished
    # downloads -> one pass after the scan they triggered has finished.
    _alt_lock = threading.Lock()
    _alt_pending = False
    _alt_running = False

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict:
        return {
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Content-Type": "application/json",
        }

    def refresh(self) -> None:
        req = urllib.request.Request(
            f"{self.url}/Library/Refresh",
            method="POST",
            headers=self._headers(),
            data=json.dumps({}).encode(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"refresh returned HTTP {resp.status}")
        if ALT_TITLES_ENABLED:
            self._schedule_alt_titles()

    # -- alternative titles (English / romaji / Japanese) ------------------- #

    def _schedule_alt_titles(self) -> None:
        cls = JellyfinNotifier
        with cls._alt_lock:
            cls._alt_pending = True
            if cls._alt_running:
                return  # the running worker will do one more pass
            cls._alt_running = True
        threading.Thread(target=self._alt_worker, name="alt-titles", daemon=True).start()

    def _alt_worker(self) -> None:
        cls = JellyfinNotifier
        while True:
            with cls._alt_lock:
                if not cls._alt_pending:
                    cls._alt_running = False
                    return
                cls._alt_pending = False
            try:
                time.sleep(ALT_TITLES_START_DELAY)  # let the scan task start
                self._wait_for_library_scan()
                changed, _ = alt_titles.run(
                    self.url, self.api_key, only_new=True, log_fn=log.info
                )
                if changed:
                    log.info("alt titles: added aliases to %d new title(s)", changed)
            except Exception:  # best-effort, never affects downloads
                log.warning("alt-title pass failed", exc_info=True)

    def _wait_for_library_scan(self, timeout: float = 45 * 60) -> None:
        """Block until Jellyfin's 'Scan Media Library' task is idle."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    f"{self.url}/ScheduledTasks?isHidden=false", headers=self._headers()
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    tasks = json.loads(resp.read())
                scan = next((t for t in tasks if t.get("Key") == "RefreshLibrary"), None)
                if not scan or scan.get("State") == "Idle":
                    time.sleep(ALT_TITLES_SETTLE)  # metadata writes settle
                    return
            except Exception:
                log.debug("scan-state poll failed", exc_info=True)
            time.sleep(15)
        log.warning("library scan still running after %ds; running alt titles anyway", timeout)


'''

s = s[:start] + NEW_CLASS + s[end:]

# imports + settings
s = s.replace("import os\n", "import os\nimport threading\nimport time\n", 1)
s = s.replace(
    "from typing import Protocol\n",
    "from typing import Protocol\n\nfrom . import alt_titles\n",
    1,
)
s = s.replace(
    'log = logging.getLogger("notifier")\n',
    'log = logging.getLogger("notifier")\n\n'
    "# After a refresh ping, add English/romaji/Japanese aliases to new anime so\n"
    "# Jellyfin search finds them by any name (see services/alt_titles.py).\n"
    'ALT_TITLES_ENABLED = os.environ.get("ALT_TITLES", "1").lower() not in ("0", "false", "no", "off")\n'
    'ALT_TITLES_START_DELAY = float(os.environ.get("ALT_TITLES_START_DELAY", "20"))\n'
    'ALT_TITLES_SETTLE = float(os.environ.get("ALT_TITLES_SETTLE", "15"))\n',
    1,
)
open(p, "w", encoding="utf-8").write(s)
print("patched", p)

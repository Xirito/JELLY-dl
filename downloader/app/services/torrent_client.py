"""TorrentClientManager — owns a headless qBittorrent process on demand.

Not a Downloader plugin (see plugins/nyaa_tor_plugin.py, the only thing
that imports this module). Searching a torrent indexer and actually
downloading a torrent are different problems: yt-dlp and ani-cli both do a
single fetch and are done, but a torrent is a peer-to-peer transfer that
takes real wall-clock time and needs a real BitTorrent client running
alongside it. Starting/stopping that client is orchestration, not a
per-backend concern — which is why it lives in services/ next to
download_service.py rather than under plugins/, and why the plugin itself
stays a thin adapter around this file.

Lifecycle: qbittorrent-nox is started the first time a torrent job needs
it (acquire()) and stopped the moment nothing needs it any more
(release(), ref-counted so N concurrent torrent jobs share one instance
and it only goes down once the last of them finishes). There's no idle
qBittorrent process sitting around between downloads.

Leech-only, three layers deep so there's no meaningful seeding window:
  1. Every torrent is added with ratio_limit=0 + share_limit_action="Stop"
     — qBittorrent's own share-limit enforcement stops the torrent itself
     the moment its next ratio check runs, which lands right as the
     download completes (0 bytes uploaded / >0 downloaded already
     satisfies a ratio>=0 threshold).
  2. upload_limit is capped low (_UPLOAD_LIMIT_BPS, currently 50 KiB/s) for
     the torrent's whole lifetime, so even the brief window before (1)
     fires can't push much real data out.
     THIS MUST STAY NON-TRIVIALLY ABOVE ZERO. It was originally set to 1
     (effectively 0 B/s) and that broke downloading entirely, not just
     seeding: on this qBittorrent/libtorrent version, the upload-rate
     limiter throttles the *entire* outbound side of the wire protocol on
     a connection, not just piece uploads — interested/request messages,
     keep-alives, and metadata-exchange traffic for magnet links all ride
     the same outbound budget. Capped at ~0 B/s, peers would complete a
     TCP handshake and then the connection just sat there forever: stuck
     at state="metaDL", seeders=0, 0 B/s, even with DHT/UPnP/trackers all
     healthy and the swarm itself fine (confirmed live: lifting one stuck
     job's limit to unlimited took it from 0 seeders to 19 and 700+ KB/s
     within ~10 seconds). 50 KiB/s is enough headroom for protocol
     control traffic to never starve while still being a light leecher,
     not a real seeder.
  3. Our own poll loop explicitly calls torrents_stop() the instant it
     observes the download finish, rather than trusting either of the
     above alone — then the torrent is removed from qBittorrent entirely
     (files kept) once we're done with it, so there's nothing left in the
     client that could ever resume seeding later.

The WebUI is bound to 127.0.0.1 only and never published in docker-compose
— nothing outside this container's own process can reach it, and nothing
outside this file ever needs to.

Connectivity: unlike the WebUI, the actual BitTorrent peer port (_TORRENT_PORT,
below) *does* need to be reachable from the internet for good performance —
without it, this client can only dial out, never be dialed in, and ends up
connecting to a much smaller and worse-behaved slice of a swarm (in
practice: plenty of leechers, but rarely any of the well-seeded ones). To
get that:
  1. In docker-compose.yml, publish _TORRENT_PORT for both protocols, e.g.
     `ports: ["45123:45123/tcp", "45123:45123/udp"]` (alongside the
     existing 8790 mapping for the web app itself).
  2. Forward that same port to this box on your router (or, simpler if
     your setup allows it: run this container with `network_mode: host`
     instead of the default bridge network — then qBittorrent's own UPnP
     client, enabled below, can usually punch the hole itself with zero
     manual router config, since it can see the box's real LAN IP).
Neither of these is something this app can do from inside the container —
they're infrastructure, not code — so they're not automatic; this module
only pins the port number and turns on every discovery mechanism
(UPnP/DHT/PeX/LSD) it can, so whichever path you take actually has
something to grab onto.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import qbittorrentapi

from ..models import DownloadProgress

log = logging.getLogger("torrent_client")

_WEBUI_PORT = 18080
# BitTorrent peer port — must be published from Docker (both TCP and UDP)
# and ideally forwarded on the router for good connectivity. See the
# module docstring and _apply_network_prefs() below.
_TORRENT_PORT = 45123
# Leech-only upload cap — see the "THIS MUST STAY NON-TRIVIALLY ABOVE
# ZERO" note in the module docstring before ever lowering this again.
_UPLOAD_LIMIT_BPS = 51_200  # 50 KiB/s
_HOST = "127.0.0.1"
_STARTUP_TIMEOUT_S = 30
_ADD_TIMEOUT_S = 20
_POLL_INTERVAL_S = 2
_LOG_HEARTBEAT_S = 15  # re-log an unchanged poll state at most this often
_NO_ETA = 8_640_000  # qBittorrent's sentinel for "unknown/infinite" eta

# *UP states mean "finished downloading, now (or about to be) uploading" —
# see qBittorrent's TorrentState enum. "uploading" itself is the plain-
# English alias several API versions use for the same thing.
_DONE_STATES = {
    "uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP",
    "forcedUP", "checkingUP",
}
_ERROR_STATES = {"error", "missingFiles"}


class TorrentClientError(RuntimeError):
    pass


class _StartCancelled(Exception):
    """Internal only — signals that acquire() was cancelled while
    qbittorrent-nox was still starting up. Never escapes this module; see
    download()."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pbkdf2_value(password: str) -> str:
    # qBittorrent's own WebUI\Password_PBKDF2 format: PBKDF2-HMAC-SHA512,
    # 100000 iterations, 64-byte key, random 16-byte salt, "salt:hash" both
    # base64'd inside a literal @ByteArray(...) wrapper (Qt's QSettings
    # serialization of a QByteArray) — this is exactly what qBittorrent's
    # own WebUI writes when you set a password by hand through it, just
    # computed here instead so the container never generates a random
    # temporary password we'd have to scrape off stdout.
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100_000, dklen=64)
    return f"@ByteArray({_b64(salt)}:{_b64(derived)})"


class TorrentClientManager:
    def __init__(self, profile_dir: Path, webui_port: int = _WEBUI_PORT):
        self.profile_dir = profile_dir
        self.port = webui_port
        self._username = "jelly-dl"
        self._password = secrets.token_urlsafe(24)  # overwritten by
                                                       # _ensure_conf() below
                                                       # if a prior run's
                                                       # secret already
                                                       # exists — see there

        self._proc: subprocess.Popen | None = None
        self._client: qbittorrentapi.Client | None = None
        self._active = 0  # ref count — how many torrent jobs need the client up
        self._lock = threading.RLock()

    # -- bootstrap -----------------------------------------------------
    def _ensure_conf(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        secret_path = self.profile_dir / "webui-secret"
        conf_path = self.profile_dir / "qBittorrent" / "config" / "qBittorrent.conf"

        # qBittorrent only ever reads WebUI settings as "WebUI\<Key>" lines
        # under the "[Preferences]" section of qBittorrent.conf — an
        # earlier version of this file wrote them under a bare "[WebUI]"
        # section instead, which qBittorrent silently ignores entirely.
        # That meant every deploy of that version had NO working seeded
        # login at all: qBittorrent fell back to its own random first-run
        # password (never captured, since stdout is discarded), so every
        # auth_log_in() attempt below failed, every time, forever — the
        # torrent backend never actually worked. Detect that broken shape
        # here and self-heal it, rather than requiring anyone to manually
        # wipe the profile directory to pick up the fix.
        needs_bootstrap = True
        if conf_path.exists():
            existing = conf_path.read_text(errors="ignore")
            needs_bootstrap = "WebUI\\Password_PBKDF2" not in existing

        # The WebUI login password has to stay the same across container
        # restarts (a fresh TorrentClientManager picks a new random one in
        # __init__ every time), but the *hash* qBittorrent checks it
        # against only ever gets written into qBittorrent.conf once, on
        # its very first launch. So the plaintext value itself is the
        # thing that has to persist — in this small sidecar file, kept
        # separate from qBittorrent's own conf so we're never racing it
        # to write the same file. As long as this always resolves to the
        # same password qBittorrent's conf was originally seeded with,
        # login keeps working forever without ever touching that conf
        # again.
        if secret_path.exists() and not needs_bootstrap:
            self._password = secret_path.read_text().strip()
        else:
            secret_path.write_text(self._password)
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass  # best-effort; wrong permissions here isn't fatal

        if not needs_bootstrap:
            return  # already correctly bootstrapped — its password hash
                     # already matches self._password via the secret file
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(
            "[Preferences]\n"
            f"WebUI\\Username={self._username}\n"
            f'WebUI\\Password_PBKDF2="{_pbkdf2_value(self._password)}"\n'
            "WebUI\\Address=127.0.0.1\n"
            "WebUI\\HostHeaderValidation=false\n"
            # Our own client only ever connects from 127.0.0.1 anyway (the
            # WebUI is never published) — bypassing auth for localhost is
            # a second, independent path to a working login even if the
            # PBKDF2 hash above is ever wrong again, and it sidesteps
            # qBittorrent's failed-login IP ban entirely.
            "WebUI\\LocalHostAuth=false\n"
        )

    def _wait_ready(self, should_cancel: Callable[[], bool] | None = None) -> None:
        started = time.monotonic()
        deadline = started + _STARTUP_TIMEOUT_S
        last_err: Exception | None = None
        attempt = 0
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                log.info("qbittorrent-nox startup: cancelled after %d login attempt(s)", attempt)
                raise _StartCancelled()
            if self._proc.poll() is not None:
                log.error("qbittorrent-nox exited during startup (code %s)", self._proc.returncode)
                raise TorrentClientError(
                    f"qbittorrent-nox exited during startup (code {self._proc.returncode})"
                )
            try:
                client = qbittorrentapi.Client(
                    host=_HOST, port=self.port,
                    username=self._username, password=self._password,
                )
                client.auth_log_in()
                self._client = client
                log.info(
                    "qbittorrent-nox ready on port %s after %.1fs (%d login attempt(s))",
                    self.port, time.monotonic() - started, attempt + 1,
                )
                self._apply_network_prefs(client)
                return
            except Exception as e:  # noqa: BLE001 — still booting, keep polling
                attempt += 1
                if isinstance(e, qbittorrentapi.Forbidden403Error):
                    # Not "still booting" — either a wrong/unread WebUI
                    # password or qBittorrent's own failed-login IP ban.
                    # Worth flagging loudly since it usually means the
                    # seeded credentials silently didn't take.
                    log.warning(
                        "qbittorrent-nox login attempt %d: 403 Forbidden (banned or "
                        "credentials not applied) — %s", attempt, e,
                    )
                elif attempt == 1 or attempt % 10 == 0:
                    log.debug("qbittorrent-nox login attempt %d still failing: %s", attempt, e)
                last_err = e
                time.sleep(0.5)
        log.error("qbittorrent-nox never became ready after %ss: %s", _STARTUP_TIMEOUT_S, last_err)
        raise TorrentClientError(f"qbittorrent-nox never became ready: {last_err}")

    @staticmethod
    def _apply_network_prefs(client: qbittorrentapi.Client) -> None:
        # We never configured a BitTorrent peer port at all before this —
        # qBittorrent defaulted to picking a random one on every launch,
        # which Docker never published and no router ever forwarded.
        # seeds=0 (never connects to any of the swarm's seeders) with
        # leechs fluctuating, stuck in "metaDL" forever, is the classic
        # symptom of a torrent client with no reachable inbound port: it
        # can still dial *out*, but only the (usually worse-connected)
        # peers that happen to dial *it* first ever complete a connection,
        # and it's effectively invisible to everyone else, including
        # well-behaved seeders.
        #
        # Fixing this at the network level (Docker port publish + router
        # forward, or host networking) is outside this repo — see the
        # module docstring — but this is the qBittorrent-side half:
        # pin a fixed, known port instead of a random one (so it CAN be
        # forwarded), and make sure UPnP/DHT/PeX/LSD are all on so it has
        # every possible avenue to actually be reachable. Best-effort and
        # non-fatal: a login that succeeds but can't set preferences still
        # means a working (if possibly still poorly-connected) client.
        try:
            client.app_set_preferences({
                "listen_port": _TORRENT_PORT,
                "random_port": False,
                "upnp": True,
                "dht": True,
                "pex": True,
                "lsd": True,
            })
            log.info("qbittorrent-nox: pinned BitTorrent listen port to %s (UPnP/DHT/PeX/LSD on)",
                      _TORRENT_PORT)
        except Exception as e:  # noqa: BLE001
            log.warning("qbittorrent-nox: could not set network preferences: %s", e)

    def _start_locked(self, should_cancel: Callable[[], bool] | None = None) -> None:
        if self._proc is not None:
            return
        binary = shutil.which("qbittorrent-nox")
        if not binary:
            log.error("qbittorrent-nox binary not found on PATH")
            raise TorrentClientError(
                "qbittorrent-nox is not installed in this image — "
                "the torrent backend can't run without it"
            )
        self._ensure_conf()
        env = dict(os.environ)
        env["HOME"] = str(self.profile_dir)
        log.info("starting qbittorrent-nox (profile=%s, port=%s)", self.profile_dir, self.port)
        self._proc = subprocess.Popen(
            [
                binary,
                "--confirm-legal-notice",
                f"--webui-port={self.port}",
                f"--profile={self.profile_dir}",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        try:
            self._wait_ready(should_cancel)
        except Exception:
            log.warning("qbittorrent-nox startup failed — tearing it back down")
            self._kill_locked()
            raise

    def _kill_locked(self) -> None:
        self._client = None
        proc, self._proc = self._proc, None
        if proc is None:
            return
        log.info("stopping qbittorrent-nox (pid=%s)", proc.pid)
        proc.terminate()  # SIGTERM — lets it flush fastresume data; never kill() first
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("qbittorrent-nox didn't exit within 10s of SIGTERM — killing it")
            proc.kill()
            proc.wait(timeout=5)

    # -- ref-counted start/stop -----------------------------------------
    def acquire(self, should_cancel: Callable[[], bool] | None = None) -> qbittorrentapi.Client:
        """Call once per torrent job, before adding anything. Starts the
        client on the first caller; every later caller shares that same
        instance until the last of them releases it. should_cancel is
        polled during the (up to _STARTUP_TIMEOUT_S) startup wait so a
        Cancel click isn't stuck behind a slow qbittorrent-nox boot —
        raises _StartCancelled rather than blocking to the full timeout."""
        with self._lock:
            self._start_locked(should_cancel)
            self._active += 1
            return self._client

    def release(self) -> None:
        """Call exactly once per acquire() — from a finally block, so it
        always runs whether the job succeeded, errored, or was cancelled.
        Shuts qBittorrent down the moment nothing else needs it."""
        with self._lock:
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self._kill_locked()

    # -- the actual download --------------------------------------------
    def download(
        self,
        magnet: str,
        destination: Path,
        job_tag: str,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[str | None, str | None]:
        """Returns (filepath, error) — deliberately not the app's
        DownloadResult DTO, so this file's only dependency on the rest of
        the app is DownloadProgress. The plugin wraps this into
        DownloadResult itself."""
        destination.mkdir(parents=True, exist_ok=True)
        log.info("torrent job %s: queued — magnet=%.80s…", job_tag, magnet)

        # Deliberately separate from the try/finally below: release() must
        # only ever run once acquire() has actually succeeded (incrementing
        # _active) — calling it after a failed acquire would either
        # double-release someone else's still-active session or tear down
        # a client that was never actually acquired. Previously this call
        # sat outside any try/except at all, so a startup failure here
        # (qbittorrent-nox never becoming ready, wrong credentials, missing
        # binary) raised straight out of download() uncaught — DownloadService
        # runs this in a bare daemon thread with nothing catching that, so
        # the thread just died silently and the job stayed stuck at
        # "running"/"cancelling" forever with no way to cancel it.
        try:
            client = self.acquire(should_cancel)
        except _StartCancelled:
            log.info("torrent job %s: cancelled while qbittorrent-nox was starting", job_tag)
            return None, "cancelled by user"
        except TorrentClientError as e:
            log.error("torrent job %s: qbittorrent-nox failed to start: %s", job_tag, e)
            return None, str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("torrent job %s: unexpected error acquiring the torrent client", job_tag)
            return None, str(e)[:2000]

        torrent_hash: str | None = None
        try:
            add_result = client.torrents_add(
                urls=magnet,
                save_path=str(destination),
                use_auto_torrent_management=False,
                is_sequential_download=True,
                tags=[job_tag],
                ratio_limit=0,
                share_limit_action="Stop",
                upload_limit=_UPLOAD_LIMIT_BPS,  # leech only, see module docstring
            )
            log.info("torrent job %s: qBittorrent add response=%r", job_tag, add_result)
            # Older qBittorrent (pre Web API v2.14.0 — e.g. the 4.5.x this
            # ships against) answers /torrents/add with a literal "Ok."/
            # "Fails." body rather than raising an HTTP error on failure
            # (bad magnet, disk full, etc). Catch that immediately instead
            # of silently waiting out the full _await_added() timeout below
            # for a torrent that was never actually added.
            if isinstance(add_result, str) and add_result.strip().rstrip(".").lower() == "fails":
                raise TorrentClientError(
                    "qBittorrent rejected the magnet link (add failed)"
                )
            torrent_hash = self._await_added(client, job_tag, should_cancel)
            if torrent_hash is None:
                return None, "cancelled by user"
            filepath, error = self._await_complete(client, torrent_hash, on_progress, should_cancel)
            if error != "cancelled by user":
                # A cancelled torrent's partial files are already deleted
                # inside _await_complete. Anything else (success or a real
                # error) just gets forgotten: removed from qBittorrent's
                # own list — so it can never resume seeding later — while
                # whatever it downloaded stays on disk.
                self._forget(client, torrent_hash, delete_files=False)
                torrent_hash = None
            if error:
                log.warning("torrent job %s: finished with error: %s", job_tag, error)
            else:
                log.info("torrent job %s: finished OK -> %s", job_tag, filepath)
            return filepath, error
        except TorrentClientError as e:
            log.error("torrent job %s: %s", job_tag, e)
            return None, str(e)
        except Exception as e:  # surfaced to the job store, mirrors the other plugins
            log.exception("torrent job %s: unexpected error", job_tag)
            return None, str(e)[:2000]
        finally:
            self.release()

    def _await_added(
        self, client: qbittorrentapi.Client, job_tag: str,
        should_cancel: Callable[[], bool] | None,
    ) -> str | None:
        # torrents_add() doesn't hand back the new torrent's hash directly
        # (magnet-only adds especially) — tagging it with a fresh per-job
        # tag and polling for that tag to appear is the standard,
        # version-independent way to find it again.
        deadline = time.monotonic() + _ADD_TIMEOUT_S
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                log.info("torrent add (%s): cancelled before qBittorrent registered it", job_tag)
                return None
            info = client.torrents_info(tag=job_tag)
            if info:
                log.info("torrent add (%s): registered as hash=%s", job_tag, info[0].hash[:12])
                return info[0].hash
            time.sleep(0.5)
        log.error("torrent add (%s): qBittorrent never registered the torrent (%ss timeout)",
                   job_tag, _ADD_TIMEOUT_S)
        raise TorrentClientError(
            "qBittorrent never registered the new torrent (magnet may be invalid or dead)"
        )

    def _await_complete(
        self, client: qbittorrentapi.Client, torrent_hash: str,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool] | None,
    ) -> tuple[str | None, str | None]:
        short_hash = torrent_hash[:12]
        last_logged_state: str | None = None
        last_heartbeat = 0.0
        while True:
            if should_cancel and should_cancel():
                log.info("torrent %s: cancelled by user, removing", short_hash)
                self._forget(client, torrent_hash, delete_files=True)
                return None, "cancelled by user"

            info = client.torrents_info(torrent_hashes=torrent_hash)
            if not info:
                log.warning("torrent %s: disappeared from qBittorrent", short_hash)
                return None, "torrent disappeared from qBittorrent"
            t = info[0]
            state = str(t.state)
            seeders = int(t.num_seeds) if t.num_seeds is not None else None
            leechers = int(t.num_leechs) if t.num_leechs is not None else None
            progress = float(t.progress or 0)

            # Log every state change immediately (the interesting bit for
            # a stuck/no-progress job — e.g. stuck in "metaDL" means it
            # never got the torrent's metadata, "stalledDL" means zero
            # useful peers), and otherwise re-log a heartbeat at most every
            # _LOG_HEARTBEAT_S so an unchanged-but-genuinely-still-running
            # download doesn't go completely silent in the logs.
            now = time.monotonic()
            if state != last_logged_state or now - last_heartbeat > _LOG_HEARTBEAT_S:
                log.info(
                    "torrent %s: state=%s seeds=%s leechs=%s progress=%.1f%% dl_speed=%sB/s",
                    short_hash, state, seeders, leechers, progress * 100, int(t.dlspeed or 0),
                )
                last_logged_state = state
                last_heartbeat = now

            if state in _ERROR_STATES:
                log.error("torrent %s: qBittorrent reported an error state: %s", short_hash, state)
                return None, f"qBittorrent reported an error (state: {state})"

            eta = t.eta if t.eta is not None and 0 <= t.eta < _NO_ETA else None
            on_progress(DownloadProgress(
                status="downloading",
                downloaded_bytes=int(t.downloaded or 0),
                total_bytes=int(t.size) if t.size and t.size > 0 else None,
                speed_bps=float(t.dlspeed) if t.dlspeed else None,
                eta_s=float(eta) if eta is not None else None,
                filename=t.name or None,
                percent=round(progress * 100, 1),
                seeders=seeders,
                leechers=leechers,
                state=state,
            ))

            if state in _DONE_STATES or progress >= 1.0:
                log.info("torrent %s: download complete (state=%s)", short_hash, state)
                try:
                    # Defense-in-depth stop — see module docstring. The
                    # ratio_limit=0 share-limit action should already have
                    # (or be about to) stop it on its own; this makes sure
                    # regardless of exactly when that check next runs.
                    client.torrents_stop(torrent_hashes=torrent_hash)
                except Exception:
                    pass
                return (t.content_path or t.save_path or None), None

            time.sleep(_POLL_INTERVAL_S)

    @staticmethod
    def _forget(client: qbittorrentapi.Client, torrent_hash: str, delete_files: bool) -> None:
        try:
            client.torrents_delete(torrent_hashes=torrent_hash, delete_files=delete_files)
        except Exception:
            pass  # best-effort cleanup — a job that already finished/failed
                   # shouldn't fail *again* just because this housekeeping
                   # step didn't land

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.config import DOWNLOAD_DIR, TMP_DIR, settings
from app.services.video.process import run

logger = logging.getLogger(__name__)


def _cookies_file() -> str:
    return settings.YOUTUBE_COOKIES_FILE or ""


def _runtime_version(name: str, path: str) -> tuple[int, ...] | None:
    """Return version tuple for a JS runtime binary, or None if unrunnable.

    yt-dlp silently reports "No supported JavaScript runtime" when the binary
    exists but is too old (Colab/Kaggle/Debian apt nodejs is often v18;
    yt-dlp needs node>=22 / deno>=2 in 2026 builds) or not executable
    (broken /tools/node symlink). Checking here lets us log the real cause
    instead of passing a dead --js-runtimes path.
    """
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
        raw = ((out.stdout or "") + " " + (out.stderr or "")).strip()
        import re

        m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw)
        if not m:
            logger.warning("JS runtime %s at %s gave unreadable version: %r", name, path, raw[:100])
            return None
        return tuple(int(g) for g in m.groups() if g is not None)
    except Exception as e:
        logger.warning("JS runtime %s at %s not runnable (%s), skipping", name, path, e)
        return None


def _js_runtime_args() -> list[str]:
    """Explicitly enable any installed, *working* JS runtime for yt-dlp.

    yt-dlp enables *only* deno by default — a box with node but no deno
    (typical Colab/Kaggle/Docker) still reports "No supported JavaScript
    runtime", which since late-2025 breaks YouTube entirely (SABR-only
    streaming + HTTP 403 + "Only images are available"), not just DASH merges.

    Two traps fixed here:
    1. Dead path: old deploys passed hardcoded ``node:/tools/node/bin/node``
       (Kaggle layout) missing on Colab — we validate existence.
    2. Too-old node: apt/debian nodejs is v18-v20 but yt-dlp 2026 builds need
       node>=22 (deno>=2). An old binary exists yet yt-dlp still warns "No
       supported runtime" — we check ``--version`` and skip unsupported ones
       with an actionable log instead of passing a useless flag.
    """
    candidates: list[tuple[str, str | None]] = [
        ("deno", shutil.which("deno")),
        ("node", shutil.which("node") or shutil.which("nodejs")),
        ("bun", shutil.which("bun")),
        ("quickjs", shutil.which("qjs") or shutil.which("quickjs")),
    ]
    # Minimums per https://github.com/yt-dlp/yt-dlp/wiki/EJS (2026 builds
    # enforce node>=22 in practice; deno>=2.0).
    minimums: dict[str, tuple[int, ...]] = {
        "deno": (2,),
        "node": (22,),
        "bun": (1,),
        "quickjs": (0,),
    }
    args: list[str] = []
    found: list[str] = []
    for name, path in candidates:
        if not path:
            continue
        if not Path(path).exists():
            logger.warning("JS runtime %s reported at %s but path missing, skipping", name, path)
            continue
        ver = _runtime_version(name, path)
        if ver is None:
            continue
        need = minimums.get(name, (0,))
        if ver < need:
            logger.warning(
                "JS runtime %s at %s is v%s — too old for yt-dlp "
                "(need %s+). Install deno (recommended, enabled by default) or "
                "Node.js 22+: Colab/Kaggle re-run the system-deps cell, "
                "Docker rebuilds with deno, Windows install Node.js LTS. "
                "See https://github.com/yt-dlp/yt-dlp/wiki/EJS",
                name, path, ".".join(map(str, ver)), ".".join(map(str, need)),
            )
            continue
        # Explicit PATH survives nvm/venv layouts (e.g. node under
        # ~/.nvm) where the binary isn't on yt-dlp's minimal PATH.
        args += ["--js-runtimes", f"{name}:{path}"]
        found.append(f"{name}:{path} (v{'.'.join(map(str, ver))})")
    if found:
        logger.debug("JS runtimes for yt-dlp: %s", ", ".join(found))
    else:
        logger.warning(
            "No working JS runtime found (need deno>=2 or node>=22) — YouTube "
            "downloads will fail with SABR/403 errors. Install deno "
            "(recommended): 'curl -fsSL https://deno.land/install.sh | sh' or "
            "'apt-get install -y nodejs' only if it gives node>=22. "
            "See https://github.com/yt-dlp/yt-dlp/wiki/EJS"
        )
    return args


def _cookies_args() -> list[str]:
    """Validate YOUTUBE_COOKIES_FILE and return yt-dlp --cookies args.

    A stale/empty path is ignored with a warning (previous code passed it
    blindly). A valid Netscape cookies.txt exported from a logged-in
    browser is currently the only reliable fix for YouTube's
    "Sign in to confirm you're not a bot" on datacenter IPs
    (Colab/Kaggle/Cloud).
    """
    cookies = _cookies_file()
    if not cookies:
        return []
    p = Path(cookies)
    if not p.exists():
        logger.warning("YOUTUBE_COOKIES_FILE=%s not found, ignoring", cookies)
        return []
    try:
        if p.stat().st_size == 0:
            logger.warning("YOUTUBE_COOKIES_FILE=%s is empty, ignoring", cookies)
            return []
        head = p.read_text(encoding="utf-8", errors="ignore")[:2000]
        if "youtube.com" not in head.lower() and "netscape" not in head.lower() and "http cookie" not in head.lower():
            logger.warning(
                "YOUTUBE_COOKIES_FILE=%s doesn't look like a YouTube cookies.txt "
                "(no youtube.com entries) — still passing it to yt-dlp, but re-export "
                "from youtube.com if downloads fail",
                cookies,
            )
    except OSError as e:
        logger.warning("Could not read YOUTUBE_COOKIES_FILE=%s (%s), ignoring", cookies, e)
        return []
    return ["--cookies", str(p)]


def _is_bot_check(text: str) -> bool:
    t = (text or "").lower()
    return (
        "sign in to confirm you" in t and "not a bot" in t
    ) or (
        "not a bot" in t and "sign in" in t
    ) or (
        "http error 429" in t and "youtube" in t
    )


def _is_sabr_block(text: str) -> bool:
    """Detect YouTube's SABR-only / PO-token block (late-2025+ escalation).

    Without a working JS runtime, YouTube returns no https formats (only SABR
    URLs yt-dlp can't fetch): "Some * client https formats have been skipped
    as they are missing a URL ... SABR-only", then "HTTP Error 403",
    "Requested format is not available", "Only images are available".
    This looks like an IP ban but is really a missing-runtime problem.
    """
    t = (text or "").lower()
    return (
        "sabr" in t
        or "missing a url" in t
        or "missing_pot" in t
        or "only images are available" in t
        or "requested format is not available" in t
        or ("http error 403" in t and "youtube" in t)
        or "unable to download video data" in t
    )


def _is_js_missing(text: str) -> bool:
    t = (text or "").lower()
    return (
        "no supported javascript runtime" in t
        or "js runtime" in t
        or "js challenge" in t
        or "n challenge solving failed" in t
        or "signature solving failed" in t
    )


def _cleanup_stale_temps(video_id: str) -> None:
    """Remove interrupted-download leftovers for this video_id.

    Stale .part/.ytdl/.temp/.fXXX files from a killed container combined
    with --continue cause the exact "Unable to rename file: ... .part"
    failure in the report. We always download fresh (see --no-continue
    below), so deleting them is safe — the validated final cache
    <video_id>.mp4 is never touched here. Fragments live in the yt-dlp
    temp dir (-P temp:), so scan both it and DOWNLOAD_DIR.
    """
    dirs = {DOWNLOAD_DIR}
    try:
        dirs.add(Path(tempfile.gettempdir()))
    except Exception:
        pass
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for p in d.glob(f"{video_id}*"):
                if p.is_dir():
                    continue
                name = p.name
                if name == f"{video_id}.mp4":
                    continue
                if (
                    name.endswith((".part", ".ytdl", ".temp", ".tmp", ".info.json"))
                    or name.startswith(f"{video_id}.f")  # e.g. <id>.f137.mp4, <id>.f140.m4a
                ):
                    try:
                        p.unlink()
                        logger.debug("Removed stale download temp %s", p)
                    except OSError:
                        pass
        except OSError as e:
            logger.debug("Stale temp cleanup skipped in %s: %s", d, e)


def download_video(video_id: str) -> Path:
    """Downloads (once) and caches the source video on disk, keyed by video_id."""
    output_path = DOWNLOAD_DIR / f"{video_id}.mp4"
    if output_path.exists():
        try:
            # Corrupt/partial downloads (killed container, 0-byte file) were
            # reused forever — validate size + stream before trusting cache.
            if output_path.stat().st_size > 1024 * 1024:
                get_video_dimensions(output_path)
                logger.info("Using cached download for video_id=%s (%s)", video_id, output_path)
                return output_path
            logger.warning("Cached download %s too small, re-downloading", output_path)
            try:
                output_path.unlink()
            except OSError:
                pass
        except Exception:
            logger.warning("Cached download %s unreadable, re-downloading", output_path)
            try:
                output_path.unlink()
            except OSError:
                pass

    logger.info("Downloading video_id=%s via yt-dlp...", video_id)
    t0 = time.perf_counter()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_temps(video_id)
    url = f"https://www.youtube.com/watch?v={video_id}"

    base_args: list[str] = []
    base_args += _cookies_args()
    base_args += _js_runtime_args()
    base_args += [
        # EJS challenge-solver scripts (yt-dlp 2025.11+): needed alongside the
        # JS runtime to solve YouTube's n/signature challenges. Without this,
        # even a good runtime can report "challenge solving failed".
        "--remote-components", "ejs:github",
        "--merge-output-format", "mp4",
        # Single progressive fallbacks may arrive as webm — remux to mp4 so
        # the cache path below (<video_id>.mp4) always holds.
        "--remux-video", "mp4",
        # Fresh atomic download: resume (.part + --continue) races with
        # killed containers and Drive-FUSE renames, producing
        # "Unable to rename file: ... .f140.m4a.part". --no-part writes
        # directly; --force-overwrites implies --no-continue.
        "--force-overwrites", "--no-continue", "--no-part",
        "--retries", "5",
        "--fragment-retries", "5",
        "--file-access-retries", "3",
        "--concurrent-fragments", "4",
        "--no-playlist",
        "--socket-timeout", "30",
        # NOTE: -P temp: is ignored by yt-dlp when -o is absolute, so keep
        # -o relative and pin dirs explicitly: fragments assemble on local
        # disk even when storage/downloads/ is symlinked to Drive-FUSE
        # (Colab), which mishandles .part renames; only the final mp4 is
        # moved to DOWNLOAD_DIR.
        "-P", f"home:{DOWNLOAD_DIR}",
        "-P", f"temp:{tempfile.gettempdir()}",
    ]
    format_sort = ["--format-sort", "res:1080,fps,vcodec:avc1,acodec:aac"]
    # Primary: 1080p H.264-merge preference with VP9/AV1 fallback.
    # Fallback 2: single progressive file — no DASH merge, works without a
    # JS runtime / on bot-guarded IPs where f137+f140 deciphering fails.
    # Fallbacks 3-4: mobile player clients (android/ios). These use a
    # different YouTube InnerTube client that usually bypasses the
    # "Sign in to confirm you're not a bot" web-client challenge on
    # datacenter IPs (Colab/Kaggle) and needs no JS runtime — quality is
    # capped (~720p) but better than a hard failure.
    attempts = [
        {"label": "dash-merge",
         "args": ["-f", "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/bv*+ba/b"]},
        {"label": "progressive",
         "args": ["-f", "b[height<=1080]/b/best"]},
        {"label": "android-client",
         "args": ["-f", "b/best",
                  "--extractor-args", "youtube:player_client=android",
                  "--format-sort", "res:720"]},
        {"label": "ios-client",
         "args": ["-f", "b/best",
                  "--extractor-args", "youtube:player_client=ios",
                  "--format-sort", "res:720"]},
    ]
    last_err: Exception | None = None
    last_stderr: str = ""
    saw_bot_check = False
    saw_js_missing = False
    saw_sabr = False
    succeeded = False
    for i, attempt in enumerate(attempts):
        fmt_args: list[str] = attempt["args"]
        # format_sort already pins 1080p preference for web clients; mobile
        # fallbacks carry their own res:720 sort — don't append a second one.
        extra_sort = [] if "label" in attempt and attempt["label"] in ("android-client", "ios-client") else format_sort
        command = (
            ["yt-dlp"]
            + base_args
            + fmt_args
            + extra_sort
            # Relative template + -P home: (see above) so temp assembly
            # stays on local disk. Final file: <DOWNLOAD_DIR>/<id>.mp4.
            + ["-o", f"{video_id}.%(ext)s", url]
        )
        try:
            # Long videos on slow links exceed the 600s default.
            run(command, timeout=1800)
            if attempt["label"] in ("android-client", "ios-client"):
                logger.warning(
                    "Downloaded video_id=%s via %s fallback (reduced quality, "
                    "YouTube bot-check bypass) — set YOUTUBE_COOKIES_FILE for full quality",
                    video_id, attempt["label"],
                )
            succeeded = True
        except Exception as e:
            last_err = e
            # process.run() logs head+tail of stderr; also capture it here so
            # the final error can distinguish "bot-check" (needs cookies)
            # from "no JS runtime" (needs nodejs) instead of one generic blob.
            import subprocess as _sp

            err_text = ""
            if isinstance(e, _sp.CalledProcessError):
                try:
                    err_text = ((e.stderr or b"").decode(errors="ignore") or "") + "\n" + ((e.stdout or b"").decode(errors="ignore") or "")
                except Exception:
                    err_text = str(e)
            else:
                err_text = str(e)
            last_stderr += "\n" + err_text[-4000:]
            if _is_bot_check(err_text):
                saw_bot_check = True
            if _is_js_missing(err_text):
                saw_js_missing = True
            if _is_sabr_block(err_text):
                saw_sabr = True
            logger.warning(
                "yt-dlp attempt %d/%d failed for video_id=%s (%s), %s",
                i + 1, len(attempts), video_id, attempt["label"],
                f"retrying with {attempts[i + 1]['label']}"
                if i + 1 < len(attempts) else "no more fallbacks",
            )
            _cleanup_stale_temps(video_id)
            # A half-merged output from the failed attempt must not pass
            # the size check below on the next iteration.
            try:
                if output_path.exists() and output_path.stat().st_size < 1024 * 1024:
                    output_path.unlink()
            except OSError:
                pass
            continue
        break
    if not succeeded:
        tried = "+".join(a["label"] for a in attempts)
        cookies_cfg = _cookies_file() or "(not set)"
        if saw_js_missing or saw_sabr:
            raise RuntimeError(
                f"yt-dlp download failed for video {video_id} (tried {tried}): "
                f"YouTube now requires a working JS runtime (deno>=2 or node>=22) "
                f"plus yt-dlp's EJS solver — without it YouTube returns SABR-only "
                f"streams that fail with HTTP 403 / 'Requested format is not "
                f"available' / 'Only images are available'. Your log shows "
                f"'No supported JavaScript runtime' with "
                f"--js-runtimes node:/tools/node/bin/node: that binary exists but "
                f"is too old (apt nodejs is v18-v20; yt-dlp 2026 builds need "
                f"node>=22) or not executable. Fix: 1) Colab/Kaggle: re-run the "
                f"system-deps cell (it now installs deno + Node 22) and restart "
                f"with the latest repo zip, 2) Docker: rebuild (image now ships "
                f"deno), 3) Windows: install Deno or Node.js 22 LTS, 4) then "
                f"`pip install -U yt-dlp`. If it still 403s after the JS warning "
                f"is gone, add cookies: export cookies.txt from a logged-in "
                f"browser (extension 'Get cookies.txt LOCALES') and set "
                f"YOUTUBE_COOKIES_FILE (now {cookies_cfg}). "
                f"See https://github.com/yt-dlp/yt-dlp/wiki/EJS. Last error: {last_err}"
            ) from last_err
        if saw_bot_check:
            raise RuntimeError(
                f"YouTube blocked the download for video {video_id} "
                f"('Sign in to confirm you're not a bot'). This is YouTube "
                f"rate-limiting datacenter IPs (Colab/Kaggle/cloud) — not a bug "
                f"in your link. Tried: {tried}. "
                f"Fix: 1) In a desktop browser logged into YouTube, export "
                f"cookies.txt (extension 'Get cookies.txt LOCALES'), "
                f"2) Colab: upload it to /content/drive/MyDrive/autoclips-config/cookies.txt "
                f"and set YOUTUBE_COOKIES_FILE to that path in .env, then restart; "
                f"Kaggle: upload to /kaggle/working/cookies.txt and re-run cells 4-8; "
                f"local/Docker: set YOUTUBE_COOKIES_FILE=cookies.txt. "
                f"3) Re-upload the latest repo zip (old Colab code passed a dead "
                f"--js-runtimes node:/tools/node/bin/node path). "
                f"Or upload the MP4 directly to skip YouTube. "
                f"(YOUTUBE_COOKIES_FILE={cookies_cfg}). Last error: {last_err}"
            ) from last_err
        raise RuntimeError(
            f"yt-dlp download failed for video {video_id} "
            f"(tried {tried}). "
            f"On 'Sign in to confirm you're not a bot', set "
            f"YOUTUBE_COOKIES_FILE (YOUTUBE_COOKIES_FILE={cookies_cfg}); "
            f"on SABR/403 or JS-runtime warnings install a working JS runtime "
            f"(deno>=2 or node>=22, see https://github.com/yt-dlp/yt-dlp/wiki/EJS) "
            f"and run `pip install -U yt-dlp`. Last error: {last_err}"
        ) from last_err
    if not output_path.exists():
        # Defensive: --remux-video/--merge-output-format should always
        # yield <id>.mp4, but adopt a same-id media file if one landed
        # with a different container instead of failing outright.
        try:
            alts = sorted(
                p for p in DOWNLOAD_DIR.glob(f"{video_id}.*")
                if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
                and not p.name.endswith((".part", ".temp", ".tmp", ".ytdl"))
            )
        except OSError:
            alts = []
        if alts:
            best = next((p for p in alts if p.suffix.lower() == ".mp4"), alts[0])
            try:
                best.rename(output_path)
                logger.info("Adopted %s as %s", best, output_path)
            except OSError as e:
                raise RuntimeError(
                    f"yt-dlp downloaded {best} but could not move it to "
                    f"{output_path} (video_id={video_id})."
                ) from e
    try:
        size_mb = output_path.stat().st_size / (1024 * 1024)
    except OSError as e:
        raise RuntimeError(
            f"yt-dlp reported success but {output_path} is missing "
            f"(video_id={video_id}). Check disk space and DOWNLOAD_DIR "
            f"writability."
        ) from e
    if size_mb < 1:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"Downloaded file for video {video_id} is only {size_mb:.2f} MB "
            f"— likely a blocked/age-restricted video. Try setting "
            f"YOUTUBE_COOKIES_FILE (see .env.example)."
        )
    logger.info(
        "Download finished for video_id=%s in %.1fs (%.1f MB)",
        video_id, time.perf_counter() - t0, size_mb,
    )
    return output_path


def get_video_credit(video_id: str) -> str:
    """Returns '@handle' credit for the source video (e.g. '@ranveerallahbadia').

    Uses yt-dlp metadata only (no download), cached under storage/tmp/ so one video
    costs one lookup. Returns '' on any failure — callers must tolerate that.
    """
    try:
        cache_path = TMP_DIR / f"{video_id}.credit.txt"
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        url = f"https://www.youtube.com/watch?v={video_id}"
        command = ["yt-dlp", "--skip-download", "--no-warnings",
                   "--print", "%(uploader_id)s||%(channel)s||%(uploader)s||%(channel_url)s"]
        command += _js_runtime_args()
        command += _cookies_args()
        command.append(url)
        out = subprocess.run(command, capture_output=True, text=True, timeout=25)
        raw = (out.stdout or "").strip().splitlines()
        line = raw[-1].strip() if raw else ""
        parts = [p.strip() for p in line.split("||")]
        credit = ""
        for p in parts:
            if p and p.lower() != "na" and p.startswith("@"):
                credit = p
                break
        if not credit:
            # Fallback: build a handle from channel/uploader name.
            for p in parts:
                if p and p.lower() != "na":
                    # channel_url like https://www.youtube.com/@handle → keep handle
                    if "youtube.com/@" in p:
                        credit = "@" + p.split("/@")[-1].split("/")[0].strip()
                        break
            if not credit:
                for p in parts:
                    if p and p.lower() != "na":
                        credit = p if p.startswith("@") else f"@{p.replace(' ', '')}"
                        break
        if credit:
            try:
                cache_path.write_text(credit, encoding="utf-8")
            except OSError:
                pass
            return credit
    except Exception:
        logger.debug("Could not fetch credit for video_id=%s", video_id, exc_info=True)
    return ""


def get_video_dimensions(input_path: Path):
    try:
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(input_path)],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        width, height = map(int, output.split(","))
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid dimensions: {output}")
        return width, height
    except Exception as e:
        # Portrait phone video with a rotate tag reports swapped dims —
        # try honoring rotation before giving up so crop math stays correct.
        try:
            output = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,side_data_list",
                 "-of", "default=noprint_wrappers=1", str(input_path)],
                capture_output=True, text=True, check=True, timeout=15,
            ).stdout
            import re
            wm = re.search(r"width=(\d+)", output)
            hm = re.search(r"height=(\d+)", output)
            if wm and hm:
                w, h = int(wm.group(1)), int(hm.group(1))
                if "rotation" in output.lower() and abs(w - h) > 0:
                    # 90/270 deg rotation swaps display dims.
                    return h, w
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        raise RuntimeError(f"ffprobe dimensions failed for {input_path}: {e}")


def get_video_duration(input_path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(input_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(output)

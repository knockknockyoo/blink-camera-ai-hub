from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


LOGGER = logging.getLogger("blink-camera-ai-hub")


class BlinkDownloader:
    def __init__(
        self,
        auth_file: Path,
        download_dir: Path,
        camera: str = "all",
        download_retries: int = 1,
        download_delay_seconds: float = 5.0,
        retry_backoff_seconds: float = 5.0,
        clip_timeout_seconds: float = 90.0,
        metadata_timeout_seconds: float = 120.0,
        max_clips_per_scan: int = 20,
        progress_callback: Callable[..., None] | None = None,
        downloaded_callback: Callable[[Path], None] | None = None,
    ):
        self.auth_file = auth_file
        self.download_dir = download_dir
        self.camera = camera
        self.download_retries = max(1, download_retries)
        self.download_delay_seconds = max(0.0, download_delay_seconds)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.clip_timeout_seconds = max(1.0, clip_timeout_seconds)
        self.metadata_timeout_seconds = max(1.0, metadata_timeout_seconds)
        self.max_clips_per_scan = max(0, max_clips_per_scan)
        self.progress_callback = progress_callback
        self.downloaded_callback = downloaded_callback
        self.incomplete_downloads = False
        self.backlog_remaining = False

    def _progress(self, **values: Any) -> None:
        if self.progress_callback:
            self.progress_callback(**values)

    def _downloaded(self, path: Path) -> None:
        if self.downloaded_callback:
            self.downloaded_callback(path.resolve())

    @property
    def configured(self) -> bool:
        return self.auth_file.exists()

    async def _download_local_item(self, item, blink, destination: Path) -> bool:
        """Download one Sync Module clip without aborting the whole scan."""
        started_at = time.monotonic()
        partial = destination.with_suffix(f"{destination.suffix}.part")
        for attempt in range(1, self.download_retries + 1):
            attempt_started_at = time.monotonic()
            try:
                async with asyncio.timeout(self.clip_timeout_seconds):
                    prepared = await item.prepare_download(blink)
                    if not prepared:
                        raise RuntimeError(
                            "Blink did not respond to the download preparation command."
                        )
                    partial.unlink(missing_ok=True)
                    await item.download_video(blink, str(partial))
                    if not partial.exists() or partial.stat().st_size == 0:
                        raise RuntimeError("Blink returned an empty video file.")
                    partial.replace(destination)
                    self._downloaded(destination)
                LOGGER.info(
                    "[Download complete] %s size=%d bytes elapsed=%.1fs",
                    destination.name,
                    destination.stat().st_size,
                    time.monotonic() - started_at,
                )
                return True
            except TimeoutError:
                error = (
                    f"Blink video preparation and download exceeded "
                    f"{self.clip_timeout_seconds:g} seconds."
                )
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            destination.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            attempt_elapsed = time.monotonic() - attempt_started_at
            if attempt >= self.download_retries:
                LOGGER.error(
                    "[Download failed] %s: skipped after %d attempts; "
                    "will retry next scan (elapsed=%.1fs, reason=%s)",
                    destination.name,
                    attempt,
                    time.monotonic() - started_at,
                    error,
                )
                return False
            wait_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "[Download retry %d/%d] %s: %s "
                "(attempt elapsed=%.1fs, retrying in %ds)",
                attempt,
                self.download_retries,
                destination.name,
                error,
                attempt_elapsed,
                round(wait_seconds),
            )
            await asyncio.sleep(wait_seconds)
        return False

    async def _metadata_stage(self, name: str, operation: Awaitable[Any]):
        """Run one Blink metadata operation with visible progress and a timeout."""
        self._progress(phase="metadata", file=name)
        LOGGER.info("[Blink metadata] %s started", name)
        started_at = time.monotonic()
        try:
            async with asyncio.timeout(self.metadata_timeout_seconds):
                result = await operation
        except TimeoutError as exc:
            raise RuntimeError(
                f"Blink metadata stage '{name}' exceeded "
                f"{self.metadata_timeout_seconds:g} seconds."
            ) from exc
        LOGGER.info(
            "[Blink metadata] %s complete in %.1fs",
            name,
            time.monotonic() - started_at,
        )
        return result

    def _publish_cloud_downloads(self, staging_dir: Path) -> int:
        """Atomically publish completed cloud clips from outside the AI queue."""
        published = 0
        for source in staging_dir.rglob("*.mp4"):
            destination = self.download_dir / source.relative_to(staging_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                source.unlink()
                continue
            source.replace(destination)
            self._downloaded(destination)
            published += 1
        return published

    def _prioritize_local_items(self, local_items):
        """Prefer recent clips and bound one scan so alerts cannot trail a backlog."""
        ordered = sorted(
            local_items,
            key=lambda value: value[0].created_at,
            reverse=True,
        )
        if not self.max_clips_per_scan:
            return ordered, 0
        selected = ordered[: self.max_clips_per_scan]
        return selected, len(ordered) - len(selected)

    async def _refresh_local_storage_manifests(self, blink) -> None:
        """Refresh manifests without preparing every stored clip for download."""
        for name, sync_module in blink.sync.items():
            local_state = getattr(sync_module, "_local_storage", None)
            if not isinstance(local_state, dict) or not local_state.get("status"):
                continue
            for attempt in range(1, 4):
                updated = await self._metadata_stage(
                    f"Sync Module manifest refresh ({name}, attempt {attempt}/3)",
                    sync_module.update_local_storage_manifest(),
                )
                if updated and not local_state.get("manifest_stale"):
                    break
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
            else:
                self.incomplete_downloads = True
                LOGGER.warning(
                    "[Blink metadata] Sync Module manifest remains stale: %s",
                    name,
                )

    async def download_new(self, since: datetime | None = None) -> int:
        self.incomplete_downloads = False
        self.backlog_remaining = False
        if not self.configured:
            return 0
        try:
            from aiohttp import ClientSession
            from blinkpy.auth import Auth
            from blinkpy.blinkpy import Blink
            from blinkpy.helpers.util import json_load
        except ImportError as exc:
            raise RuntimeError("The Blink integration package is not installed.") from exc

        before = {path.resolve() for path in self.download_dir.rglob("*.mp4")}
        since = since or datetime.now(timezone.utc) - timedelta(days=1)
        LOGGER.info("[Scan] Fetching Blink video list: since=%s", since.isoformat())
        async with ClientSession() as session:
            blink = Blink(session=session)
            blink.auth = Auth(
                await json_load(str(self.auth_file)),
                no_prompt=True,
                session=session,
            )
            started = await self._metadata_stage(
                "authentication and setup", blink.start()
            )
            if not started:
                raise RuntimeError("Blink authentication or platform setup failed.")
            # A full Blink refresh calls check_new_videos(), which prepares every
            # matching local-storage clip before our batch limit can be applied.
            # Refresh only the manifest here, then select and prepare the bounded
            # set of clips below.
            await self._refresh_local_storage_manifests(blink)
            selected = None if self.camera.lower() == "all" else self.camera.lower()

            # BlinkPy's camera.recent_clips intentionally expires items older
            # than one hour. Read the local-storage manifest directly so an
            # explicit 3h/6h/9h backfill can retrieve older clips as well.
            local_items = []
            for sync_module in blink.sync.values():
                local_state = getattr(sync_module, "_local_storage", None)
                if not isinstance(local_state, dict) or not local_state.get("status"):
                    continue
                manifest = local_state.get("manifest") or []
                for item in sorted(manifest, key=lambda value: value.created_at):
                    captured = item.created_at
                    if captured.tzinfo is None:
                        captured = captured.replace(tzinfo=timezone.utc)
                    else:
                        captured = captured.astimezone(timezone.utc)
                    if captured < since.astimezone(timezone.utc):
                        continue
                    if selected and item.name.lower() != selected:
                        continue
                    safe_camera = "".join(
                        character if character.isalnum() or character in "-_" else "_"
                        for character in item.name
                    ).strip("_") or "camera"
                    camera_dir = self.download_dir / safe_camera
                    camera_dir.mkdir(parents=True, exist_ok=True)
                    local_time = captured.astimezone()
                    destination = camera_dir / f"{local_time:%Y%m%d_%H%M%S}_{safe_camera}.mp4"
                    rejected_destination = (
                        self.download_dir.parent / "rejected" / safe_camera / destination.name
                    )
                    if destination.exists() or rejected_destination.exists():
                        continue
                    local_items.append((item, destination))

            discovered_count = len(local_items)
            local_items, deferred_count = self._prioritize_local_items(local_items)
            self.backlog_remaining = deferred_count > 0
            LOGGER.info(
                "[Download] Found %d new Sync Module clips; processing newest %d",
                discovered_count,
                len(local_items),
            )
            self._progress(
                phase="downloading", current=0, total=len(local_items), file=None
            )
            if deferred_count:
                self.incomplete_downloads = True
                LOGGER.warning(
                    "[Download] Deferring %d older clips to the next scan",
                    deferred_count,
                )
            failed_downloads = 0
            for index, (item, destination) in enumerate(local_items, start=1):
                self._progress(current=index, file=destination.name)
                LOGGER.info(
                    "[Download %d/%d] camera=%s file=%s",
                    index,
                    len(local_items),
                    item.name,
                    destination.name,
                )
                if not await self._download_local_item(item, blink, destination):
                    failed_downloads += 1
                    self.incomplete_downloads = True
                await asyncio.sleep(self.download_delay_seconds)

            if failed_downloads:
                LOGGER.warning(
                    "[Download] Skipped %d clips due to Blink response limits; retrying next scan",
                    failed_downloads,
                )

            # Cloud-backed accounts can expose more than the recent-clip window.
            # Downloading again is harmless because BlinkPy skips existing names.
            if not deferred_count:
                cloud_staging = self.download_dir.parent / ".cloud-download-staging"
                shutil.rmtree(cloud_staging, ignore_errors=True)
                cloud_staging.mkdir(parents=True, exist_ok=True)
                try:
                    await blink.download_videos(
                        str(cloud_staging),
                        since=since.astimezone().strftime("%Y/%m/%d %H:%M"),
                        camera=self.camera,
                        delay=self.download_delay_seconds,
                    )
                    published = self._publish_cloud_downloads(cloud_staging)
                    if published:
                        LOGGER.info(
                            "[Cloud download] Published %d completed clips to the AI queue",
                            published,
                        )
                except Exception as exc:
                    # Local-storage clips already downloaded above must still be
                    # analyzed even when Blink throttles the optional cloud pass.
                    LOGGER.warning(
                        "[Cloud download] Failed, but analysis will continue: %s",
                        exc,
                    )
                finally:
                    shutil.rmtree(cloud_staging, ignore_errors=True)
            else:
                LOGGER.info(
                    "[Cloud download] Skipped because deferred Sync Module clips remain"
                )
            await blink.save(str(self.auth_file))
        after = {path.resolve() for path in self.download_dir.rglob("*.mp4")}
        downloaded = len(after - before)
        LOGGER.info("[Download complete] %d new clips", downloaded)
        return downloaded


async def interactive_setup(auth_file: Path) -> None:
    from aiohttp import ClientSession
    from blinkpy.auth import BlinkTwoFARequiredError
    from blinkpy.blinkpy import Blink

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    print("Your Blink email and password are used only on this computer.")
    print("Enter the two-factor authentication code when prompted.\n")
    async with ClientSession() as session:
        blink = Blink(session=session)
        try:
            await blink.start()
        except BlinkTwoFARequiredError:
            await blink.prompt_2fa()
        await blink.save(str(auth_file))
    try:
        auth_file.chmod(0o600)
    except OSError:
        pass
    print(f"\nAuthentication complete: {auth_file}")

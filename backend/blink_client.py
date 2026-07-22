from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
        max_clips_per_scan: int = 20,
    ):
        self.auth_file = auth_file
        self.download_dir = download_dir
        self.camera = camera
        self.download_retries = max(1, download_retries)
        self.download_delay_seconds = max(0.0, download_delay_seconds)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.clip_timeout_seconds = max(1.0, clip_timeout_seconds)
        self.max_clips_per_scan = max(0, max_clips_per_scan)
        self.incomplete_downloads = False

    @property
    def configured(self) -> bool:
        return self.auth_file.exists()

    async def _download_local_item(self, item, blink, destination: Path) -> bool:
        """Download one Sync Module clip without aborting the whole scan."""
        started_at = time.monotonic()
        for attempt in range(1, self.download_retries + 1):
            attempt_started_at = time.monotonic()
            try:
                async with asyncio.timeout(self.clip_timeout_seconds):
                    prepared = await item.prepare_download(blink)
                    if not prepared:
                        raise RuntimeError(
                            "Blink가 다운로드 준비 명령에 응답하지 않았습니다."
                        )
                    await item.download_video(blink, str(destination))
                    if not destination.exists() or destination.stat().st_size == 0:
                        raise RuntimeError("Blink가 빈 영상 파일을 반환했습니다.")
                LOGGER.info(
                    "[다운로드 성공] %s 크기=%d bytes 소요=%.1f초",
                    destination.name,
                    destination.stat().st_size,
                    time.monotonic() - started_at,
                )
                return True
            except TimeoutError:
                error = (
                    f"Blink 영상 준비·다운로드가 "
                    f"{self.clip_timeout_seconds:g}초를 초과했습니다."
                )
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            destination.unlink(missing_ok=True)
            attempt_elapsed = time.monotonic() - attempt_started_at
            if attempt >= self.download_retries:
                LOGGER.error(
                    "[다운로드 실패] %s: %d회 시도 후 건너뜀; "
                    "다음 스캔에서 재시도 (소요=%.1f초, 원인=%s)",
                    destination.name,
                    attempt,
                    time.monotonic() - started_at,
                    error,
                )
                return False
            wait_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "[다운로드 재시도 %d/%d] %s: %s "
                "(이번 시도 %.1f초, %d초 후 재시도)",
                attempt,
                self.download_retries,
                destination.name,
                error,
                attempt_elapsed,
                round(wait_seconds),
            )
            await asyncio.sleep(wait_seconds)
        return False

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

    async def download_new(self, since: datetime | None = None) -> int:
        self.incomplete_downloads = False
        if not self.configured:
            return 0
        try:
            from aiohttp import ClientSession
            from blinkpy.auth import Auth
            from blinkpy.blinkpy import Blink
            from blinkpy.helpers.util import json_load
        except ImportError as exc:
            raise RuntimeError("Blink 연동 패키지가 설치되지 않았습니다.") from exc

        before = {path.resolve() for path in self.download_dir.rglob("*.mp4")}
        since = since or datetime.now(timezone.utc) - timedelta(days=1)
        LOGGER.info("[스캔] Blink 영상 목록 조회 시작: since=%s", since.isoformat())
        async with ClientSession() as session:
            blink = Blink(session=session)
            blink.auth = Auth(
                await json_load(str(self.auth_file)),
                no_prompt=True,
                session=session,
            )
            await blink.start()
            # Refresh populates each camera's recent_clips from both cloud and
            # Sync Module local-storage manifests. This is the important path
            # for free accounts using USB/microSD storage.
            local_since = since.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
            for sync_module in blink.sync.values():
                local_state = getattr(sync_module, "_local_storage", None)
                if isinstance(local_state, dict) and local_state.get("status"):
                    local_state["last_manifest_read"] = local_since
            await blink.refresh(force=True)
            # Sync Modules occasionally return code 2102 while rebuilding a
            # larger manifest. Retry that manifest without redoing login.
            for sync_module in blink.sync.values():
                local_state = getattr(sync_module, "_local_storage", None)
                if not isinstance(local_state, dict) or not local_state.get("status"):
                    continue
                for retry in range(3):
                    if not local_state.get("manifest_stale"):
                        break
                    await asyncio.sleep(3 * (retry + 1))
                    await sync_module.update_local_storage_manifest()
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
            LOGGER.info(
                "[다운로드] Sync Module 새 영상 %d개 발견; 최신 %d개 처리",
                discovered_count,
                len(local_items),
            )
            if deferred_count:
                self.incomplete_downloads = True
                LOGGER.warning(
                    "[다운로드] 오래된 영상 %d개는 다음 스캔으로 이월",
                    deferred_count,
                )
            failed_downloads = 0
            for index, (item, destination) in enumerate(local_items, start=1):
                LOGGER.info(
                    "[다운로드 %d/%d] 카메라=%s 파일=%s",
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
                    "[다운로드] %d개 영상은 Blink 응답 제한으로 건너뜀; 다음 스캔에서 다시 시도",
                    failed_downloads,
                )

            # Cloud-backed accounts can expose more than the recent-clip window.
            # Downloading again is harmless because BlinkPy skips existing names.
            if not deferred_count:
                try:
                    await blink.download_videos(
                        str(self.download_dir),
                        since=since.astimezone().strftime("%Y/%m/%d %H:%M"),
                        camera=self.camera,
                        delay=self.download_delay_seconds,
                    )
                except Exception as exc:
                    # Local-storage clips already downloaded above must still be
                    # analyzed even when Blink throttles the optional cloud pass.
                    LOGGER.warning("[Cloud 다운로드] 실패했지만 분석은 계속함: %s", exc)
            else:
                LOGGER.info(
                    "[Cloud 다운로드] Sync Module 이월 영상이 있어 이번에는 건너뜀"
                )
            await blink.save(str(self.auth_file))
        after = {path.resolve() for path in self.download_dir.rglob("*.mp4")}
        downloaded = len(after - before)
        LOGGER.info("[다운로드 완료] 새 영상 %d개", downloaded)
        return downloaded


async def interactive_setup(auth_file: Path) -> None:
    from aiohttp import ClientSession
    from blinkpy.auth import BlinkTwoFARequiredError
    from blinkpy.blinkpy import Blink

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    print("Blink 이메일과 비밀번호는 이 컴퓨터에서만 사용됩니다.")
    print("2단계 인증 번호가 오면 아래 안내에 따라 입력하세요.\n")
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
    print(f"\n인증 완료: {auth_file}")

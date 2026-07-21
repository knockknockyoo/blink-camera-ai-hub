from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession
from blinkpy.auth import Auth
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load

from backend.config import settings


async def inspect(hours: float) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with ClientSession() as session:
        blink = Blink(session=session)
        blink.auth = Auth(
            await json_load(str(settings.auth_file)),
            no_prompt=True,
            session=session,
        )
        started = await blink.start()
        print(f"login_ok={bool(started)} cameras={len(blink.cameras)} sync_modules={len(blink.sync)}")

        local_since = since.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        for name, sync_module in blink.sync.items():
            state = getattr(sync_module, "_local_storage", {})
            print(
                f"sync={name!r} local_active={bool(state.get('status'))} "
                f"local_enabled={bool(state.get('enabled'))} "
                f"local_compatible={bool(state.get('compatible'))}"
            )
            if state.get("status"):
                state["last_manifest_read"] = local_since

        refreshed = await blink.refresh(force=True)
        print(f"refresh_ok={bool(refreshed)} range_hours={hours:g}")
        for name, sync_module in blink.sync.items():
            state = getattr(sync_module, "_local_storage", {})
            manifest = state.get("manifest") or []
            recent_manifest = [
                item
                for item in manifest
                if item.created_at.replace(tzinfo=timezone.utc) >= since
            ]
            print(
                f"sync={name!r} manifest_ready={not bool(state.get('manifest_stale'))} "
                f"manifest_clips={len(manifest)} recent_manifest_clips={len(recent_manifest)}"
            )
            latest = sorted(manifest, key=lambda value: value.created_at, reverse=True)[:3]
            for item in latest:
                print(
                    f"latest camera={item.name!r} created_at={item.created_at.isoformat()} "
                    f"size={item.size}"
                )
            for item in sorted(recent_manifest, key=lambda value: value.created_at, reverse=True)[:5]:
                print(
                    f"recent camera={item.name!r} created_at={item.created_at.isoformat()} "
                    f"size={item.size}"
                )
        for name, camera in blink.cameras.items():
            print(f"camera={name!r} recent_clips={len(camera.recent_clips)}")

        cloud = await blink.get_videos_metadata(
            since=since.astimezone().strftime("%Y/%m/%d %H:%M"),
            stop=10,
        )
        print(f"cloud_clips={len(cloud)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=3)
    args = parser.parse_args()
    asyncio.run(inspect(args.hours))

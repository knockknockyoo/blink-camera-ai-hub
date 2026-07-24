from __future__ import annotations

import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
REQUIRED = {
    "NATIVE_AI_URL": "http://host.docker.internal:8790",
    "NATIVE_AI_TIMEOUT_SECONDS": "300",
    "AI_DEVICE": "mps",
    "NATIVE_AI_BACKEND": "rfdetr",
    "RFDETR_MODEL_SIZE": "small",
    "RFDETR_MAX_FRAMES": "14",
    "RFDETR_VEHICLE_MIN_BOX_MOTION": "0.03",
    "NATIVE_AI_CONCURRENCY": "1",
    "NATIVE_AI_PORT": "8790",
}


def configure() -> None:
    content = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    lines = content.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    required = dict(REQUIRED)
    required["NATIVE_AI_TOKEN"] = values.get("NATIVE_AI_TOKEN") or secrets.token_hex(32)

    output: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in required and not line.lstrip().startswith("#"):
            output.append(f"{key}={required[key]}")
            replaced.add(key)
        else:
            output.append(line)
    if output and output[-1]:
        output.append("")
    if set(required) - replaced:
        output.append("# macOS native Apple GPU analysis")
    for key, value in required.items():
        if key not in replaced:
            output.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    configure()

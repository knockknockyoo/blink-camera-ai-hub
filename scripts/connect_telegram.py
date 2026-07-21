from __future__ import annotations

import asyncio
import getpass
from pathlib import Path

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def save_env(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


async def api(token: str, method: str, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as response:
            result = await response.json(content_type=None)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API 오류"))
    return result["result"]


async def main() -> None:
    print("Telegram Bot 토큰은 화면에 표시되거나 로그에 남지 않습니다.")
    token = getpass.getpass("@BotFather가 발급한 Bot Token: ").strip()
    bot = await api(token, "getMe")
    print(f"Bot 확인 완료: @{bot['username']}")
    print(f"Telegram에서 @{bot['username']}에게 /start 또는 아무 메시지를 보내세요.")
    input("메시지를 보낸 뒤 Enter를 누르세요: ")
    updates = await api(token, "getUpdates", timeout="0", limit="100")
    chats = [
        update["message"]["chat"]
        for update in updates
        if update.get("message", {}).get("chat", {}).get("id") is not None
    ]
    if not chats:
        raise RuntimeError("Bot에 보낸 메시지를 찾지 못했습니다. /start를 보낸 뒤 다시 실행하세요.")
    chat = chats[-1]
    chat_id = str(chat["id"])
    save_env(
        {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_CHAT_ID": chat_id,
            "TELEGRAM_PROTECT_CONTENT": "true",
        }
    )
    await api(
        token,
        "sendMessage",
        chat_id=chat_id,
        text="✅ Blink Camera AI Hub Telegram 연결이 완료됐습니다.",
        protect_content="true",
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:8787/api/telegram/reload"
            ) as response:
                if response.status == 200:
                    print("실행 중인 Blink Camera AI Hub에도 Telegram 설정을 적용했습니다.")
    except aiohttp.ClientError:
        print("Blink Camera AI Hub가 꺼져 있습니다. bash scripts/run.sh로 실행하세요.")
    print("Telegram 연결 완료.")


if __name__ == "__main__":
    asyncio.run(main())

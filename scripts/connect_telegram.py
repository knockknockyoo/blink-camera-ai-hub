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
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result["result"]


async def main() -> None:
    print("The Telegram bot token will not be displayed or written to logs.")
    token = getpass.getpass("Bot token issued by @BotFather: ").strip()
    bot = await api(token, "getMe")
    print(f"Bot verified: @{bot['username']}")
    print(f"Send /start or any message to @{bot['username']} in Telegram.")
    input("Press Enter after sending the message: ")
    updates = await api(token, "getUpdates", timeout="0", limit="100")
    chats = [
        update["message"]["chat"]
        for update in updates
        if update.get("message", {}).get("chat", {}).get("id") is not None
    ]
    if not chats:
        raise RuntimeError("No message to the bot was found. Send /start and run this setup again.")
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
        text="✅ Blink Camera AI Hub is connected to Telegram.",
        protect_content="true",
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:8787/api/telegram/reload"
            ) as response:
                if response.status == 200:
                    print("Telegram settings were applied to the running Blink Camera AI Hub service.")
    except aiohttp.ClientError:
        print("Blink Camera AI Hub is not running. Start it with bash scripts/run.sh.")
    print("Telegram setup complete.")


if __name__ == "__main__":
    asyncio.run(main())

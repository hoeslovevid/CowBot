import os
import runpy
import subprocess
import sys
import threading
import time


def resolve_role() -> tuple[str, str]:
    role = (os.getenv("APP_ROLE") or "all").strip().lower()
    if role == "combined":
        role = "all"
    if role in {"bot", "web", "all"}:
        return role, f"APP_ROLE={role}"
    return "all", "default"


def run_script(script: str) -> None:
    sys.argv[0] = script
    runpy.run_path(script, run_name="__main__")


def internal_bot_port() -> str:
    public_port = os.getenv("PORT") or "5000"
    requested = os.getenv("BOT_API_PORT") or "8080"
    if requested == public_port:
        return "8090"
    return requested


REQUIRED_BOT_VARS = (
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "TWITCH_NICK",
    "TWITCH_CHANNEL",
)


def missing_bot_vars(env: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for key in REQUIRED_BOT_VARS:
        value = (env.get(key) or "").strip()
        if not value or value.lower().startswith("your_"):
            missing.append(key)
    return missing


def supervise_bot(bot_env: dict[str, str]) -> None:
    missing = missing_bot_vars(bot_env)
    if missing:
        print(
            "Bot is not starting. Add these Railway Variables on this service: "
            + ", ".join(missing)
        )
        print("Copy the values from your local .env. Do not leave placeholders like your_twitch_client_id.")
        return

    while True:
        print("Starting bot.py")
        bot = subprocess.Popen([sys.executable, "-u", "bot.py"], env=bot_env)
        code = bot.wait()
        print(f"Bot process exited with {code}; restarting in 5s")
        time.sleep(5)


def run_combined() -> None:
    bot_port = internal_bot_port()
    bot_env = os.environ.copy()
    bot_env.pop("PORT", None)
    bot_env["APP_ROLE"] = "bot"
    bot_env["BOT_API_HOST"] = "127.0.0.1"
    bot_env["BOT_API_PORT"] = bot_port

    os.environ["BOT_API_URL"] = f"http://127.0.0.1:{bot_port}"

    print(f"Starting CowBot combined: dashboard on PORT={os.getenv('PORT')}, bot API on 127.0.0.1:{bot_port}")
    threading.Thread(target=supervise_bot, args=(bot_env,), daemon=True).start()
    run_script("dashboard.py")


def main() -> None:
    role, reason = resolve_role()
    if role == "all":
        print(f"Starting CowBot as combined ({reason})")
        run_combined()
        return
    script = "bot.py" if role == "bot" else "dashboard.py"
    print(f"Starting CowBot as {role} ({reason}) -> {script}")
    run_script(script)


if __name__ == "__main__":
    main()

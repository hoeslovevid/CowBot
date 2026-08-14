import os
import runpy
import subprocess
import sys
import threading
import time
import urllib.request


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


def wait_for_bot(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(60):
        if process.poll() is not None:
            raise SystemExit(f"Bot exited before it was ready (code {process.returncode})")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.5)
    process.terminate()
    raise SystemExit("Bot API did not become ready")


def run_combined() -> None:
    bot_port = os.getenv("BOT_API_PORT") or "8080"
    bot_env = os.environ.copy()
    bot_env.pop("PORT", None)
    bot_env["APP_ROLE"] = "bot"
    bot_env["BOT_API_HOST"] = "127.0.0.1"
    bot_env["BOT_API_PORT"] = bot_port

    print(f"Starting CowBot combined: bot.py on 127.0.0.1:{bot_port}, dashboard on PORT")
    bot = subprocess.Popen([sys.executable, "-u", "bot.py"], env=bot_env)

    def on_bot_exit() -> None:
        code = bot.wait()
        print(f"Bot process exited with {code}")
        os._exit(code or 1)

    threading.Thread(target=on_bot_exit, daemon=True).start()
    wait_for_bot(f"http://127.0.0.1:{bot_port}/health", bot)

    os.environ["BOT_API_URL"] = os.getenv("BOT_API_URL") or f"http://127.0.0.1:{bot_port}"
    try:
        run_script("dashboard.py")
    finally:
        if bot.poll() is None:
            bot.terminate()
            try:
                bot.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bot.kill()


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

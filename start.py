import os
import runpy
import sys


def resolve_role() -> tuple[str, str]:
    role = (os.getenv("APP_ROLE") or "").strip().lower()
    if role in {"bot", "web"}:
        return role, f"APP_ROLE={role}"

    name = (os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower()
    if name in {"bot", "cowbot"} or name.endswith("-bot") or name.endswith("_bot"):
        return "bot", f"RAILWAY_SERVICE_NAME={name}"
    if name and "bot" in name and "web" not in name:
        return "bot", f"RAILWAY_SERVICE_NAME={name}"
    if name:
        return "web", f"RAILWAY_SERVICE_NAME={name}"
    return "web", "default"


def main() -> None:
    role, reason = resolve_role()
    script = "bot.py" if role == "bot" else "dashboard.py"
    print(f"Starting CowBot as {role} ({reason}) -> {script}")
    sys.argv[0] = script
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()

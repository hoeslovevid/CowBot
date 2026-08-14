import os
from collections.abc import Awaitable, Callable

from aiohttp import web

import store

AnnounceFn = Callable[[str], Awaitable[None]]
StatusFn = Callable[[], dict]


def _authorized(request: web.Request) -> bool:
    expected = os.getenv("API_SECRET", "")
    if not expected:
        return True
    return request.headers.get("X-API-Secret", "") == expected


def create_api_app(*, get_status: StatusFn, announce: AnnounceFn) -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "cowbot"})

    async def require_auth(request: web.Request) -> web.Response | None:
        if not _authorized(request):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
        return None

    async def status(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        return web.json_response(get_status())

    async def update_settings(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        daily_min = store.parse_non_negative_int(str(payload.get("daily_min", "")), 25)
        daily_max = store.parse_non_negative_int(str(payload.get("daily_max", "")), 100)
        if daily_min > daily_max:
            daily_min, daily_max = daily_max, daily_min
        starting_points = store.parse_non_negative_int(str(payload.get("starting_points", "")), 100)
        default_raffle_cost = max(store.parse_non_negative_int(str(payload.get("default_raffle_cost", "")), 50), 1)
        store.set_setting("daily_min", str(daily_min))
        store.set_setting("daily_max", str(daily_max))
        store.set_setting("starting_points", str(starting_points))
        store.set_setting("default_raffle_cost", str(default_raffle_cost))
        return web.json_response({"ok": True, "settings": store.get_dashboard_settings()})

    async def manage_poll(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action == "start":
            name = (payload.get("name") or "").strip()
            question = (payload.get("question") or "").strip()
            options = payload.get("options") or []
            if isinstance(options, str):
                options = [opt.strip() for opt in options.split(",") if opt.strip()]
            success, error = store.create_poll(name, question, options)
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
            await announce(f"Poll '{name}' started: {question} Options: {', '.join(store.unique_options(options))}")
            return web.json_response({"ok": True})
        if action == "end":
            active_poll = store.get_active_poll_name()
            if not active_poll:
                return web.json_response({"ok": False, "error": "There is no active poll."}, status=400)
            results = store.end_poll(active_poll)
            top = ", ".join(f"{row['option']}({row['votes']})" for row in results)
            await announce(f"Poll '{active_poll}' ended. Results: {top}")
            return web.json_response({"ok": True, "results": top})
        return web.json_response({"ok": False, "error": "Unknown poll action."}, status=400)

    async def manage_raffle(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action == "start":
            name = (payload.get("name") or "").strip()
            cost = store.parse_non_negative_int(str(payload.get("cost", "")), 0)
            if cost <= 0:
                cost = store.get_dashboard_settings()["default_raffle_cost"]
            success, error = store.create_raffle(name, cost)
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
            await announce(f"Raffle '{name}' started with entry cost {cost} points. Type !raffle enter.")
            return web.json_response({"ok": True})
        if action == "end":
            active_raffle = store.get_active_raffle_name()
            if not active_raffle:
                return web.json_response({"ok": False, "error": "There is no active raffle."}, status=400)
            winner = store.end_raffle(active_raffle)
            if winner:
                await announce(f"Raffle '{active_raffle}' ended! The winner is {winner}.")
            else:
                await announce(f"Raffle '{active_raffle}' ended with no entries.")
            return web.json_response({"ok": True, "winner": winner})
        return web.json_response({"ok": False, "error": "Unknown raffle action."}, status=400)

    async def manage_giveaway(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action == "start":
            name = (payload.get("name") or "").strip()
            success, result = store.start_giveaway(name)
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            await announce(f"Giveaway '{result}' started! Type !giveaway enter to join.")
            return web.json_response({"ok": True, "name": result})
        if action == "end":
            winner, giveaway_name = store.finish_giveaway()
            if winner and giveaway_name:
                await announce(f"Giveaway '{giveaway_name}' ended! The winner is {winner}.")
                return web.json_response({"ok": True, "winner": winner, "name": giveaway_name})
            return web.json_response({"ok": False, "error": giveaway_name or "No giveaway is currently active."}, status=400)
        return web.json_response({"ok": False, "error": "Unknown giveaway action."}, status=400)

    async def manage_quote(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        text = (payload.get("text") or "").strip()
        author = (payload.get("author") or "Unknown").strip() or "Unknown"
        if not text:
            return web.json_response({"ok": False, "error": "Quote text cannot be empty."}, status=400)
        quote_id = store.add_quote(text, author, "dashboard")
        return web.json_response({"ok": True, "id": quote_id})

    app.router.add_get("/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/settings", update_settings)
    app.router.add_post("/api/poll", manage_poll)
    app.router.add_post("/api/raffle", manage_raffle)
    app.router.add_post("/api/giveaway", manage_giveaway)
    app.router.add_post("/api/quote", manage_quote)
    return app


async def start_api_server(app: web.Application) -> web.AppRunner:
    host = os.getenv("BOT_API_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("BOT_API_PORT") or "8080")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Bot API listening on {host}:{port}")
    return runner

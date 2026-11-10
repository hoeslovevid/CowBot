import asyncio
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable

from aiohttp import web

import store
import shop
import chat_messages as chat
from tts import TTSQueue

AnnounceFn = Callable[..., Awaitable[None]]
StatusFn = Callable[[], dict]
ApplyTokensFn = Callable[[str, str], Awaitable[None]]


def _authorized(request: web.Request) -> bool:
    expected = os.getenv("API_SECRET", "")
    if not expected:
        return True
    return request.headers.get("X-API-Secret", "") == expected


def create_api_app(
    *,
    get_status: StatusFn,
    announce: AnnounceFn,
    apply_tokens: ApplyTokensFn | None = None,
    tts_queue: TTSQueue | None = None,
    apply_streamer_tokens: ApplyTokensFn | None = None,
    fetch_tts_rewards: Callable[[], Awaitable[list]] | None = None,
) -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "cowbot"})

    async def require_auth(request: web.Request) -> web.Response | None:
        if not _authorized(request):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
        return None

    def require_feature(name: str) -> web.Response | None:
        if store.is_feature_enabled(name):
            return None
        return web.json_response({"ok": False, "error": store.feature_off_message(name)}, status=400)

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
        watchtime_points = store.parse_non_negative_int(str(payload.get("watchtime_points", "")), 10)
        watchtime_minutes = store.parse_watchtime_minutes(payload.get("watchtime_minutes"))
        boost_watch_cost = store.parse_non_negative_int(str(payload.get("boost_watch_cost", "")), 500)
        boost_luck_cost = store.parse_non_negative_int(str(payload.get("boost_luck_cost", "")), 1000)
        boost_watch_minutes = store.parse_boost_minutes(payload.get("boost_watch_minutes"), 15)
        boost_luck_minutes = store.parse_boost_minutes(payload.get("boost_luck_minutes"), 15)
        boost_pricing = {}
        for kind in store.ECONOMY_COOLDOWNS:
            key = f"{kind}_cooldown_seconds"
            raw = str(payload.get(key, store.economy_cooldown_seconds(kind)))
            if not raw.isascii() or not raw.isdigit() or len(raw) > 5 or int(raw) > store.MAX_ECONOMY_COOLDOWN:
                return web.json_response({"ok": False, "error": "Cooldowns must be whole seconds from 0 to 86400."}, status=400)
            boost_pricing[key] = str(int(raw))
        for kind in store.BOOSTS:
            mode_key = f"boost_{kind}_cost_mode"
            percentage_key = f"boost_{kind}_percentage"
            mode = payload.get(mode_key, store.boost_cost_mode(kind))
            raw_percentage = str(payload.get(percentage_key, store.boost_percentage(kind)))
            if not isinstance(mode, str) or mode not in {"percentage", "flat"}:
                return web.json_response({"ok": False, "error": "Choose percentage or flat boost pricing."}, status=400)
            if not raw_percentage.isascii() or not raw_percentage.isdigit() or len(raw_percentage) > 3 or not 0 <= int(raw_percentage) <= 100:
                return web.json_response({"ok": False, "error": "Boost percentage must be a whole number from 0 to 100."}, status=400)
            boost_pricing[mode_key] = mode
            boost_pricing[percentage_key] = str(int(raw_percentage))
        if "prefixes" in payload:
            success, error = store.set_command_prefixes(str(payload.get("prefixes") or ""))
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
        for key, value in boost_pricing.items():
            store.set_setting(key, value)
        store.set_setting("daily_min", str(daily_min))
        store.set_setting("daily_max", str(daily_max))
        store.set_setting("starting_points", str(starting_points))
        store.set_setting("default_raffle_cost", str(default_raffle_cost))
        store.set_setting("watchtime_points", str(watchtime_points))
        store.set_setting("watchtime_minutes", str(watchtime_minutes))
        store.set_setting("boost_watch_cost", str(boost_watch_cost))
        store.set_setting("boost_luck_cost", str(boost_luck_cost))
        store.set_setting("boost_watch_minutes", str(boost_watch_minutes))
        store.set_setting("boost_luck_minutes", str(boost_luck_minutes))
        return web.json_response({"ok": True, "settings": store.get_dashboard_settings()})

    async def update_features(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        flags = payload.get("features") if isinstance(payload.get("features"), dict) else payload
        store.set_feature_flags(flags if isinstance(flags, dict) else {})
        return web.json_response({"ok": True, "features": store.get_feature_flags()})

    async def apply_oauth(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or "").strip()
        if not access:
            return web.json_response({"ok": False, "error": "Missing access token."}, status=400)
        store.set_twitch_tokens(access, refresh)
        if apply_tokens:
            async def apply_live() -> None:
                try:
                    await apply_tokens(access, refresh)
                except Exception as exc:
                    print(f"Failed to apply Twitch token live: {exc}")
            asyncio.create_task(apply_live())
        return web.json_response({"ok": True})

    async def update_builtin_commands(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        payload = await request.json()
        if isinstance(payload.get("commands"), dict):
            flags = payload["commands"]
        else:
            flags = {key: payload.get(key) for key in store.BUILTIN_COMMANDS if key in payload}
        store.set_command_flags(flags)
        if "lurk_message" in payload:
            success, error = store.set_lurk_message(str(payload.get("lurk_message") or ""))
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
        if "unlurk_message" in payload:
            success, error = store.set_unlurk_message(str(payload.get("unlurk_message") or ""))
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
        return web.json_response({
            "ok": True,
            "builtin_command_groups": store.get_command_groups(),
            "settings": store.get_dashboard_settings(),
        })

    async def manage_poll(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("poll"):
            return disabled
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
            await announce(chat.line("poll", f"Poll '{name}' started: {question} Options: {', '.join(store.unique_options(options))}"))
            return web.json_response({"ok": True})
        if action == "end":
            active_poll = store.get_active_poll_name()
            if not active_poll:
                return web.json_response({"ok": False, "error": "There is no active poll."}, status=400)
            results = store.end_poll(active_poll)
            top = ", ".join(f"{row['option']}({row['votes']})" for row in results)
            await announce(chat.line("poll", f"Poll '{active_poll}' ended. Results: {top}"))
            return web.json_response({"ok": True, "results": top})
        return web.json_response({"ok": False, "error": "Unknown poll action."}, status=400)

    async def manage_raffle(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("raffle"):
            return disabled
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
            await announce(chat.line("raffle", f"Raffle '{name}' started with entry cost {cost} points. Type {store.primary_prefix()}raffle enter."))
            return web.json_response({"ok": True})
        if action == "end":
            active_raffle = store.get_active_raffle_name()
            if not active_raffle:
                return web.json_response({"ok": False, "error": "There is no active raffle."}, status=400)
            winner = store.end_raffle(active_raffle)
            if winner:
                await announce(chat.line("raffle", f"Raffle '{active_raffle}' ended! The winner is {winner}."))
            else:
                await announce(chat.line("raffle", f"Raffle '{active_raffle}' ended with no entries."))
            return web.json_response({"ok": True, "winner": winner})
        return web.json_response({"ok": False, "error": "Unknown raffle action."}, status=400)

    async def manage_queue(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("queue"):
            return disabled
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        prefix = store.primary_prefix()
        if action == "start":
            name = (payload.get("name") or "").strip()
            success, result = store.start_queue(name, payload.get("cap"))
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            cap = store.current_queue_state()["cap"]
            extra = f" Cap {cap}." if cap else ""
            await announce(chat.line("queue", f"Queue '{result}' is open! Type {prefix}queue to join.{extra}"))
            return web.json_response({"ok": True, "name": result, "cap": cap})
        if action == "next":
            user, error, remaining = store.next_queue()
            if error:
                return web.json_response({"ok": False, "error": error}, status=400)
            name = store.current_queue_state()["name"]
            leftover = f"{remaining} remaining." if remaining else "Queue is empty."
            await announce(chat.line("queue", f"{store.mention_user(user)} you're up for {name}! {leftover}"))
            return web.json_response({"ok": True, "user": user, "remaining": remaining, "name": name})
        if action == "close":
            success, result = store.close_queue()
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            await announce(chat.line("queue", f"Queue '{result}' is closed. No more joins."))
            return web.json_response({"ok": True, "name": result})
        if action == "open":
            success, result = store.open_queue()
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            await announce(chat.line("queue", f"Queue '{result}' is open again. Type {prefix}queue to join."))
            return web.json_response({"ok": True, "name": result})
        if action == "remove":
            user = (payload.get("user") or "").strip()
            success, result = store.remove_from_queue(user)
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            await announce(chat.line("queue", f"{store.mention_user(result)} was removed from the queue."))
            return web.json_response({"ok": True, "user": result})
        if action == "clear":
            success, result = store.clear_queue()
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            await announce(chat.line("queue", f"Queue '{result}' was cleared."))
            return web.json_response({"ok": True, "name": result})
        return web.json_response({"ok": False, "error": "Unknown queue action."}, status=400)

    async def manage_giveaway(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("giveaway"):
            return disabled
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action == "start":
            name = (payload.get("name") or "").strip()
            success, result = store.start_giveaway(name, payload.get("count"))
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            count = store.get_giveaway_winner_count()
            await announce(chat.giveaway_open(result, store.primary_prefix(), count), pin_giveaway=True)
            return web.json_response({"ok": True, "name": result, "count": count})
        if action == "draw":
            winners, giveaway_name, entries = store.draw_giveaway(payload.get("count"))
            if winners and giveaway_name:
                return web.json_response({
                    "ok": True,
                    "winner": winners[0],
                    "winners": winners,
                    "name": giveaway_name,
                    "entries": entries,
                })
            return web.json_response({"ok": False, "error": giveaway_name or "No giveaway is currently active."}, status=400)
        if action == "complete":
            winners, giveaway_name, is_reroll, replaced, drawn_winner = store.complete_giveaway_draw()
            if winners and giveaway_name:
                for index, message in enumerate(chat.giveaway_results(giveaway_name, winners, reroll=is_reroll, replaced=replaced, selected=drawn_winner)):
                    await announce(message, unpin_giveaway=not is_reroll and index == 0)
                return web.json_response({
                    "ok": True,
                    "winner": winners[0],
                    "winners": winners,
                    "name": giveaway_name,
                    "reroll": is_reroll,
                    "replaced": replaced,
                })
            return web.json_response({"ok": False, "error": giveaway_name or "No giveaway draw is waiting to finish."}, status=400)
        if action == "reroll":
            winners, giveaway_name, entries, replaced = store.reroll_giveaway(payload.get("replace"))
            if winners and giveaway_name:
                spin = store.get_overlay_spin() or {}
                return web.json_response({
                    "ok": True,
                    "winner": winners[0],
                    "winners": winners,
                    "name": giveaway_name,
                    "entries": entries,
                    "reroll": True,
                    "replaced": replaced or "",
                    "excluded": spin.get("excluded") or ([replaced] if replaced else []),
                })
            return web.json_response({"ok": False, "error": giveaway_name or "There is no giveaway to reroll."}, status=400)
        if action == "end":
            winners, giveaway_name = store.finish_giveaway()
            if winners and giveaway_name:
                for index, message in enumerate(chat.giveaway_results(giveaway_name, winners)):
                    await announce(message, unpin_giveaway=index == 0)
                return web.json_response({"ok": True, "winner": winners[0], "winners": winners, "name": giveaway_name})
            return web.json_response({"ok": False, "error": giveaway_name or "No giveaway is currently active."}, status=400)
        if action == "cancel":
            success, result = store.cancel_giveaway()
            if not success:
                return web.json_response({"ok": False, "error": result or "No giveaway is currently active."}, status=400)
            await announce(
                chat.giveaway_cancelled(result),
                unpin_giveaway=True,
            )
            return web.json_response({"ok": True, "name": result})
        return web.json_response({"ok": False, "error": "Unknown giveaway action."}, status=400)

    async def giveaway_overlay(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        return web.json_response({"ok": True, "spin": store.get_overlay_spin()})

    async def boost_overlay(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        return web.json_response(store.boost_overlay_state(), headers={"Cache-Control": "no-store"})

    async def manage_points(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("economy"):
            return disabled
        payload = await request.json()
        action = (payload.get("action") or "add").lower()
        amount = store.parse_non_negative_int(
            str(payload.get("amount") or payload.get("points_amount") or ""),
            0,
        )
        if amount <= 0:
            return web.json_response({"ok": False, "error": "Enter a points amount of 1 or more."}, status=400)
        if action == "remove":
            amount = -amount
        elif action not in {"add", "give"}:
            return web.json_response({"ok": False, "error": "Unknown points action."}, status=400)
        ok, error, results = store.adjust_points(
            [payload.get("user") or payload.get("points_user")], amount,
        )
        if not ok:
            return web.json_response({"ok": False, "error": error}, status=400)
        user, total, amount = results[0]["user"], results[0]["total"], results[0]["amount"]
        if amount > 0:
            await announce(chat.line("points", f"{store.mention_user(user)} gained {amount} points. Total: {total}."))
        else:
            await announce(chat.line("points", f"{store.mention_user(user)} lost {abs(amount)} points. Total: {total}."))
        return web.json_response({"ok": True, "user": user, "amount": amount, "total": total})

    async def bulk_points(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("economy"):
            return disabled
        payload = await request.json()
        amount = store.parse_non_negative_int(str(payload.get("amount", "")), 0)
        if amount <= 0:
            return web.json_response({"ok": False, "error": "Enter a points amount of 1 or more."}, status=400)
        ok, error, results = store.adjust_points(payload.get("users"), amount)
        if not ok:
            return web.json_response({"ok": False, "error": error}, status=400)
        await announce(chat.line("points", f"Gave {amount:,} points each to {len(results)} viewers."))
        return web.json_response({"ok": True, "amount": amount, "count": len(results), "users": results})

    async def shop_state(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        offset = store.parse_non_negative_int(request.query.get("offset", "0"), 0)
        return web.json_response(shop.snapshot(request.query.get("user", ""), offset), headers={"Cache-Control": "no-store"})

    async def manage_shop(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        data = await request.json()
        action = data.get("action")
        if action == "save":
            ok, message = shop.save_item(data)
        elif action in {"approve", "reject", "complete"}:
            order_id = store.parse_non_negative_int(str(data.get("id", "")), 0)
            ok, message = shop.review(order_id, action)
        else:
            return web.json_response({"ok": False, "error": "Unknown shop action."}, status=400)
        return web.json_response({"ok": ok, "message" if ok else "error": message}, status=200 if ok else 400)

    async def reset_points(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("economy"):
            return disabled
        payload = await request.json()
        scope = payload.get("scope")
        if not isinstance(scope, str) or scope not in {"user", "all"}:
            return web.json_response({"ok": False, "error": "Choose one viewer or everyone to reset."}, status=400)
        ok, result, count = store.reset_points(scope, payload.get("user"))
        if not ok:
            return web.json_response({"ok": False, "error": result}, status=400)
        return web.json_response({"ok": True, "scope": scope, "user": result, "count": count, "total": 0})

    async def manage_quote(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("quotes"):
            return disabled
        payload = await request.json()
        text = (payload.get("text") or "").strip()
        author = (payload.get("author") or "Unknown").strip() or "Unknown"
        if not text:
            return web.json_response({"ok": False, "error": "Quote text cannot be empty."}, status=400)
        quote_id = store.add_quote(text, author, "dashboard")
        return web.json_response({"ok": True, "id": quote_id})

    async def manage_schedule(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("scheduled_messages"):
            return disabled
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action == "create":
            success, error = store.add_scheduled_message(
                str(payload.get("message") or ""),
                store.parse_non_negative_int(str(payload.get("interval_minutes", "")), 0),
            )
            if not success:
                return web.json_response({"ok": False, "error": error}, status=400)
            return web.json_response({"ok": True})
        message_id = store.parse_non_negative_int(str(payload.get("id", "")), 0)
        if message_id <= 0:
            return web.json_response({"ok": False, "error": "Missing scheduled message."}, status=400)
        if action == "delete":
            if not store.delete_scheduled_message(message_id):
                return web.json_response({"ok": False, "error": "Scheduled message not found."}, status=404)
            return web.json_response({"ok": True})
        if action == "toggle":
            row = store.get_scheduled_message(message_id)
            if not row:
                return web.json_response({"ok": False, "error": "Scheduled message not found."}, status=404)
            store.set_scheduled_enabled(message_id, not row["enabled"])
            return web.json_response({"ok": True})
        if action == "send":
            row = store.get_scheduled_message(message_id)
            if not row:
                return web.json_response({"ok": False, "error": "Scheduled message not found."}, status=404)
            await announce(row["message"])
            store.mark_scheduled_sent(message_id)
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "Unknown schedule action."}, status=400)

    async def manage_commands(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if disabled := require_feature("custom_commands"):
            return disabled
        payload = await request.json()
        action = (payload.get("action") or "").lower()
        if action in {"create", "update"}:
            command_id = store.parse_non_negative_int(str(payload.get("id") or payload.get("command_id") or ""), 0)
            if action == "update" or command_id > 0:
                if command_id <= 0:
                    return web.json_response({"ok": False, "error": "Missing custom command."}, status=400)
                success, result = store.update_custom_command(
                    command_id,
                    str(payload.get("name") or ""),
                    str(payload.get("response") or ""),
                    str(payload.get("aliases") or ""),
                    payload.get("cooldown_seconds"),
                )
            else:
                success, result = store.upsert_custom_command(
                    str(payload.get("name") or ""),
                    str(payload.get("response") or ""),
                    str(payload.get("aliases") or ""),
                    payload.get("cooldown_seconds"),
                )
            if not success:
                return web.json_response({"ok": False, "error": result}, status=400)
            return web.json_response({"ok": True, "name": result})
        command_id = store.parse_non_negative_int(str(payload.get("id", "")), 0)
        if command_id <= 0:
            return web.json_response({"ok": False, "error": "Missing custom command."}, status=400)
        if action == "delete":
            if not store.delete_custom_command(command_id):
                return web.json_response({"ok": False, "error": "Custom command not found."}, status=404)
            return web.json_response({"ok": True})
        if action == "toggle":
            row = store.get_custom_command_by_id(command_id)
            if not row:
                return web.json_response({"ok": False, "error": "Custom command not found."}, status=404)
            store.set_custom_command_enabled(command_id, not row["enabled"])
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "Unknown command action."}, status=400)

    async def manage_tts(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if tts_queue is None:
            return web.json_response({"ok": False, "error": "TTS is not ready yet."}, status=503)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a TTS action object.")
            action = payload.get("action")
            if action == "settings":
                tts_queue.save_settings(payload.get("settings", {}))
            elif action == "test":
                tts_queue.enqueue(event_key=uuid.uuid4().hex, username="CowBot",
                                  text=payload.get("text"), source="test", test=True)
            elif action in {"pause", "resume", "clear", "approve", "reject", "skip", "replay"}:
                tts_queue.action(action, payload.get("id"))
            else:
                raise ValueError("Unknown TTS action.")
            return web.json_response({"ok": True, "tts": tts_queue.snapshot(), "message": "Speech queued." if action == "test" else "TTS updated."})
        except (ValueError, TypeError, AttributeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def tts_access(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if not tts_queue:
            return web.json_response({"ok": False, "error": "TTS is not ready yet."}, status=503)
        return web.json_response({"ok": True, "key": tts_queue.overlay_key()})

    async def tts_player(request: web.Request) -> web.StreamResponse:
        if unauthorized := await require_auth(request):
            return unauthorized
        if not tts_queue:
            return web.json_response({"ok": False, "error": "TTS is not ready yet."}, status=503)
        key = request.headers.get("X-TTS-Key", "")
        if not key or not secrets.compare_digest(key, tts_queue.overlay_key()):
            return web.json_response({"ok": False, "error": "Invalid TTS player key."}, status=403)
        kind = request.match_info["operation"]
        try:
            if request.method == "GET" and kind == "state":
                return web.json_response({"ok": True, **tts_queue.player_state(request.query.get("client", ""))})
            if request.method == "GET" and kind == "audio":
                path = tts_queue.audio_path(request.query.get("id", ""), request.query.get("client", ""))
                if path:
                    return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": "audio/wav"})
                return web.json_response({"ok": False, "error": "Audio not available."}, status=404)
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a player action object.")
            client = str(payload.get("client", ""))
            if kind == "claim":
                job = await asyncio.to_thread(tts_queue.claim, client)
                return web.json_response({"ok": True, "job": job})
            if kind == "finish":
                success = tts_queue.finish(payload.get("id"), client, failed=payload.get("failed") is True)
                return web.json_response({"ok": True, "finished": success})
            raise ValueError("Unknown player action.")
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)

    async def streamer_oauth(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        if not apply_streamer_tokens:
            return web.json_response({"ok": False, "error": "Streamer connection is not ready."}, status=503)
        payload = await request.json()
        try:
            await apply_streamer_tokens(str(payload.get("access_token", "")), str(payload.get("refresh_token", "")))
            return web.json_response({"ok": True})
        except Exception:
            return web.json_response({"ok": False, "error": "Connect the configured streamer account and grant both TTS permissions."}, status=400)

    async def tts_rewards(request: web.Request) -> web.Response:
        if unauthorized := await require_auth(request):
            return unauthorized
        try:
            rewards = await fetch_tts_rewards() if fetch_tts_rewards else []
            return web.json_response({"ok": True, "rewards": rewards})
        except Exception:
            return web.json_response({"ok": False, "error": "Connect your streamer account to load Channel Point rewards."}, status=400)

    app.router.add_post("/api/tts", manage_tts)
    app.router.add_get("/api/tts/access", tts_access)
    app.router.add_get("/api/tts/rewards", tts_rewards)
    app.router.add_post("/api/tts/oauth", streamer_oauth)
    app.router.add_route("*", "/api/tts/player/{operation:state|audio|claim|finish}", tts_player)
    app.router.add_get("/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/settings", update_settings)
    app.router.add_post("/api/features", update_features)
    app.router.add_post("/api/oauth", apply_oauth)
    app.router.add_post("/api/builtin-commands", update_builtin_commands)
    app.router.add_post("/api/poll", manage_poll)
    app.router.add_post("/api/raffle", manage_raffle)
    app.router.add_post("/api/queue", manage_queue)
    app.router.add_post("/api/giveaway", manage_giveaway)
    app.router.add_get("/api/giveaway/overlay", giveaway_overlay)
    app.router.add_get("/api/boosts/overlay", boost_overlay)
    app.router.add_post("/api/points", manage_points)
    app.router.add_post("/api/points/reset", reset_points)
    app.router.add_post("/api/points/bulk", bulk_points)
    app.router.add_get("/api/shop", shop_state)
    app.router.add_post("/api/shop", manage_shop)
    app.router.add_post("/api/quote", manage_quote)
    app.router.add_post("/api/schedule", manage_schedule)
    app.router.add_post("/api/commands", manage_commands)
    return app


async def start_api_server(app: web.Application) -> web.AppRunner:
    host = os.getenv("BOT_API_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("BOT_API_PORT") or "8080")
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Bot API listening on {host}:{port}")
    return runner

import json
import time

import xbmc


_PLAYER_STATE_TIMEOUT = 2.0
_PLAYER_STATE_POLL_INTERVAL_MS = 100


def _execute_jsonrpc(method: str, params: dict) -> 'object | None':

    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        })))
    except json.JSONDecodeError as error:
        xbmc.log(f"Could not parse JSON-RPC response for {method}: {error}", xbmc.LOGWARNING)
        return None

    if not isinstance(response, dict):
        xbmc.log(f"JSON-RPC {method} returned an invalid response.", xbmc.LOGWARNING)
        return None

    if "error" in response:
        xbmc.log(f"JSON-RPC {method} failed: {response['error']}", xbmc.LOGWARNING)
        return None

    if "result" not in response:
        xbmc.log(f"JSON-RPC {method} returned no result.", xbmc.LOGWARNING)
        return None

    return response["result"]


def get_player_speed(player_id: int) -> 'int | None':

    result = _execute_jsonrpc("Player.GetProperties", {
        "playerid": player_id,
        "properties": ["speed"]
    })
    if not isinstance(result, dict) or not isinstance(result.get("speed"), int):
        xbmc.log(f"Could not determine the state of player {player_id}.", xbmc.LOGWARNING)
        return None

    return result["speed"]


def wait_for_player_pause(player_id: int) -> bool:

    deadline = time.monotonic() + _PLAYER_STATE_TIMEOUT
    while True:
        speed = get_player_speed(player_id)
        if speed == 0:
            return True
        if speed is None:
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        xbmc.sleep(min(_PLAYER_STATE_POLL_INTERVAL_MS, int(remaining * 1000)))


def get_active_player() -> 'tuple[int, int] | None':

    result = _execute_jsonrpc("Player.GetActivePlayers", {})
    if not isinstance(result, list):
        xbmc.log("Could not determine Kodi's active player.", xbmc.LOGWARNING)
        return None

    if not result:
        return None

    if not isinstance(result[0], dict):
        xbmc.log("Kodi returned an invalid active player.", xbmc.LOGWARNING)
        return None

    player_id = result[0].get("playerid")
    if not isinstance(player_id, int):
        xbmc.log("Kodi returned an invalid active player.", xbmc.LOGWARNING)
        return None

    speed = get_player_speed(player_id)
    if speed is None:
        return None

    return player_id, speed


def play_player(player_id: int) -> bool:

    result = _execute_jsonrpc("Player.PlayPause", {
        "playerid": player_id,
        "play": True
    })
    if (not isinstance(result, dict)
            or not isinstance(result.get("speed"), int)
            or result["speed"] <= 0):
        xbmc.log(f"Could start player {player_id}.", xbmc.LOGWARNING)
        return False

    return True

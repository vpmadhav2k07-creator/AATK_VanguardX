import threading
import json
import requests
import os
import random
import time
import queue
import shutil
import chess
import chess.engine
import chess.variant
import sys
import platform
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- CONFIGURATION ---
TOKEN = os.environ.get("LICHESS_TOKEN", "YOUR_SECRET_TOKEN_HERE")
BOT_USERNAME = "AATK_VANGUARDX"
BOT_USERNAME_LC = BOT_USERNAME.lower()

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": f"{BOT_USERNAME}/1.0 (+https://lichess.org/@/{BOT_USERNAME_LC})"
}

# Small helper to ensure important debug prints are flushed to logs immediately
def dbg_print(*args, **kwargs):
    print(*args, **kwargs, flush=True)

# Fail fast if token not provided
if not TOKEN or TOKEN == "YOUR_SECRET_TOKEN_HERE":
    dbg_print("[FATAL] LICHESS_TOKEN not set. Set LICHESS_TOKEN env var and restart.")
    # Do NOT exit immediately in debug mode; raise to ensure Render logs capture traceback
    raise SystemExit(1)

# Accept challenges from other bots? Set ACCEPT_BOTS=false to decline challengers explicitly flagged as bots.
ACCEPT_BOTS = os.environ.get("ACCEPT_BOTS", "true").lower() in ("1", "true", "yes")

SUPPORTED_VARIANTS = {
    'standard': chess.Board,
    'antichess': chess.variant.AntichessBoard,
    'atomic': chess.variant.AtomicBoard,
    'crazyhouse': chess.variant.CrazyhouseBoard,
    'horde': chess.variant.HordeBoard,
    'kingofthehill': chess.variant.KingOfTheHillBoard,
    'racingkings': chess.variant.RacingKingsBoard,
    'threecheck': chess.variant.ThreeCheckBoard,
}

engine_queue = queue.Queue()
active_games = set()
active_games_lock = threading.Lock()

# --- FAKE SERVER FOR RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Lichess Bot & Fake Server are fully active!")

    def log_message(self, format, *args):
        return


def run_fake_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    dbg_print(f"[RENDER] Fake health check server listening on port {port}")
    server.serve_forever()

# --- SAFE REQUESTS ---
def safe_lichess_post(url, json_data=None):
    try:
        # Some Lichess endpoints expect an empty POST with no JSON body (accept/decline).
        # requests.post(..., json=None) still sends a `null` body; call without json when json_data is None.
        headers = HEADERS.copy()
        if json_data is None and 'Content-Type' in headers:
            # Avoid sending Content-Type: application/json with an empty body
            headers.pop('Content-Type', None)

        dbg_print(f"[HTTP] POST -> {url} (body={'present' if json_data is not None else 'none'})")
        if json_data is None:
            response = requests.post(url, headers=headers, timeout=10)
        else:
            response = requests.post(url, headers=headers, json=json_data, timeout=10)

        dbg_print(f"[HTTP] Response {response.status_code} for POST {url}")
        if response.status_code == 429:
            dbg_print("[WARNING] 429 Rate Limit. Backing off...")
            time.sleep(5)
        if not response.ok:
            dbg_print(f"[POST ERROR] {response.status_code}: {response.text}")
        return response
    except Exception as e:
        dbg_print(f"[POST ERROR] Exception when POST {url}: {e}")
        dbg_print(traceback.format_exc())
        return None


def safe_lichess_stream(url, game_id=""):
    backoff = 5
    while True:
        try:
            dbg_print(f"[STREAM] Opening stream to {url} (game={game_id})")
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            dbg_print(f"[STREAM] Got HTTP {response.status_code} for stream {game_id}")
            if response.status_code == 200:
                return response
            elif response.status_code == 429:
                dbg_print(f"[{game_id}] 429 error. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                dbg_print(f"[{game_id}] Stream failed ({response.status_code}). Retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            dbg_print(f"[{game_id}] Stream exception: {e}. Retrying in 5s...")
            dbg_print(traceback.format_exc())
            time.sleep(5)

# --- GAME ACTIONS ---
def send_chat_message(game_id, room, text):
    url = f"https://lichess.org/api/bot/game/{game_id}/chat"
    data = {"room": room, "text": text}
    response = safe_lichess_post(url, json_data=data)
    if response and response.status_code == 200:
        dbg_print(f"[{game_id}] Chat message sent: {text}")
    else:
        dbg_print(f"[{game_id}] Failed to send chat: {response.status_code if response else 'No response'}")


def make_lichess_move(game_id, move_str):
    url = f"https://lichess.org/api/bot/game/{game_id}/move/{move_str}"
    response = safe_lichess_post(url)
    if response and response.status_code == 200:
        dbg_print(f"[{game_id}] Played move: {move_str}")
    elif response:
        dbg_print(f"[{game_id}] Move failed ({response.status_code}): {response.text}")

# --- ENGINE ---
def find_engine_binary(engine_name):
    resolved_path = shutil.which(engine_name)
    if resolved_path:
        return resolved_path
    fallback_paths = {
        'stockfish': ["/usr/games/stockfish", "/usr/bin/stockfish", "/usr/local/bin/stockfish"],
        'fairy-stockfish': ["/usr/local/bin/fairy-stockfish"],
    }
    for path in fallback_paths.get(engine_name, []):
        if os.path.exists(path):
            return path
    return None


def stockfish_worker():
    dbg_print("[ENGINE] Initializing...")
    stockfish_path = find_engine_binary("stockfish")
    dbg_print(f"[ENGINE] stockfish_path={stockfish_path}")
    if not stockfish_path:
        dbg_print("[CRITICAL] Stockfish not found!")
        return

    fairy_stockfish_path = find_engine_binary("fairy-stockfish")
    try:
        normal_engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
        normal_engine.configure({"Skill Level": 20, "Hash": 1024, "Threads": 4})
        dbg_print("[ENGINE] Stockfish started")
    except Exception as e:
        dbg_print(f"[CRITICAL] Failed to start Stockfish: {e}")
        dbg_print(traceback.format_exc())
        return

    fairy_engine = None
    if fairy_stockfish_path:
        try:
            fairy_engine = chess.engine.SimpleEngine.popen_uci(fairy_stockfish_path)
            fairy_engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
            dbg_print("[ENGINE] Fairy Stockfish started")
        except Exception as e:
            dbg_print(f"[WARNING] Failed to start Fairy Stockfish: {e}")
            dbg_print(traceback.format_exc())
            fairy_engine = None

    while True:
        game_id, moves_list, callback, variant_key = engine_queue.get()
        try:
            engine = normal_engine if variant_key == 'standard' else (fairy_engine or normal_engine)
            board_class = SUPPORTED_VARIANTS.get(variant_key, chess.Board)
            board = board_class()
            for move in moves_list:
                try:
                    board.push_uci(move)
                except Exception:
                    pass

            if board.is_game_over():
                callback(None)
            else:
                result = engine.play(board, chess.engine.Limit(time=0.1))
                best_move = result.move
                if best_move and board.is_legal(best_move):
                    callback(best_move.uci())
                else:
                    legal_moves = list(board.legal_moves)
                    callback(random.choice(legal_moves).uci() if legal_moves else None)
        except Exception as err:
            dbg_print(f"[{game_id}] Engine error: {err}")
            dbg_print(traceback.format_exc())
            callback(None)
        finally:
            engine_queue.task_done()


def play_game(game_id, variant_key):
    try:
        dbg_print(f"[GAME START] {game_id} | Variant: {variant_key}")
        moves_played = []
        bot_color = None
        opening_move_played = False

        game_url = f"https://lichess.org/api/bot/game/stream/{game_id}"
        response = safe_lichess_stream(game_url, game_id)

        for line in response.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line.decode('utf-8'))
            except Exception as parse_err:
                dbg_print(f"[STREAM ERROR] Failed to parse: {parse_err}")
                dbg_print(traceback.format_exc())
                continue

            event_type = event.get('type')

            if event_type == 'gameFull':
                state = event.get('state', {})
                moves_str = state.get('moves', '')
                moves_played = moves_str.split() if moves_str else []

                # Determine bot color - check both white and black
                white_id = (event.get('white') or {}).get('id', '') or ''
                black_id = (event.get('black') or {}).get('id', '') or ''

                if white_id and white_id.lower() == BOT_USERNAME_LC:
                    bot_color = 'white'
                elif black_id and black_id.lower() == BOT_USERNAME_LC:
                    bot_color = 'black'
                else:
                    dbg_print(f"[{game_id}] ERROR: Bot not found in game! White: {white_id}, Black: {black_id}")
                    # If bot not in game, ensure we remove from active_games so it can be retried later
                    with active_games_lock:
                        active_games.discard(game_id)
                    continue

                dbg_print(f"[GAME INFO] Bot plays as {bot_color}")

                # Send opening greeting
                send_chat_message(game_id, 'player', 'Hello! Good luck!')

                if bot_color == 'white' and len(moves_played) == 0 and not opening_move_played:
                    dbg_print(f"[{game_id}] Bot is White — making opening move...")
                    opening_move_played = True

                    def handle_move_result(move_uci):
                        if move_uci:
                            make_lichess_move(game_id, move_uci)

                    engine_queue.put((game_id, moves_played, handle_move_result, variant_key))

            elif event_type == 'gameState':
                # If we haven't determined bot_color yet, skip gameState updates
                if bot_color is None:
                    continue

                moves_str = event.get('moves', '')
                moves_played = moves_str.split() if moves_str else []
                is_bot_turn = (
                    (len(moves_played) % 2 == 0 and bot_color == 'white') or
                    (len(moves_played) % 2 == 1 and bot_color == 'black')
                )

                if is_bot_turn:
                    dbg_print(f"[{game_id}] Bot turn detected ({bot_color}), moves so far: {len(moves_played)}")

                    def handle_move_result(move_uci):
                        if move_uci:
                            make_lichess_move(game_id, move_uci)

                    engine_queue.put((game_id, moves_played, handle_move_result, variant_key))

    except Exception as conn_err:
        dbg_print(f"[SERVER CRITICAL] {conn_err}. Reconnecting in 10s...")
        dbg_print(traceback.format_exc())
        time.sleep(10)

    finally:
        with active_games_lock:
            active_games.discard(game_id)
        dbg_print(f"[GAME END] {game_id}")


def handle_challenge(event):
    try:
        challenge = event.get('challenge', {})
        challenge_id = challenge.get('id')
        challenger = challenge.get('challenger', {})
        challenger_id = challenger.get('id', '')
        challenger_name = challenger.get('name', challenger_id or 'Unknown')
        challenger_is_bot = challenger.get('bot', False)
        variant = challenge.get('variant', {}).get('key', 'unknown')
        speed = challenge.get('timeControl', {}).get('type', 'unknown')
        rated = challenge.get('rated', False)

        # Fallback heuristic if 'bot' flag is missing/unreliable
        heuristic_bot = False
        if not challenger_is_bot and challenger_id:
            heuristic_bot = 'bot' in challenger_id.lower()

        dbg_print(f"[CHALLENGE] Received from {challenger_name} id={challenger_id} (bot_flag={challenger.get('bot', None)} heuristic={heuristic_bot}) ({variant}, {speed}, {'rated' if rated else 'casual'})[...]")

        # If challenger is explicitly marked as a bot and ACCEPT_BOTS is false, decline.
        if challenger_is_bot and not ACCEPT_BOTS:
            dbg_print(f"[CHALLENGE] Declining challenge from bot (explicit): {challenger_name} ({challenger_id})")
            url = f"https://lichess.org/api/challenge/{challenge_id}/decline"
            response = safe_lichess_post(url)
            dbg_print(f"[CHALLENGE] Decline response: {response.status_code if response else 'No response'}")
            return

        # Otherwise accept all challenges (including bots if ACCEPT_BOTS=True). Log heuristic-only matches.
        if heuristic_bot and not challenger_is_bot:
            dbg_print(f"[CHALLENGE] Heuristic indicates challenger id contains 'bot' but challenger flag is not set; accepting: {challenger_id}")

        dbg_print(f"[CHALLENGE] Accepting challenge from {challenger_name} ({challenger_id})")
        url = f"https://lichess.org/api/challenge/{challenge_id}/accept"
        response = safe_lichess_post(url)
        dbg_print(f"[CHALLENGE] Accept response: {response.status_code if response else 'No response'} | body: {response.text if response else 'N/A'}")
        if response and response.status_code == 200:
            dbg_print(f"[CHALLENGE] Successfully accepted challenge from {challenger_name}")
        else:
            dbg_print(f"[CHALLENGE ERROR] Failed to accept challenge: {response.status_code if response else 'No response'}")
            if response:
                dbg_print(f"[CHALLENGE ERROR] Response: {response.text}")
    except Exception as e:
        dbg_print(f"[CHALLENGE ERROR] Exception in handle_challenge: {e}")
        dbg_print(traceback.format_exc())

# --- GLOBAL LISTENER ---
def listen_to_events():
    dbg_print("[SERVER] Connecting to Lichess event stream...")
    url = "https://lichess.org/api/stream/event"

    while True:
        try:
            response = safe_lichess_stream(url, "events")
            dbg_print("[SERVER] Connected to Lichess event stream.")

            for line in response.iter_lines():
                if not line:
                    continue
                decoded_line = line.decode('utf-8').strip()
                if not decoded_line:
                    continue
                try:
                    event = json.loads(decoded_line)
                except json.JSONDecodeError:
                    # Ignore malformed or heartbeat lines silently
                    continue

                event_type = event.get('type')
                dbg_print(f"[STREAM EVENT] Received: {event_type}")

                if event_type == 'challenge':
                    handle_challenge(event)
                elif event_type == 'gameStart':
                    game_id = event['game']['id']
                    variant_key = event['game']['variant']['key']
                    with active_games_lock:
                        if game_id in active_games:
                            dbg_print(f"[{game_id}] Already handling game, skipping duplicate gameStart")
                            continue
                        active_games.add(game_id)
                    t = threading.Thread(target=play_game, args=(game_id, variant_key), daemon=True)
                    t.start()

        except Exception as conn_err:
            dbg_print(f"[SERVER CRITICAL] {conn_err}. Reconnecting in 10s...")
            dbg_print(traceback.format_exc())
            time.sleep(10)
            continue

# --- ENTRY POINT ---
if __name__ == "__main__":
    try:
        dbg_print("\n=== DEBUG STARTUP ===")
        dbg_print(f"PID={os.getpid()} Python={platform.python_version()} CWD={os.getcwd()}")
        dbg_print(f"PORT={os.environ.get('PORT', 'not set')} ACCEPT_BOTS={ACCEPT_BOTS} LICHHESS_TOKEN_PRESENT={bool(TOKEN and TOKEN != 'YOUR_SECRET_TOKEN_HERE')}")
        dbg_print("Environment vars sample: ", {k: os.environ.get(k) for k in ['PORT', 'ACCEPT_BOTS', 'SF_THREADS', 'SF_HASH']})

        # Start the fake server for Render health checks
        server_thread = threading.Thread(target=run_fake_server, daemon=True)
        server_thread.start()

        # Start the Stockfish worker thread
        worker_thread = threading.Thread(target=stockfish_worker, daemon=True)
        worker_thread.start()

        # Start listening to Lichess events
        listen_to_events()
    except Exception as e:
        dbg_print(f"[FATAL] Bot crashed: {e}")
        dbg_print(traceback.format_exc())
        # prevent immediate container exit during debugging
        while True:
            time.sleep(60)

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
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- CONFIGURATION ---
TOKEN = os.environ.get("LICHESS_TOKEN", "YOUR_SECRET_TOKEN_HERE")
BOT_USERNAME = "AATK_VanguardX"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

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
    print(f"[RENDER] Fake health check server listening on port {port}")
    server.serve_forever()

# --- SAFE REQUESTS ---
def safe_lichess_post(url, json_data=None):
    try:
        response = requests.post(url, headers=HEADERS, json=json_data, timeout=10)
        if response.status_code == 429:
            print("[WARNING] 429 Rate Limit. Backing off...")
            time.sleep(5)
        return response
    except Exception as e:
        print(f"[POST ERROR] {e}")
        return None

def safe_lichess_stream(url, game_id=""):
    backoff = 60
    while True:
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            if response.status_code == 200:
                return response
            elif response.status_code == 429:
                print(f"[{game_id}] 429 error. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                print(f"[{game_id}] Stream failed ({response.status_code}). Retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"[{game_id}] Stream exception: {e}. Retrying in 10s...")
            time.sleep(10)

# --- GAME ACTIONS ---
def send_chat_message(game_id, room, text):
    url = f"https://lichess.org/api/bot/game/{game_id}/chat"
    data = {"room": room, "text": text}
    safe_lichess_post(url, json_data=data)

def make_lichess_move(game_id, move_str):
    url = f"https://lichess.org/api/bot/game/{game_id}/move/{move_str}"
    response = safe_lichess_post(url)
    if response and response.status_code == 200:
        print(f"[{game_id}] Played move: {move_str}")
    elif response:
        print(f"[{game_id}] Move failed ({response.status_code}): {response.text}")

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
    print("[ENGINE] Initializing...")
    stockfish_path = find_engine_binary("stockfish")
    if not stockfish_path:
        print("[CRITICAL] Stockfish not found!")
        return

    fairy_stockfish_path = find_engine_binary("fairy-stockfish")
    try:
        normal_engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
        normal_engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
    except Exception as e:
        print(f"[CRITICAL] Failed to start Stockfish: {e}")
        return

    fairy_engine = None
    if fairy_stockfish_path:
        try:
            fairy_engine = chess.engine.SimpleEngine.popen_uci(fairy_stockfish_path)
            fairy_engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
        except Exception as e:
            print(f"[WARNING] Failed to start Fairy Stockfish: {e}")

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
            print(f"[{game_id}] Engine error: {err}")
            callback(None)
        finally:
            engine_queue.task_done()
#Faah
def play_game(game_id, variant_key):
    try:
        print(f"[GAME START] {game_id} | Variant: {variant_key}")
        moves_played = []
        bot_color = None

        # Fetch initial game state
        game_url = f"https://lichess.org/api/bot/game/stream/{game_id}"
        response = requests.get(game_url, headers=HEADERS, stream=True, timeout=None)

        for line in response.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line.decode('utf-8'))
            except Exception as parse_err:
                print(f"[STREAM ERROR] Failed to parse: {parse_err}")
                continue

            event_type = event.get('type')

            if event_type == 'gameFull':
                state = event.get('state', {})
                moves_played = state.get('moves', '').split()
                bot_color = 'white' if event['white']['id'] == BOT_USERNAME else 'black'
                print(f"[GAME INFO] Bot plays as {bot_color}")

            elif event_type == 'gameState':
                moves_played = event.get('moves', '').split()
                is_bot_turn = (
                    (len(moves_played) % 2 == 0 and bot_color == 'white') or
                    (len(moves_played) % 2 == 1 and bot_color == 'black')
                )

                if is_bot_turn:
                    def handle_move_result(move_uci):
                        if move_uci:
                            make_lichess_move(game_id, move_uci)

                    engine_queue.put((game_id, moves_played, handle_move_result, variant_key))

    except Exception as conn_err:
        print(f"[SERVER CRITICAL] {conn_err}. Reconnecting in 10s...")
        time.sleep(10)

    finally:
        with active_games_lock:
            active_games.discard(game_id)
        print(f"[GAME END] {game_id}")

# --- GLOBAL LISTENER ---
# --- GLOBAL LISTENER ---
def listen_to_events():
    print("[SERVER] Connecting to Lichess event stream...")
    url = "https://lichess.org/api/stream/event"

    while True:
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            print("[SERVER] Connected to Lichess event stream.")

            for line in response.iter_lines():
                if not line:
                    continue
                decoded_line = line.decode('utf-8').strip()
                if not decoded_line:
                    continue
                try:
                    event = json.loads(decoded_line)
                except json.JSONDecodeError:
                    # Ignore empty or malformed lines silently
                    continue

                event_type = event.get('type')
                print(f"[STREAM EVENT] Received: {event_type}")

                if event_type == 'challenge':
                    handle_challenge(event)
                elif event_type == 'gameStart':
                    game_id = event['game']['id']
                    variant_key = event['game']['variant']['key']
                    threading.Thread(target=play_game, args=(game_id, variant_key)).start()

        except Exception as conn_err:
            print(f"[SERVER CRITICAL] {conn_err}. Reconnecting in 10s...")
            time.sleep(10)
# --- ENTRY POINT ---
if __name__ == "__main__":
    try:
        # Start the fake server for Render health checks
        server_thread = threading.Thread(target=run_fake_server, daemon=True)
        server_thread.start()

        # Start the Stockfish worker thread
        worker_thread = threading.Thread(target=stockfish_worker, daemon=True)
        worker_thread.start()

        # Start listening to Lichess events
        listen_to_events()
    except Exception as e:
        print(f"[FATAL] Bot crashed: {e}")
        while True:
            time.sleep(60)


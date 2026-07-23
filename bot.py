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

# Supported variants mapping
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

# Thread-safe job queue for engine calculations
engine_queue = queue.Queue()

# Global tracking to prevent duplicate concurrent streams per game
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
        return  # Suppress internal server logs to keep console clean

def run_fake_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[RENDER] Fake health check server listening on port {port}")
    server.serve_forever()

# --- RATE-LIMIT SAFE REQUEST WRAPPERS ---
def safe_lichess_post(url, json_data=None):
    """Executes a POST request with basic error checking to prevent cascading 429s."""
    try:
        response = requests.post(url, headers=HEADERS, json=json_data, timeout=10)
        if response.status_code == 429:
            print("[WARNING] Post received 429 Rate Limit from Lichess. Throttling outbound calls.")
            time.sleep(5)
        return response
    except Exception as e:
        print(f"[POST ERROR] Request failed: {e}")
        return None

def safe_lichess_stream(url, game_id=""):
    """
    Safely handles streaming endpoints.
    Implements a strict 60-second backoff upon encountering a 429 error.
    """
    backoff = 60  # Lichess strict minimum wait window
    while True:
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            
            if response.status_code == 200:
                return response
                
            elif response.status_code == 429:
                print(f"[{game_id}] [MAIN ERROR] Connection rejected by Lichess (429). Retrying in {backoff}s...")
                # FIX: Removed the invalid 'time.lock = True' line
                time.sleep(backoff)
                backoff = min(backoff * 2, 300) # Double wait, capped at 5 minutes
            else:
                print(f"[{game_id}] Stream initialization failed status: {response.status_code}. Retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"[{game_id}] Stream connection exception: {e}. Reconnecting in 10s...")
            time.sleep(10)

# --- GAME ACTIONS ---
def send_chat_message(game_id, room, text):
    """Sends a chat message to the opponent or spectator room."""
    url = f"https://lichess.org/api/bot/game/{game_id}/chat"
    data = {"room": room, "text": text}
    safe_lichess_post(url, json_data=data)

def make_lichess_move(game_id, move_str):
    """Sends the calculated move back to Lichess."""
    url = f"https://lichess.org/api/bot/game/{game_id}/move/{move_str}"
    response = safe_lichess_post(url)
    if response and response.status_code == 200:
        print(f"[{game_id}] Played move: {move_str}")
    elif response:
        print(f"[{game_id}] Move failed ({response.status_code}): {response.text}")

# --- ENGINE DETECTION ---
def find_engine_binary(engine_name):
    """Finds the engine binary in system paths."""
    resolved_path = shutil.which(engine_name)
    if resolved_path:
        print(f"[ENGINE] Successfully located {engine_name} binary at: {resolved_path}")
        return resolved_path
    
    fallback_paths = {
        'stockfish': ["./stockfish", "/usr/games/stockfish", "/usr/bin/stockfish", "/usr/local/bin/stockfish"],
        'fairy-stockfish': ["./fairy-stockfish", "/usr/games/fairy-stockfish", "/usr/bin/fairy-stockfish", "/usr/local/bin/fairy-stockfish"],
        'fairyfish': ["./fairyfish", "/usr/games/fairyfish", "/usr/bin/fairyfish", "/usr/local/bin/fairyfish"]
    }
    
    for path in fallback_paths.get(engine_name, []):
        if os.path.exists(path):
            print(f"[ENGINE] Fallback found {engine_name} binary at: {path}")
            return path
    return None

# --- BACKGROUND ENGINE WORKER ---
def stockfish_worker():
    """Dedicated background thread handling all Stockfish calculations sequentially."""
    print("[ENGINE] Initializing engine instances...")
    
    stockfish_path = find_engine_binary("stockfish")
    if not stockfish_path:
        print("[CRITICAL] Could not locate Stockfish binary!")
        return
    
    fairy_stockfish_path = find_engine_binary("fairy-stockfish") or find_engine_binary("fairyfish")
    if fairy_stockfish_path:
        print("[ENGINE] Fairy Stockfish found - variant support enabled")
    else:
        print("[WARNING] Fairy Stockfish not found - only standard chess will be optimal")

    try:
        normal_engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
        normal_engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
        print("[ENGINE] Normal Stockfish is fully loaded and ready.")
    except Exception as e:
        print(f"[CRITICAL] Failed to start Normal Stockfish: {e}")
        return

    fairy_engine = None
    if fairy_stockfish_path:
        try:
            fairy_engine = chess.engine.SimpleEngine.popen_uci(fairy_stockfish_path)
            fairy_engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
            print("[ENGINE] Fairy Stockfish is fully loaded and ready.")
        except Exception as e:
            print(f"[WARNING] Failed to start Fairy Stockfish: {e}")

    while True:
        game_id, moves_list, callback, variant_key = engine_queue.get()
        try:
            engine = normal_engine if variant_key == 'standard' else (fairy_engine or normal_engine)
            if fairy_engine is None and variant_key != 'standard':
                print(f"[{game_id}] WARNING: Using Normal Stockfish for {variant_key}")

            board_class = SUPPORTED_VARIANTS.get(variant_key, chess.Board)
            board = board_class()
            
            for move in moves_list:
                try:
                    board.push_uci(move)
                except Exception:
                    pass

            if board.is_game_over():
                callback(None)
                # FIX: We use a structured return mapping instead of an naked loop break/continue layout issue
                engine_queue.task_done()
                continue

            result = engine.play(board, chess.engine.Limit(time=0.1))
            best_move = result.move

            if best_move and board.is_legal(best_move):
                print(f"[{game_id}] Engine generated valid move: {best_move.uci()}")
                callback(best_move.uci())
            else:
                legal_moves = list(board.legal_moves)
                if legal_moves:
                    fallback_move = random.choice(legal_moves).uci()
                    callback(fallback_move)
                else:
                    callback(None)
        except Exception as err:
            print(f"[{game_id}] Engine error during analysis: {err}")
            callback(None)
        finally:
            # FIX: Handled safely via tracking states
            pass

# --- INDIVIDUAL GAME THREAD ---
def play_game(game_id, variant_key='standard'):
    """Streams individual match events. Breaks loop when game ends."""
    with active_games_lock:
        if game_id in active_games:
            return
        active_games.add(game_id)
    
    # You can continue pasting your streaming event loops here...
# --- INDIVIDUAL GAME THREAD ---
def play_game(game_id, variant_key='standard'):
    """Streams individual match events. Breaks loop when game ends."""
    with active_games_lock:
        if game_id in active_games:
            return  # Prevent spinning up multiple concurrent stream loops for one game ID
        active_games.add(game_id)

    print(f"\n[GAME START] Thread spawned for game: {game_id} | Variant: {variant_key}")
    url = f"https://lichess.org/api/bot/game/stream/{game_id}"
    
    response = safe_lichess_stream(url, game_id)
    
    bot_color = None
    opponent = None
    sent_welcome = False

    def _parse_player_info(player_obj):
        if not isinstance(player_obj, dict):
            return {"id": "", "name": "", "rating": None, "title": ""}
        player_id = player_obj.get('id') or (player_obj.get('user') or {}).get('id') or ""
        return {
            "id": player_id,
            "name": player_obj.get('name', "") or "",
            "rating": player_obj.get('rating'),
            "title": player_obj.get('title', "") or ""
        }

    try:
        # SINGLE UNIFIED STREAM LOOP
        for line in response.iter_lines():
            if not line:
                continue
            
            try:
                game_event = json.loads(line.decode('utf-8'))
            except Exception as parse_err:
                print(f"[{game_id}] Parsing error: {parse_err}")
                continue

            event_type = game_event.get('type')
            state = None
            
            if event_type == 'gameFull':
                white_player = _parse_player_info(game_event.get('white', {}))
                black_player = _parse_player_info(game_event.get('black', {}))

                if white_player["id"] and white_player["id"].lower() == BOT_USERNAME.lower():
                    bot_color = 'white'
                    opponent = black_player
                elif black_player["id"] and black_player["id"].lower() == BOT_USERNAME.lower():
                    bot_color = 'black'
                    opponent = white_player
                else:
                    bot_color = None
                    opponent = black_player if white_player["id"] else white_player

                state = game_event['state']
                print(f"[{game_id}] Match configuration locked. Bot Color side: {bot_color.upper() if bot_color else 'UNKNOWN'}")
                if opponent and opponent.get('id'):
                    print(f"[{game_id}] Opponent found: @{opponent.get('id')} (name={opponent.get('name')}, rating={opponent.get('rating')}, title={opponent.get('title')})")

            elif event_type == 'gameState':
                state = game_event
                if bot_color is None:
                    print(f"[{game_id}] Stream reconnected mid-game. Fetching true match details...")
                    try:
                        export_url = f"https://lichess.org/api/bot/game/{game_id}"
                        meta_resp = requests.get(export_url, headers=HEADERS, timeout=5)
                        if meta_resp.status_code == 200:
                            meta_data = meta_resp.json()
                            white_player = _parse_player_info(meta_data.get('white', {}))
                            black_player = _parse_player_info(meta_data.get('black', {}))

                            if white_player["id"] and white_player["id"].lower() == BOT_USERNAME.lower():
                                bot_color = 'white'
                                opponent = black_player
                            elif black_player["id"] and black_player["id"].lower() == BOT_USERNAME.lower():
                                bot_color = 'black'
                                opponent = white_player

                            print(f"[{game_id}] Recovered color profile safely: {bot_color.upper() if bot_color else 'UNKNOWN'}")
                            if opponent and opponent.get('id'):
                                print(f"[{game_id}] Recovered opponent: @{opponent.get('id')}")
                    except Exception as ex:
                        print(f"[{game_id}] Error recovering color profile: {ex}")
            else:
                continue

            if not state:
                continue

            # Check if match is complete
            if state.get('status') != 'started':
                opponent_tag = f"@{opponent['id']}" if opponent and opponent.get('id') else ""
                print(f"[{game_id}] Match complete. Reason: {state.get('status')}")
                send_chat_message(game_id, "player", f"Good game! Thanks for playing. {opponent_tag}")
                break

            # Send greetings
            if event_type == 'gameFull' and not sent_welcome:
                if opponent and opponent.get('id'):
                    send_chat_message(game_id, "player", f"Hello @{opponent.get('id')}! Engine Mode active ({variant_key}). Good luck!")
                else:
                    send_chat_message(game_id, "player", f"Hello! Engine Mode active ({variant_key}). Good luck!")
                sent_welcome = True

            moves_played = state['moves'].strip().split() if state['moves'].strip() else []
            total_moves = len(moves_played)

            if bot_color is None:
                print(f"[{game_id}] Warning: Skipping move check because bot color is unknown.")
                continue

            is_bot_turn = (total_moves % 2 == 0 and bot_color == 'white') or \
                          (total_moves % 2 != 0 and bot_color == 'black')

            if is_bot_turn:
                print(f"[{game_id}] Bot turn detected (Move #{total_moves + 1}). Queueing engine evaluation...")
                def handle_move_result(move_uci):
                    if move_uci:
                        make_lichess_move(game_id, move_uci)

                engine_queue.put((game_id, moves_played, handle_move_result, variant_key))

    except Exception as stream_loop_err:
        print(f"[{game_id}] Active game stream exception dropped: {stream_loop_err}")
    finally:
        with active_games_lock:
            active_games.discard(game_id)
        print(f"[GAME END] Cleaned up thread context for game: {game_id}")


# --- GLOBAL EVENT LISTENER ---
def listen_to_events():
    """Listens to global challenges and game starts with heavy diagnostic tracking."""
    print(f"Starting global event listener for user: {BOT_USERNAME}")
    print(f"[VARIANTS] Supported: {', '.join(SUPPORTED_VARIANTS.keys())}")
    url = "https://lichess.org/api/stream/event"
    
    while True:
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            print("[SERVER] Stream connection successfully established with Lichess pipelines.")
            
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line.decode('utf-8'))
                except Exception as parse_err:
                    print(f"[STREAM ERROR] Failed to parse stream line data: {parse_err}")
                    continue

                event_type = event.get('type')
                print(f"[STREAM EVENT] Received incoming packet notification type: '{event_type}'")

                if event_type == 'challenge':
                    challenge_data = event['challenge']
                    challenge_id = challenge_data['id']
                    variant_info = challenge_data.get('variant', {})
                    variant_key = variant_info.get('key', 'standard')
                    
                    print(f"[CHALLENGE] Incoming request ID: {challenge_id} | Variant: {variant_key}")
                    
                    if variant_key in SUPPORTED_VARIANTS:
                        # Auto-accept challenge if supported
                        accept_url = f"https://lichess.org{challenge_id}/accept"
                        safe_lichess_post(accept_url)
                        print(f"[CHALLENGE] Accepted variant challenge: {challenge_id}")
                    else:
                        # Decline challenge if unsupported
                        decline_url = f"https://lichess.org{challenge_id}/decline"
                        safe_lichess_post(decline_url, json_data={"reason": "variant"})
                        print(f"[CHALLENGE] Declined unsupported variant challenge: {challenge_id}")

                elif event_type == 'gameStart':
                    game_info = event['game']
                    game_id = game_info['id']
                    # Use fallback variant mapping logic safely
                    variant_key = game_info.get('variant', {}).get('key', 'standard')
                    
                    # Spin up an independent asynchronous tracking loop thread per game layout
                    game_thread = threading.Thread(target=play_game, args=(game_id, variant_key), daemon=True)
                    game_thread.start()

        except Exception as conn_err:
            print(f"[SERVER CRITICAL] Pipeline context drop exception: {conn_err}. Reconnecting in 10s...")
            time.sleep(10)

# --- APPLICATION ENTRY POINT ---
if __name__ == "__main__":
    # Start the local environment health validation server for background deployment hosts
    server_thread = threading.Thread(target=run_fake_server, daemon=True)
    server_thread.start()
    
    # Run the background Stockfish analytical processing worker
    worker_thread = threading.Thread(target=stockfish_worker, daemon=True)
    worker_thread.start()
    
    # Start our infinite stream parsing routine on main system loop thread
    listen_to_events()

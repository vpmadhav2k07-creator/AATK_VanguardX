# 1. ALL IMPORTS MUST GO FIRST AT THE VERY TOP
import chess
import chess.engine
import http.server
import json
import os
import queue
import random
import requests
import shutil
import socketserver
import threading
import time

# 2. THE RENDER FREE TIER ALIVE TRICK
def run_fake_server():
    """Starts a minimal HTTP server to satisfy Render's port binding requirement."""
    port = int(os.environ.get("PORT", 8080))
    # Custom handler suppresses excessive asset logs in your Render dashboard
    class CleanHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        def log_message(self, format, *args):
            return  # Prevents filling log files with ping records

    try:
        with socketserver.TCPServer(("0.0.0.0", port), CleanHandler) as httpd:
            print(f"[RENDER] Web server successfully bound to port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"[RENDER CRITICAL] Failed to start mock web server: {e}")

# Fire up the fake web server in a daemon thread so it runs concurrently with the bot
threading.Thread(target=run_fake_server, daemon=True).start()

# 3. LICHESS CONFIGURATION & CONSTANTS
TOKEN = os.environ.get("LICHESS_TOKEN", "YOUR_SECRET_TOKEN_HERE")
BOT_USERNAME = "AATK_VanguardX"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Thread-safe job queue for engine calculations
engine_queue = queue.Queue()

# --- LICHESS HELPER FUNCTIONS ---

def send_chat_message(game_id, room, text):
    """Sends a chat message to the opponent or spectator room."""
    url = f"https://lichess.org/api/bot/game/{game_id}/chat"
    data = {"room": room, "text": text}
    try:
        requests.post(url, headers=HEADERS, json=data, timeout=5)
    except Exception as e:
        print(f"[{game_id}] Failed to send chat: {e}")

def make_lichess_move(game_id, move_str):
    """Sends the calculated move back to Lichess."""
    url = f"https://lichess.org/api/bot/game/{game_id}/move/{move_str}"
    try:
        response = requests.post(url, headers=HEADERS, timeout=5)
        if response.status_code == 200:
            print(f"[{game_id}] Played move: {move_str}")
        else:
            print(f"[{game_id}] Move failed ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[{game_id}] Error posting move: {e}")

# --- STOCKFISH WORKER THREAD ---

def stockfish_worker():
    """Dedicated background thread handling all Stockfish calculations sequentially."""
    print("[ENGINE] Initializing local Stockfish engine instance...")
    
    resolved_path = shutil.which("stockfish")
    
    if resolved_path:
        print(f"[ENGINE] Successfully located Stockfish binary at: {resolved_path}")
    else:
        possible_paths = ["/usr/games/stockfish", "/usr/bin/stockfish", "./stockfish", "/usr/local/bin/stockfish"]
        for path in possible_paths:
            if os.path.exists(path):
                resolved_path = path
                print(f"[ENGINE] Fallback found Stockfish binary at: {resolved_path}")
                break
                
    if not resolved_path:
        print("[CRITICAL] Could not locate Stockfish binary anywhere in the system path!")
        return

    try:
        engine = chess.engine.SimpleEngine.popen_uci(resolved_path)
        engine.configure({"Skill Level": 20, "Hash": 64, "Threads": 1})
        print("[ENGINE] Stockfish is fully loaded and ready to accept match jobs.")
    except Exception as e:
        print(f"[CRITICAL] Failed to start Stockfish engine instance: {e}")
        return

    while True:
        game_id, moves_list, callback = engine_queue.get()
        try:
            board = chess.Board()
            for move in moves_list:
                try:
                    board.push_uci(move)
                except Exception:
                    pass

            if board.is_game_over():
                callback(None)
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
                    print(f"[{game_id}] Panic fallback triggered. Selected move: {fallback_move}")
                    callback(fallback_move)
                else:
                    callback(None)

        except Exception as err:
            print(f"[{game_id}] Engine error during analysis: {err}")
            callback(None)
        finally:
            engine_queue.task_done()

# --- GAME STREAM LOGIC ---

def play_game(game_id):
    """Streams individual match events. Breaks loop when game ends."""
    print(f"\n[GAME START] Thread spawned for game: {game_id}")
    url = f"https://lichess.org/api/bot/game/stream/{game_id}"
    
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
    except Exception as e:
        print(f"[{game_id}] Stream connection failed: {e}")
        return
        
    bot_color = None
    sent_welcome = False
    for line in response.iter_lines():
        if not line:
            continue
            
        # Add these two lines here to protect the game loop as well!
        decoded_line = line.decode('utf-8').strip()
        if not decoded_line:
            continue
            
        try:
            game_event = json.loads(decoded_line)
        except Exception:
            continue

            
        try:
            game_event = json.loads(line.decode('utf-8'))
        except Exception:
            continue

        event_type = game_event.get('type')
        
        if event_type == 'gameFull':
            white_player = game_event.get('white', {})
            white_id = white_player.get('id', '') if isinstance(white_player, dict) else ''
            
            if white_id.lower() == BOT_USERNAME.lower():
                bot_color = 'white'
            else:
                bot_color = 'black'
                
            state = game_event['state']
            print(f"[{game_id}] Match configuration locked. Bot Color side: {bot_color.upper()}")
            
        elif event_type == 'gameState':
            state = game_event
            if bot_color is None:
                print(f"[{game_id}] Stream reconnected mid-game. Fetching true match details...")
                try:
                    export_url = f"https://lichess.org/api/bot/game/{game_id}"
                    export_headers = {**HEADERS, "Accept": "application/json"}
                    meta_resp = requests.get(export_url, headers=export_headers, timeout=5)
                    
                    if meta_resp.status_code == 200:
                        meta_data = meta_resp.json()
                        w_id = meta_data.get('players', {}).get('white', {}).get('user', {}).get('id', '')
                        bot_color = 'white' if w_id.lower() == BOT_USERNAME.lower() else 'black'
                        print(f"[{game_id}] Recovered color profile safely: {bot_color.upper()}")
                    else:
                        print(f"[{game_id}] Export API returned status code: {meta_resp.status_code}")
                except Exception as ex:
                    print(f"[{game_id}] Error recovering color profile: {ex}")
        else:
            continue

        if state.get('status') != 'started':
            print(f"[{game_id}] Match complete. Reason: {state.get('status')}")
            send_chat_message(game_id, "player", "Good game! Thanks for playing.")
            break

        if event_type == 'gameFull' and not sent_welcome:
            send_chat_message(game_id, "player", "Hello! Fast Engine Mode active. Good luck!")
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
                else:
                    print(f"[{game_id}] No valid move returned by engine framework.")

            engine_queue.put((game_id, moves_played, handle_move_result))
def main_loop():
    """Monitors the Lichess event stream for incoming challenges and handles rated permissions."""
    print("[MAIN] Establishing primary event stream connection to Lichess API...")
    url = "https://lichess.org/api/stream/event"
    
    # ⚠️ ADD YOUR USERNAMES HERE IN LOWERCASE ⚠️
    ALLOWED_RATED_PLAYERS = ["mahadevvp-2012", "chessrocker2"]
    
    # Initialize engine background worker thread
    threading.Thread(target=stockfish_worker, daemon=True).start()
    
    while True:
        try:
            response = requests.get(url, headers=HEADERS, stream=True, timeout=None)
            if response.status_code != 200:
                print(f"[MAIN ERROR] Connection rejected by Lichess ({response.status_code}). Retrying in 10s...")
                time.sleep(10)
                continue
                
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line.decode('utf-8'))
                except Exception:
                    continue
                
                event_type = event.get('type')
                
                if event_type == 'challenge':
                    challenge_id = event['challenge']['id']
                    is_rated = event['challenge'].get('rated', False)
                    challenger = event['challenge'].get('challenger', {}).get('id', 'unknown').lower()
                    
                    # RATED GAME EVALUATION
                    if is_rated:
                        # If the user is on the whitelist, allow the rated game!
                        if challenger in ALLOWED_RATED_PLAYERS:
                            print(f"[CHALLENGE ACCEPTED] Accepting RATED match from Whitelisted friend: '{challenger}'")
                            accept_url = f"https://lichess.org/api/challenge/{challenge_id}/accept"
                            requests.post(accept_url, headers=HEADERS)
                        # If a stranger requests a rated game, decline it
                        else:
                            print(f"[CHALLENGE BANNED] Declined rated challenge from stranger: '{challenger}'")
                            decline_url = f"https://lichess.org/api/challenge/{challenge_id}/decline"
                            requests.post(decline_url, headers=HEADERS, json={"reason": "casualOnly"})
                    
                    # CASUAL GAME EVALUATION
                    else:
                        print(f"[CHALLENGE ACCEPTED] Accepting casual match from '{challenger}'")
                        accept_url = f"https://lichess.org/api/challenge/{challenge_id}/accept"
                        requests.post(accept_url, headers=HEADERS)
                        
                elif event_type == 'gameStart':
                    game_id = event['game']['id']
                    # Process active match inside an independent background thread       
                    threading.Thread(target=play_game, args=(game_id,), daemon=True).start()

        except Exception as global_err:
            print(f"[MAIN SYSTEM EXCEPTION] Connection dropped: {global_err}. Reconnecting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main_loop()

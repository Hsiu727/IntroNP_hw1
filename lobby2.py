import sqlite3
import bcrypt
import json
import socket
import threading
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone


# ===== 時區與時間工具 =====
TZ_TAIPEI = timezone(timedelta(hours=8))
def now_tz():
    # 以 ISO8601 文字存進 SQLite（跨平台、免型態轉換）
    return datetime.now(TZ_TAIPEI).isoformat()

# ===== SQLite 設定 =====
DB_DIR = Path(__file__).resolve().parent / "storage"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "users.db"

def with_db():
    """
    每次呼叫回傳一個新的連線（thread-safe）。
    row_factory 設成 Row，方便以字典方式取欄位。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()

def ensure_schema():
    conn, cur = with_db()
    try:
        # 使用者表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
            """
        )
        # 狀態表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users_status (
                username     TEXT PRIMARY KEY,
                login_count  INTEGER NOT NULL DEFAULT 0,
                wins         INTEGER NOT NULL DEFAULT 0,
                losses       INTEGER NOT NULL DEFAULT 0,
                last_seen    TEXT NOT NULL,
                online       INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

# ===== 執行時狀態 =====
PLAYER_NUM = 0
player_lock = threading.Lock()

active_sessions = {}   # username -> {"conn": socket, "last_seen": ts}
sessions_lock = threading.Lock()

HEARTBEAT_TTL = 15  # 秒

# ===== Session/狀態維護 =====
def cleanup_inactive_sessions():
    while True:
        time.sleep(10)
        now = time.time()
        with sessions_lock:
            inactive = [
                user for user, sess in active_sessions.items()
                if now - sess["last_seen"] > HEARTBEAT_TTL
            ]
        for user in inactive:
            print(f"[CLEANUP] {user} inactive, marking offline.")
            mark_offline(user)
            clear_active(user)

def reset_all_online_flags():
    conn, cur = with_db()
    try:
        cur.execute("UPDATE users_status SET online = 0")
        conn.commit()
    finally:
        cur.close()
        conn.close()

def mark_offline(username: str):
    conn, cur = with_db()
    try:
        cur.execute(
            "UPDATE users_status SET online=0, last_seen=? WHERE username=?",
            (now_tz(), username),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def normalize_status(row: Optional[sqlite3.Row]):
    if not row:
        return None
    out = dict(row)
    # last_seen 已經是 ISO8601 字串，直接回傳即可
    return out

def get_status(username: str):
    conn, cur = with_db()
    try:
        cur.execute("SELECT * FROM users_status WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            # 建立初始狀態
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (?, 0, 0, 0, ?, 0)",
                (username, now_tz()),
            )
            conn.commit()
            cur.execute("SELECT * FROM users_status WHERE username=?", (username,))
            row = cur.fetchone()
        return row
    finally:
        cur.close()
        conn.close()

def update_status(username: str, delta=None, online=None):
    if delta is None:
        delta = {}
    conn, cur = with_db()
    try:
        cur.execute("SELECT * FROM users_status WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (?, 0, 0, 0, ?, 0)",
                (username, now_tz()),
            )
            conn.commit()
            cur.execute("SELECT * FROM users_status WHERE username=?", (username,))
            row = cur.fetchone()

        wins   = int(row["wins"])   + int(delta.get("wins", 0))
        losses = int(row["losses"]) + int(delta.get("losses", 0))
        on     = int(row["online"]) if online is None else (1 if online else 0)

        cur.execute(
            "UPDATE users_status SET wins=?, losses=?, last_seen=?, online=? WHERE username=?",
            (wins, losses, now_tz(), on, username),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def inc_login_count_and_online(username: str):
    conn, cur = with_db()
    try:
        cur.execute("SELECT * FROM users_status WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (?, 1, 0, 0, ?, 1)",
                (username, now_tz()),
            )
        else:
            cur.execute(
                "UPDATE users_status "
                "SET login_count=login_count+1, online=1, last_seen=? "
                "WHERE username=?",
                (now_tz(), username),
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def is_active(username: str) -> bool:
    with sessions_lock:
        sess = active_sessions.get(username)
        if not sess:
            return False
        return (time.time() - sess["last_seen"]) <= HEARTBEAT_TTL

def set_active(username: str, conn_sock: socket.socket):
    with sessions_lock:
        active_sessions[username] = {"conn": conn_sock, "last_seen": time.time()}

def refresh_active(username: str):
    with sessions_lock:
        sess = active_sessions.get(username)
        if sess:
            sess["last_seen"] = time.time()

def clear_active(username: str):
    with sessions_lock:
        if username in active_sessions:
            del active_sessions[username]

# ===== 連線處理 =====
def handle_client(conn: socket.socket, addr):
    global PLAYER_NUM
    with player_lock:
        PLAYER_NUM += 1
        current_players = PLAYER_NUM
    print(f"[+] Player connected from {addr}")
    print(f"[LOBBY SERVER] Current players: {current_players}")

    username_bound = None

    try:
        while True:
            data = conn.recv(1024).decode('utf-8')
            if not data:
                break
            # 你的 client 用的是「每次 send 一個 JSON」，這裡沿用一次解析一個
            try:
                msg = json.loads(data)
            except Exception as e:
                print(f"[!] JSON parse error from {addr}: {e} | raw={data!r}")
                continue

            action   = msg.get("action")
            username = msg.get("username")
            password = msg.get("password")

            if username:
                refresh_active(username)

            if action == "register":
                try:
                    conn_db, cursor = with_db()
                    cursor.execute("SELECT 1 FROM users WHERE username=?", (username,))
                    if cursor.fetchone():
                        conn.sendall(b"REGISTER_FAILED_USER_EXISTS")
                    else:
                        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
                        cursor.execute(
                            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                            (username, hashed_pw.decode(), now_tz())
                        )
                        conn_db.commit()
                        _ = get_status(username)  # 確保 users_status 也建好
                        conn.sendall(b"REGISTER_SUCCESS")
                except Exception as e:
                    print(f"[!] register error: {e}")
                    conn.sendall(b"REGISTER_FAILED")
                finally:
                    try:
                        cursor.close(); conn_db.close()
                    except:
                        pass

            elif action == "login":
                try:
                    conn_db, cursor = with_db()
                    cursor.execute("SELECT username, password_hash FROM users WHERE username=?", (username,))
                    user = cursor.fetchone()
                    if not user:
                        conn.sendall(b"LOGIN_FAILED_NO_USER")
                    elif not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode()):
                        conn.sendall(b"LOGIN_FAILED_WRONG_PASSWORD")
                    else:
                        # 重複登入策略：拒絕新連線
                        if is_active(username):
                            conn.sendall(b"LOGIN_FAILED_DUPLICATE")
                        else:
                            set_active(username, conn)
                            username_bound = username
                            inc_login_count_and_online(username)
                            status = normalize_status(get_status(username))
                            resp = {"type": "LOGIN_SUCCESS", "status": status}
                            conn.sendall(json.dumps(resp).encode("utf-8"))
                except Exception as e:
                    print(f"[!] login error: {e}")
                    conn.sendall(b"LOGIN_FAILED")
                finally:
                    try:
                        cursor.close(); conn_db.close()
                    except:
                        pass

            elif action == "status_report":
                try:
                    st = msg.get("status", {})  # wins_delta / losses_delta / in_game...
                    delta = {
                        "wins":   int(st.get("wins_delta", 0)),
                        "losses": int(st.get("losses_delta", 0)),
                    }
                    update_status(username, delta=delta, online=True)
                    refresh_active(username)
                    # 可選：回覆 OK，或省略
                    # conn.sendall(b"OK")
                except Exception as e:
                    print(f"[!] status_report error: {e}")

            elif action == "logout":
                try:
                    if username:
                        mark_offline(username)
                        clear_active(username)
                        if username_bound == username:
                            username_bound = None
                    conn.sendall(b"LOGOUT_OK")
                except Exception as e:
                    print(f"[!] logout error: {e}")

            else:
                conn.sendall(b"ERROR_UNKNOWN_ACTION")

    except Exception as e:
        print(f"[!] Connection error with {addr}: {e}")

    finally:
        # 連線關閉，若還綁定使用者，清 session 與標記離線（避免殭屍 session）
        if username_bound:
            mark_offline(username_bound)
            clear_active(username_bound)

        try:
            conn.close()
        finally:
            with player_lock:
                PLAYER_NUM -= 1
                current_players = PLAYER_NUM
            print(f"[-] Disconnected: {addr}, Current players: {current_players}")

# ===== 入口點 =====
def start_server(host='140.113.17.11', port=15000):
    ensure_schema()
    reset_all_online_flags()
    threading.Thread(target=cleanup_inactive_sessions, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 允許快速重綁（Demo 時換埠/重啟很方便）
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    print(f"[Lobby] DB @ {DB_PATH}")
    print(f"[LOBBY SERVER] Listening on {host}:{port} (TCP, NDJSON)")

    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        thread.start()

if __name__ == '__main__':
    start_server()

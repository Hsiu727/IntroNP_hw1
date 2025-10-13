import mysql.connector
import bcrypt
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
TZ_TAIPEI = timezone(timedelta(hours=8))
def now_tz():
    return datetime.now(TZ_TAIPEI)

db_config = {
    "host":"localhost",
    "user":"root",
    "password":"@Flask12345",
    "database":"DB"
}

PLAYER_NUM=0
player_lock = threading.Lock()

active_sessions = {}
sessions_lock = threading.Lock()

HEARTBEAT_TTL = 15

def with_db():
    conn = mysql.connector.connect(**db_config)
    return conn, conn.cursor(dictionary=True)

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
        cur.execute("UPDATE users_status SET online=0")
        conn.commit()
    finally:
        cur.close()
        conn.close()

def mark_offline(username):
    # DB: online=0, last_seen=NOW()
    conn, cur = with_db()
    try:
        cur.execute(
            "UPDATE users_status SET online=0, last_seen=%s WHERE username=%s",
            (now_tz(), username),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def normalize_status(row):
    if not row:
        return None
    out = dict(row)
    ls = out.get("last_seen")
    if isinstance(ls, datetime):
        out["last_seen"] = ls.isoformat()
    return out

def get_status(username):
    conn, cur = with_db()
    try:
        cur.execute("SELECT * FROM users_status WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            # 建立初始狀態
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (%s, 0, 0, 0, %s, 0)",
                (username, now_tz()
),
            )
            conn.commit()
            cur.execute("SELECT * FROM users_status WHERE username=%s", (username,))
            row = cur.fetchone()
        return row
    finally:
        cur.close()
        conn.close()

def update_status(username, delta=None, online=None):
    # delta: 可包含 wins/losses 的增量（可正可零）
    if delta is None:
        delta = {}
    conn, cur = with_db()
    try:
        # 先取目前狀態
        cur.execute("SELECT * FROM users_status WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            # 不存在就建立
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (%s, 0, 0, 0, %s, 0)",
                (username, now_tz()),
            )
            conn.commit()
            cur.execute("SELECT * FROM users_status WHERE username=%s", (username,))
            row = cur.fetchone()

        wins = row["wins"] + int(delta.get("wins", 0))
        losses = row["losses"] + int(delta.get("losses", 0))
        last_seen = datetime.utcnow()
        on = row["online"] if online is None else (1 if online else 0)

        cur.execute(
            "UPDATE users_status SET wins=%s, losses=%s, last_seen=%s, online=%s WHERE username=%s",
            (wins, losses, last_seen, on, username),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def inc_login_count_and_online(username):
    conn, cur = with_db()
    try:
        cur.execute("SELECT * FROM users_status WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users_status (username, login_count, wins, losses, last_seen, online) "
                "VALUES (%s, 1, 0, 0, %s, 1)",
                (username, now_tz()),
            )
        else:
            cur.execute(
                "UPDATE users_status SET login_count=login_count+1, online=1, last_seen=%s WHERE username=%s",
                (now_tz(), username),
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def is_active(username):
    with sessions_lock:
        sess = active_sessions.get(username)
        if not sess:
            return False
        return (time.time() - sess["last_seen"]) <= HEARTBEAT_TTL

def set_active(username, conn):
    with sessions_lock:
        active_sessions[username] = {"conn": conn, "last_seen": time.time()}

def refresh_active(username):
    with sessions_lock:
        sess = active_sessions.get(username)
        if sess:
            sess["last_seen"] = time.time()

def clear_active(username):
    with sessions_lock:
        if username in active_sessions:
            del active_sessions[username]

def handle_client(conn, addr):
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
            print(f"Received data from {addr}")
            if not data:
                break

            try:
                msg = json.loads(data)
            except Exception as e:
                print(f"[!] JSON parse error: {e}")
                continue

            action = msg.get("action")
            username = msg.get("username")
            password = msg.get("password")

            if username:
                refresh_active(username)

            if action == "register":
                try:
                    conn_db, cursor = with_db()
                    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
                    if cursor.fetchone():
                        conn.sendall(b"REGISTER_FAILED_USER_EXISTS")
                    else:
                        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
                        cursor.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, hashed_pw.decode()))
                        conn_db.commit()
                        _ = get_status(username)
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
                    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
                    user = cursor.fetchone()
                    if not user:
                        conn.sendall(b"LOGIN_FAILED_NO_USER")
                    elif not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode()):
                        conn.sendall(b"LOGIN_FAILED_WRONG_PASSWORD")
                    else:
                        # handle duplicate
                        if is_active(username):
                            conn.sendall(b"LOGIN_FAILED_DUPLICATE")
                        else:
                            set_active(username, conn)
                            username_bound = username
                            inc_login_count_and_online(username)
                            status = normalize_status(get_status(username))
                            resp = {"type":"LOGIN_SUCCESS", "status": status}
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
                # 週期回報：更新 db 狀態與 last_seen/online
                try:
                    st = msg.get("status", {})  # wins/losses/in_game...
                    delta = {
                        "wins": int(st.get("wins_delta", 0)),
                        "losses": int(st.get("losses_delta", 0)),
                    }
                    update_status(username, delta=delta, online=True)
                    # 刷新 active session
                    refresh_active(username)
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
                        # 即使錯誤仍關閉

            else:
                conn.sendall(b"ERROR_UNKNOWN_ACTION")
    
    except Exception as e:
        print(f"[!] Error: {e}")

    finally:
        # 連線關閉，若還綁定使用者，清 session 與標記離線（避免殭屍 session）
        if username_bound:
            mark_offline(username_bound)
            clear_active(username_bound)

        conn.close()
        with player_lock:
            PLAYER_NUM -= 1
            current_players = PLAYER_NUM
        print(f"[-] Disconnected: {addr}, Current players: {current_players}")

def start_server(host='0.0.0.0', port=5000):
    reset_all_online_flags()
    threading.Thread(target=cleanup_inactive_sessions, daemon=True).start()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((host, port))
    server.listen()
    print(f"[LOBBY SERVER] Listening on {host}:{port}")

    while True:
        conn, addr = server.accept()
        print(f"[LOBBY SERVER] Connected by {str(addr)}")
        thread = threading.Thread(target=handle_client, args=(conn, addr))
        thread.start()

# 啟動 lobby server
# === 啟動 Flask Server ===
if __name__ == '__main__':
    start_server()
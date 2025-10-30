import socket
import json
import time

from tt import pls, GameUI, send_json_line, recv_json_line, start_status_reporter, safe_logout

HOST = '140.113.17.11'
PORT = 16000
UDP_PORT = 10002
RECV_STEP = 2.0
WAIT_WINDOW = 15.0

def client_game(conn, lobby_sock, username):
    ui = GameUI()
    B = pls()
    my_role = username
    op_name = None
    buf = b""
    target_wins = 3
    rnd = 1

    try:
        while True:
            msg, buf = recv_json_line(conn, buf)
            t = msg.get("type")
            
            if t == "START":
                target_wins = int(msg.get("target_wins", 3))
                op_name = msg.get("name")
                ui.show_game_start(target_wins)
            elif t == "MOVE":  # 收到對手牌
                ui.show_round(rnd)
                ui.show_cards(B.cards)
                ui.show_opponents_cards(op_name, msg.get("cards"))
                while True:
                    try:
                        my_cards = ui.get_player_move()
                        if _validate_move(my_cards, B.cards):
                            # 發送自己的出牌
                            send_json_line(conn, {"type": "MOVE", "cards": my_cards})
                            # 移除使用的牌
                            B.use_cards(my_cards)
                            break
                    except ValueError:
                        print("請輸入數字")
            elif t == "ROUND_RESULT":
                a_play = msg["a_play"]
                b_play = msg["b_play"]
                winner = msg["winner"]
                a_wins = msg["a_wins"]
                b_wins = msg["b_wins"]

                ui.show_round_result(b_play, a_play, winner, b_wins, a_wins)
                rnd += 1
            elif t == "GAME_OVER":
                ui.show_game_over(msg.get('a_wins'), msg.get('b_wins'), 
                                msg.get("winner"), my_role)
                try:
                    winner = msg.get("winner")
                    wins_delta   = 1 if winner == username else 0
                    losses_delta = 1 if winner == op_name else 0
                    payload = {
                        "action": "status_report",
                        "username": username,
                        "status": {
                            "wins_delta": wins_delta,
                            "losses_delta": losses_delta,
                            "in_game": False
                        }
                    }
                    lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
                except Exception:
                    pass

                    # === 雙向 REMATCH：我方表態 + 5 秒內等待對方 REMATCH（或直接等到新的 START） ===
                ans = input("Rematch? (y/n): ").strip().lower()
                if ans == "y":
                    send_json_line(conn, {"type": "REMATCH"})
                    # 在 5 秒內等待：最好情況是先收到 REMATCH，再收到新的 START（由 A 發）
                    # 為了更 robust，也接受直接收到 START（代表主機端已經確認雙方都同意）
                    buf2 = b""
                    try:
                        conn.settimeout(5.0)
                        while True:
                            msg2, buf2 = recv_json_line(conn, buf2)
                            if msg2.get("type") == "REMATCH":
                                print("雙方都同意再來一局，重置牌庫與比分。")
                                B = pls()
                                rnd = 1
                                conn.settimeout(None)
                                break
                            # 其他忽略
                    except Exception:
                        # 超時仍未收到對方 REMATCH/START → 結束
                        print("對方未在 5 秒內同意或未開始新局，返回等待。")
                        break
                    finally:
                        try:
                            conn.settimeout(None)
                        except Exception:
                            pass
                    # 若 5 秒內只收到對方的 REMATCH，但尚未收到 START，也讓出 loop 等下一個訊息（主迴圈繼續）
                    continue
                else:
                    print("你選擇不再來一局，結束並返回等待。")
                    break
            elif t == "DISCONNECT":
                # 對手主動告知中斷/離線
                print("[!] Peer requested disconnect:", msg.get("reason", ""))
                break  # 回到 LISTEN        
        
            else:
                pass
    except KeyboardInterrupt:
        try:
            send_json_line(conn, {"type": "DISCONNECT", "reason": "KeyboardInterrupt"})
        except Exception:
            pass
        # 回報離線
        safe_logout(lobby_sock, username)
        print("\n[!] You pressed Ctrl+C. Disconnected from peer and logged out.")

def _validate_move(cards, available_cards):
    if len(cards) not in (1,2):
        print("必須出 1 或 2 張牌")
        return False
    if any(c<1 or c>7 for c in cards):
        print("牌的數字必須在 1-7 之間")
        return False
    if len(cards)==2 and cards[0]==cards[1]:
        print("不能出相同的牌")
        return False
    if any(c not in available_cards for c in cards):
        print("你沒有這張牌")
        return False
    return True

def waiting_op(udp, lobby_sock, username):

    state = "LISTEN"
    invite_from = None
    deadline = 0.0

    while True:
        try:
            if state == "LISTEN":
                print(f"[WAITING] Listening UDP on port {UDP_PORT}")
                data, addr = udp.recvfrom(1024)
                msg = json.loads(data.decode())
                
                if msg["type"] == "SEARCH":
                    reply = {"type": "REPLY", "name": username}
                    udp.sendto(json.dumps(reply).encode(), addr)

                elif msg["type"] == "INVITE":
                    print(f"Got invitation from {msg['from']}")
                    choice = input("Accept? (y/n): ").strip().lower()
                    resp = {"type":"ACCEPT"} if choice=="y" else {"type":"DECLINE"}
                    udp.sendto(json.dumps(resp).encode(), addr)

                    if choice == "y":
                        state = "INVITE_PENDING"
                        invite_from = addr
                        deadline = time.time() + WAIT_WINDOW
                        udp.settimeout(RECV_STEP)
                    else:
                        pass

            elif state == "INVITE_PENDING":
                try:
                    data, addr = udp.recvfrom(1024)
                except socket.timeout:
                    if time.time() >= deadline:
                        print("[B] Invite window expired. Back to LISTEN.")
                        state = "LISTEN"
                        invite_from = None
                        udp.settimeout(None)        # 回到阻塞等待
                    continue    
                        
                info = json.loads(data.decode())
                if addr != invite_from:
                    continue
                if info.get("type") == "TCP_INFO":
                    tcp_port = int(info["port"])
                    print(f"[B] Connecting to A via TCP {addr[0]}:{tcp_port} ...")
                    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    tcp.connect((addr[0], tcp_port))
                    try:
                        client_game(tcp, lobby_sock=lobby_sock, username=username, )
                    except ConnectionError:
                        print("主機斷線，遊戲結束。")
                    finally:
                        tcp.close()

                    print("[B] Back to LISTEN and waiting new invitations.")
                    state = "LISTEN"
                    invite_from = None
                    udp.settimeout(None)
                    continue  # 重新等待下一波 SEARCH/INVITE

                elif info.get("type") == "CANCEL":
                    print("[B] A cancelled invite. Back to LISTEN.")
                    state = "LISTEN"
                    invite_from = None
                    udp.settimeout(None)
        except KeyboardInterrupt:
            print("\nBYE!")
            safe_logout(lobby_sock, username)
            break

def sign_in(client):
    while True:
        print("Select your action\tA. register\tB. login\tQ. quit")
        act = input("> ").strip().lower()

        if act in ["a", "register"]:
            username = input("Enter username: ").strip()
            password = input("Enter password: ").strip()
            msg = {"action": "register", "username": username, "password": password}
            client.sendall(json.dumps(msg).encode("utf-8"))
            response = client.recv(1024).decode("utf-8")
            print(f"Server response: {response}\n")
            if response == "REGISTER_SUCCESS":
                print("Resgister. Please login.\n")
                continue
            else:
                continue

        elif act in ["b", "login"]:
            username = input("Enter username: ").strip()
            password = input("Enter password: ").strip()
            msg = {"action": "login", "username": username, "password": password}
            client.sendall(json.dumps(msg).encode('utf-8'))
            resp_raw = client.recv(1024).decode('utf-8')
            try:
                resp = json.loads(resp_raw)
                if resp.get("type") == "LOGIN_SUCCESS":
                    print("You are now logged in.")
                    print(f"Your status: {resp.get('status')}")
                    return True, username, resp.get("status", {})
            except json.JSONDecodeError:
                # 舊版文字回覆或錯誤碼
                print(f"Server response: {resp_raw}")
                if resp_raw in ["LOGIN_SUCCESS"]:
                    return True, username, {}
                elif resp_raw == "LOGIN_FAILED_DUPLICATE":
                    print("Duplicate login detected. Please logout previous session.")
                else:
                    print("Login failed. Try again.\n")

        elif act in ["q", "quit"]:
            print("Goodbye!")
            client.close()
            exit(0)

        else:
            print("Invalid choice. Try again.\n")
            continue

        client.sendall(json.dumps(msg).encode('utf-8'))

        response = client.recv(1024).decode('utf-8')
        print(f"Server response: {response}\n")
        if response in ["REGISTER_SUCCESS", "LOGIN_SUCCESS"]:
            print("You are now logged in.")
            break
        else:
            print("Action failed. Try again.\n")
            continue
            

def main():
    print("=== Welcome to lobby ===")

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((HOST, PORT))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(('0.0.0.0', UDP_PORT))
        udp.settimeout(None)
        print(f"Connected to lobby with {HOST}:{PORT}")

        ok, username, status0 = sign_in(client)
        if not ok:
            print("Login failed.")
            return
        
        def stats_provider():
            return {
                "wins_delta": 0,
                "losses_delta": 0,
                "in_game": False,
            }

        _ = start_status_reporter(client, username, stats_provider=stats_provider)

        _ = input("Press any key to search opponent")
        waiting_op(udp, lobby_sock=client, username=username)
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C detected. Closing sockets and exiting...")
        safe_logout(client, locals().get("username", None))

    finally:
        client.close()
        print("[!] Clean exit complete.")
        


if __name__ == "__main__":
    main()
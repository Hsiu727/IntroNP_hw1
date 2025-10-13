import socket
import json
import time
import random

from tt import GameUI, gameplay, send_json_line, recv_json_line, start_status_reporter, safe_logout

HOST = 'localhost'
PORT = 5000
UDP_PORT_RANGE = [i for i in range(10000,11001)]
INVITE_RETRY_INTERVAL = 2.0
INVITE_WAIT_WINDOW    = 15.0
ACK_TYPES = {"ACCEPT", "DECLINE"}
TCP_BASE_PORT = 10000
TCP_TRY_COUNT = 100

class HostGame(gameplay):
    def __init__(self, conn, peer_name: str, lobby_sock, username: str):
        super().__init__()
        self.conn = conn
        self.peer_name = peer_name
        self.target_wins = 3
        self.round = 1
        self.buf = b""
        self.ui = GameUI()
        self.my_role = "A"
        self.lobby_sock = lobby_sock
        self.username = username

    def start_game(self):
        try:
            while True:  # 支援多局
                self.ui.show_game_start(self.my_role, self.target_wins)
                self._send_new_start()

                while True:
                    if not self._play_round():
                        break

                print("對戰結束。你可以選擇是否在 5 秒內與對方同時發起 REMATCH。")
                ok = self._rematch_both_sides()
                if ok:
                    print("雙方都同意再來一局，重置牌庫與比分。")
                    self._reset_match()
                    self._send_new_start()
                    continue
                else:
                    print("對方未在 5 秒內同意或你選擇不再一局，結束遊戲。")
                    break
        except KeyboardInterrupt:
            # 通知對手我已中斷
            try:
                send_json_line(self.conn, {"type": "DISCONNECT", "reason": "KeyboardInterrupt"})
            except Exception:
                pass
            # 回報不在遊戲中（可選）
            try:
                payload = {
                    "action": "status_report",
                    "username": self.username,
                    "status": {"in_game": False}
                }
                self.lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
            except Exception:
                pass
            # 登出 lobby
            safe_logout(self.lobby_sock, self.username)
            print("\n[!] You pressed Ctrl+C. Disconnected from peer and logged out.")

    def _play_round(self):
        self.ui.show_round(self.round)
        if not self._check_cards():
            return False
            
        send_json_line(self.conn, {"type": "MOVE", "cards": self.playr1.cards})
        # 取得自己的出牌
        my_cards = self._get_my_move()
        if my_cards is None:
            return False
        
        # 接收對手出牌
        msg, self.buf = recv_json_line(self.conn, self.buf)

        t = msg.get("type")
        if t == "DISCONNECT":
            print("[!] 對手中斷連線（DISCONNECT）。本局結束。")
            # 可選：把本局結束狀態回報給 lobby（in_game=False）
            try:
                payload = {
                    "action": "status_report",
                    "username": self.username,
                    "status": {"in_game": False}
                }
                self.lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
            except Exception:
                pass
            return False
        if t != "MOVE":
            print("Unexpected message type")
            return False
        
        op_cards = msg.get("cards", [])
        
        # 計算結果
        my_sum = sum(my_cards)
        op_sum = sum(op_cards)
        
        # 移除使用的牌
        self.playr1.use_cards(my_cards)
        self.playr2.use_cards(op_cards)
        
        self.round += 1
        return self._handle_round_result(my_sum, op_sum)

    def _get_my_move(self):
        self.ui.show_cards(self.playr1.cards)
        self.ui.show_opponents_cards(self.playr2.cards)
        while True:
            try:
                pick = self.ui.get_player_move()
                if self._validate_move(pick, self.playr1.cards):
                    return pick
            except ValueError:
                print("請輸入數字")
            except KeyboardInterrupt:
                raise

    def _check_cards(self):
        if not self.playr1.has_cards():
            print("你沒有牌了，PlayerB 勝！")
            self._send_game_over("B")
            return False
        if not self.playr2.has_cards():
            print("對手沒有牌了，你勝！")
            self._send_game_over("A")
            return False
        return True

    def _validate_move(self, cards, available_cards):
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

    def _handle_round_result(self, a_sum, b_sum):
        aa = float(a_sum)
        bb = float(b_sum)
        if aa == bb:
            aa += random.choice([0.1, -0.1])
        
        winner = "A" if aa > bb else "B"
        if winner == "A":
            self.playr1.winRound += 1
            print(f"你贏了此回合！ 你的: {a_sum}  對手: {b_sum}")
        else:
            self.playr2.winRound += 1
            print(f"你輸了此回合… 你的: {a_sum}  對手: {b_sum}")

        self._send_round_result(a_sum, b_sum, winner)
        print(f"目前比分：你 {self.playr1.winRound} : {self.playr2.winRound} 對手")

        if self.playr1.winRound >= self.target_wins or self.playr2.winRound >= self.target_wins:
            self._send_game_over(winner)
            return False
        return True

    def _send_round_result(self, a_sum, b_sum, winner):
        send_json_line(self.conn, {
            "type": "ROUND_RESULT",
            "a_play": a_sum, 
            "b_play": b_sum,
            "winner": winner,
            "a_wins": self.playr1.winRound, 
            "b_wins": self.playr2.winRound,
            "a_left": len(self.playr1.cards), 
            "b_left": len(self.playr2.cards)
        })

    def _send_game_over(self, winner):
        send_json_line(self.conn, {
            "type": "GAME_OVER",
            "winner": winner,
            "a_wins": self.playr1.winRound,
            "b_wins": self.playr2.winRound
        })
        self.ui.show_game_over(self.playr1.winRound, self.playr2.winRound, winner, self.my_role)

        try:
            wins_delta   = 1 if winner == "A" else 0
            losses_delta = 1 if winner == "B" else 0
            payload = {
                "action": "status_report",
                "username": self.username,
                "status": {
                    "wins_delta": wins_delta,
                    "losses_delta": losses_delta,
                    "in_game": False
                }
            }
            self.lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
        except Exception as e:
            pass

    def _reset_match(self):
        # 重新發一副牌、比分歸零、回合數歸 1
        from tt import pls
        self.playr1 = pls()
        self.playr2 = pls()
        self.playr1.winRound = 0
        self.playr2.winRound = 0
        self.round = 1

    def _send_new_start(self):
        send_json_line(self.conn, {
            "type": "START",
            "you_are": "PlayerB",
            "target_wins": self.target_wins
        })

    def _rematch_both_sides(self) -> bool:
        """
        問自己是否要 rematch；若選 y：送 REMATCH，並在 5 秒內等待對方也送 REMATCH。
        雙方都送出才回 True，由主機負責送新的 START；否則回 False。
        """
        ans = input("Rematch? (y/n): ").strip().lower()
        if ans != "y":
            return False

        # 我方先送出 REMATCH 表態
        send_json_line(self.conn, {"type": "REMATCH"})

        # 等待對方的 REMATCH（5 秒），期間忽略其他訊息
        buf = b""
        try:
            self.conn.settimeout(5.0)
            while True:
                msg, buf = recv_json_line(self.conn, buf)
                t = msg.get("type")
                if t == "REMATCH":
                    return True
                if t == "DISCONNECT":
                    print("[!] 對手中斷連線（DISCONNECT）。無法 rematch。")
                    return False
                # 其他型別忽略
        except Exception:
            return False
        finally:
            try:
                self.conn.settimeout(None)
            except Exception:
                pass

def tcp_gameplay(udp, op, lobbySock, username, host = '0.0.0.0', port = 8000):
    op_ip, op_port, name = op[0]
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    selected_port = None
    try:
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # === 新增：循環找可用埠 ===
        base = TCP_BASE_PORT if port is None else port
        for p in range(base, base + TCP_TRY_COUNT):
            try:
                tcp.bind((host, p))
                selected_port = p
                if p != base:
                    print(f"[PORT SWITCH] TCP port {base} in-use, switched to {p}")
                break
            except OSError:
                continue

        if selected_port is None:
            print("[ERROR] No available TCP port in range.")
            return  # 回上層，讓主程式再走一輪
        tcp.listen()
        print(f"TCP listen on {host}:{selected_port}")

        tcp_info = {"type":"TCP_INFO", "port":selected_port}
        udp.sendto(json.dumps(tcp_info).encode(), (op_ip, op_port))

        tcp.settimeout(10.0)
        print(f"Waiting for {name} to connect...")

        while True:
            try:
                conn, addr = tcp.accept()
                if addr[0] == op_ip:
                    print(f"Connected by {name} from {addr}")
                    game = HostGame(conn, name, lobby_sock=lobbySock, username=username)
                    try:
                        game.start_game()
                    except ConnectionError:
                        print("[A] Peer disconnected during game.")
                    finally:
                        conn.close()
                    # 無論如何都 return，讓主流程回到再搜尋
                    return
                else:
                    print(f"Rejected connection from {addr}")
                    conn.close()
            except socket.timeout:
                print("Connection timeout")
                return
    except KeyboardInterrupt:
        safe_logout(lobbySock, username)
        print("\n[!] Ctrl+C: closed TCP and logged out.")
    except Exception as e:
        print(f"TCP server error: {e}")
    finally:
        tcp.close()
        print("TCP connection closed")

def choose_opponent(opponents):
    """
    opponents: list[(ip, port, name)]
    return:    (ip, port, name) or None (代表要重掃)
    """
    if not opponents:
        print("No opponents available.")
        return None

    print("\n=== Available Players ===")
    for i, (ip, port, name) in enumerate(opponents, 1):
        print(f"{i}. {name}  @ {ip}:{port}")

    while True:
        choice = input("Select a player by number, or [r]escan / [q]uit: ").strip().lower()
        if choice == "q":
            print("Goodbye!")
            exit(0)
        if choice == "r":
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(opponents):
                return opponents[idx-1]
        print("Invalid choice. Try again.")

def Selected_opponent(udp, opponent):
    target_ip, target_port, name = opponent
    invite = {"type": "INVITE", "from": "PlayerA"}

    prev_to = udp.gettimeout()          # ← 記住原本 timeout（通常是 1.0）
    try:
        udp.settimeout(INVITE_WAIT_WINDOW)
        deadline = time.time() + INVITE_WAIT_WINDOW
        udp.sendto(json.dumps(invite).encode(), (target_ip, target_port))
        last_send = time.time()
        print(f"Inviting {name} at {(target_ip, target_port)} ...")

        while time.time() < deadline:
            try:
                data, addr = udp.recvfrom(1024)
                reply = json.loads(data.decode('utf-8'))
                if addr[0] == target_ip and reply.get("type") in ACK_TYPES:
                    if reply["type"] == "ACCEPT":
                        print(f"{name} accepted! Starting TCP server...")
                        return "ACCEPT"
                    else:
                        print(f"{name} declined.")
                        return "DECLINE"
            except socket.timeout:
                if time.time() - last_send >= INVITE_RETRY_INTERVAL:
                    udp.sendto(json.dumps(invite).encode("utf-8"), (target_ip, target_port))
                    last_send = time.time()
    finally:
        udp.settimeout(prev_to)         # ← 一定要復原！

    print("Invitation timed out (no response).")
    return "TIMEOUT"
  
def search_game(broadcast):
    found = []
    for port in UDP_PORT_RANGE:
        broadcast.sendto(json.dumps({"type": "SEARCH"}).encode(), ('255.255.255.255', port))

        try:
            data, addr = broadcast.recvfrom(1024)
            reply = json.loads(data.decode('utf-8'))
            if reply["type"] == "REPLY":
                found.append((addr[0], addr[1], reply["name"]))
                print("Found ", reply["name"], "at", addr)
        except socket.timeout:
            print("Timeout: no player available")
            pass

    if found:
        print("Available players:", found)
        return found
    else:
        print("No game servers found.")
        exit(0)

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
    username = None

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    broadcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.connect((HOST, PORT))
        broadcast.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        broadcast.settimeout(1.0)
        print(f"Connected to lobby with {HOST}:{PORT}")

        ok, username, status0 = sign_in(client)
        if not ok:
            print("Login failed.")
            return

        def stats_provider():
            return {
                "wins_delta":0,
                "losses_delta":0,
                "in_game":False,
            }
        _ = start_status_reporter(client, username, role="A", stats_provider=stats_provider)

        while True:
            _ = input("Press any key to search opponent")
            while True:
                players = search_game(broadcast)
                target = choose_opponent(players)
                if target is None:
                    continue
                result = Selected_opponent(broadcast, target)
                if result == "ACCEPT":
                    tcp_gameplay(broadcast, [target], lobbySock=client, username=username)
                    break
                else:
                    print("Invite not accepted. Choose another or rescan.")

    except KeyboardInterrupt:
        print("\n[!] Ctrl+C detected. Closing sockets and exiting...")
    finally:
        broadcast.close()
        client.close()
        print("[!] Clean exit complete.")


if __name__ == "__main__":
    main()
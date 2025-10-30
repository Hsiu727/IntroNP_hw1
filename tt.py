import random
import json
import atexit
import threading
import time
"""
There are some utils and original game design
"""
def send_json_line(conn, obj: dict):
    data = (json.dumps(obj) + "\n").encode("utf-8")
    conn.sendall(data)

def recv_json_line(conn, buf=b""):
    while True:
        if b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8")), buf
        chunk = conn.recv(1024)
        if not chunk:
            raise ConnectionError("Peer closed")
        buf += chunk

class GameUI:
    @staticmethod
    def show_game_start(target_wins: int):
        print(f"== Game start ==")
        print(f"對戰目標：先到 {target_wins} 勝")

    @staticmethod
    def show_round(round_num: int):
        print(f"\n=== Round {round_num} ===")

    @staticmethod
    def show_cards(cards: list):
        print("Your cards:")
        print("[ " + " ".join(map(str,cards)) + " ]")
    @staticmethod
    def show_opponents_cards(op_name: str, cards: list):
        print(f"{op_name}'s cards:")
        print("[ " + " ".join(map(str, cards)) + " ]")
    @staticmethod
    def get_player_move():
        while True:
            try:
                choice = input("你要出幾張牌？(1 or 2): ").strip()
                if choice not in ["1","2"]:
                    print("請輸入 1 或 2")
                    continue
                pick = []
                k = int(choice)
                while len(pick) < k:
                    c = int(input(f"選第 {len(pick)+1} 張 (1..7): "))
                    if c < 1 or c > 7:
                        print("請輸入 1..7")
                        continue
                    if c in pick:
                        print("同一回合不能重複同一張")
                        continue
                    pick.append(c)
                return pick
            except ValueError:
                print("請輸入數字")

    @staticmethod
    def show_round_result(my_play, op_play, winner, my_wins, op_wins):
        print(f"回合結果：你出 {my_play}，對手出 {op_play}；勝者 = {winner}")
        print(f"目前比分：你 {my_wins} : {op_wins} 對手")

    @staticmethod
    def show_game_over(a_wins, b_wins, winner, my_role):
        print("=== 遊戲結束 ===")
        if my_role == "A":
            print(f"最終比分：你 {a_wins} : {b_wins} 對手")
        else:
            print(f"最終比分：對手 {a_wins} : {b_wins} 你")
        print(f"勝者：{winner}")

    def print_info(self, msg: str):
        print(f"[INFO] {msg}")

class pls:
    def __init__(self):
        self.winRound = 0
        self.cards = [i for i in range(1,8)]
    def show_cards(self):
        print("This is your cards!!")
        print("[ " + " ".join(map(str, self.cards)) + " ]")      
    def use_cards(self, cards_to_use):
        for card in cards_to_use:
            if card not in self.cards:
                print(f"Card {card} is not fucking available!")
                return "error"
        for card in cards_to_use:
            self.cards.remove(card)
        return sum(cards_to_use)
    def has_cards(self):
        return len(self.cards) > 0
    def show_opponent_cards(self):
        print("Opponent's cards:")
        print("[ " + " ".join(map(str, self.cards)) + " ]")

class gameplay:
    def __init__(self):
        self.playr1 = pls()
        self.playr2 = pls()
        self.turn = {1, 2}

    def roundWin(self, who):
        if who == 1:
            self.playr1.winRound += 1
        elif who == 2:
            self.playr2.winRound += 1

    def checkWinStatus(self):
        if self.playr1.winRound >= 3:
            print("PlayerA WIN!!!!!!!")
            exit(0)
        elif self.playr2.winRound >= 3:
            print("PlayerB WIN!!!!!!!")
            exit(0)
        elif not self.playr1.has_cards():
            print("PlayerA has no cards left - PlayerB WIN!!!!!!!")
            exit(0)
        elif not self.playr2.has_cards():
            print("PlayerB has no cards left - PlayerA WIN!!!!!!!")
            exit(0)
        else:
            pass

    def get_player_move(self, player, player_name, op):
        print(f" ==== {player_name} Turn ==== ")
        player.show_cards()
        op.show_opponent_cards()
        while True:
            try:
                choice = input("How many cards to play? (1 or 2): ").strip()
                if choice not in ['1', '2']:
                    print("Please enter 1 or 2")
                    continue
                    
                cards = []
                num_cards = int(choice)
                
                # 收集所有要出的牌
                while len(cards) < num_cards:
                    card = int(input(f"Select card {len(cards)+1}: "))
                    
                    # 檢查牌的範圍
                    if card < 1 or card > 7:
                        print("Invalid card number! Please choose between 1-7")
                        continue
                        
                    # 檢查是否已經選過這張牌
                    if card in cards:
                        print("You already selected this card!")
                        continue
                        
                    cards.append(card)
                    
                result = player.use_cards(cards)
                if result != "error":
                    if num_cards == 1:
                        return result, str(result)
                    else:
                        return result, f"{cards[0]} + {cards[1]} = {result}"
                        
            except ValueError:
                print("Please enter valid numbers")

    def operates(self):
        # Get moves from both players
        print("\n=== New Round ===")

        if not self.playr1.has_cards():
            print("\nPlayerA has no cards left! PlayerB wins!")
            self.playr2.winRound = 3
            self.checkWinStatus()
            return
        
        if not self.playr2.has_cards():
            print("\nPlayerB has no cards left! PlayerA wins!")
            self.playr1.winRound = 3
            self.checkWinStatus()
            return
        
        score_a, str_a = self.get_player_move(self.playr1, "PlayerA", self.playr2)
        print("\nPlayerA has played:", str_a)
        
        score_b, str_b = self.get_player_move(self.playr2, "PlayerB", self.playr1)
        print("\nPlayerB has played:", str_b)
        
        if score_a == "error" or score_b == "error":
            return
            
        print("\nFinal scores:")
        print(f"PlayerA: {str_a} = {score_a}")
        print(f"PlayerB: {str_b} = {score_b}")

        if score_a == score_b:
            score_a += random.choice([0.1, -0.1])

        if score_a < score_b:
            print("\nPlayerB wins this round!")
            self.roundWin(2)
        elif score_a > score_b:
            print("\nPlayerA wins this round!")
            self.roundWin(1)
            
        print(f"\nCurrent score - PlayerA {self.playr1.winRound} : {self.playr2.winRound} PlayerB")
        print("=" * 30)
        self.checkWinStatus()

def main():
    gg = gameplay()

    for _ in range(5):
        gg.operates()

def start_status_reporter(lobby_sock, username, stats_provider):
    """
    stats_provider(): -> dict like
      {
        "wins_delta": int,
        "losses_delta": int,
        "in_game": bool,
      }
    """
    stop_flag = {"stop": False}

    def _loop():
        while not stop_flag["stop"]:
            try:
                status = stats_provider() or {}
                payload = {
                    "action": "status_report",
                    "username": username,
                    "status": status,
                }
                lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
            except Exception as e:
                # 不影響主要遊戲流程
                pass
            finally:
                time.sleep(5.0)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

    def _cleanup():
        try:
            stop_flag["stop"] = True
            payload = {"action": "logout", "username": username}
            try:
                lobby_sock.sendall(json.dumps(payload).encode("utf-8"))
            except Exception:
                pass
            try:
                lobby_sock.close()
            except Exception:
                pass
        except Exception:
            pass

    atexit.register(_cleanup)
    return stop_flag

def safe_logout(lobby_sock, username: str):
    """Try to tell lobby that this user logs out, ignore any errors."""
    try:
        if lobby_sock and username:
            lobby_sock.sendall(json.dumps({
                "action": "logout",
                "username": username
            }).encode("utf-8"))
            # 如果要等回覆可加：
            # lobby_sock.settimeout(1.0)
            # _ = lobby_sock.recv(1024)
            # lobby_sock.settimeout(None)
    except Exception:
        pass

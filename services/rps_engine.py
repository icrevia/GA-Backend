import time

class RPSEngine:
    def __init__(self, match_id: int, players: list[int], duration_seconds: int = 10):
        self.match_id = match_id
        self.players = players
        self.moves = {p: None for p in players}
        
        # State machine: COUNTDOWN -> REVEAL -> COMPLETED
        self.state = "COUNTDOWN"
        
        self.start_time_ms = int(time.time() * 1000)
        self.end_time_ms = self.start_time_ms + (duration_seconds * 1000)
        
        self.winner_id = None
        self.is_draw = False

    def submit_move(self, user_id: int, move: str):
        if self.state != "COUNTDOWN":
            return False
            
        if user_id not in self.players:
            return False
            
        if self.moves[user_id] is not None:
            return False # Move already submitted
            
        if move not in ["ROCK", "PAPER", "SCISSORS"]:
            return False
            
        self.moves[user_id] = move
        return True
        
    def all_moves_submitted(self) -> bool:
        return all(move is not None for move in self.moves.values())

    def is_time_up(self) -> bool:
        return int(time.time() * 1000) >= self.end_time_ms

    def evaluate_result(self):
        self.state = "REVEAL"
        
        p1, p2 = self.players
        m1 = self.moves[p1]
        m2 = self.moves[p2]
        
        if m1 == m2:
            self.is_draw = True
            self.winner_id = None
        elif m1 is None and m2 is not None:
            self.winner_id = p2
        elif m2 is None and m1 is not None:
            self.winner_id = p1
        elif m1 is None and m2 is None:
            self.is_draw = True # both forfeited -> cancel/refund
        else:
            win_map = {
                "ROCK": "SCISSORS",
                "SCISSORS": "PAPER",
                "PAPER": "ROCK"
            }
            if win_map[m1] == m2:
                self.winner_id = p1
            else:
                self.winner_id = p2

    def get_state(self):
        return {
            "match_id": self.match_id,
            "state": self.state,
            "players": self.players,
            "moves": self.moves if self.state in ["REVEAL", "COMPLETED"] else {p: ("READY" if m else None) for p, m in self.moves.items()},
            "end_time_ms": self.end_time_ms,
            "winner_id": self.winner_id,
            "is_draw": self.is_draw
        }

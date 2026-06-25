import logging
from typing import Dict, List, Optional
import random

logger = logging.getLogger("GamerzAdda.LudoEngine")

# Ludo Constants
SAFE_CELLS = {1, 9, 14, 22, 27, 35, 40, 48}
TOTAL_CELLS_PER_PLAYER = 57 # 51 normal + 6 home stretch

class LudoEngine:
    def __init__(self, match_id: int, players: List[str]):
        self.match_id = match_id
        # players = ["RED", "BLUE", "GREEN", "YELLOW"]
        self.players = players
        
        # State
        self.turn_index = 0
        self.state = "WAITING"
        self.winner = None
        
        # Player positions: [token1, token2, token3, token4] (-1 = home, 57 = finished)
        self.positions: Dict[str, List[int]] = {p: [-1, -1, -1, -1] for p in players}
        
        self.last_dice_roll = 0
        self.dice_rolled = False
        self.sixes_in_a_row = 0

    def get_current_player(self) -> str:
        return self.players[self.turn_index]
        
    def next_turn(self):
        if self.winner:
            return
            
        self.turn_index = (self.turn_index + 1) % len(self.players)
        self.dice_rolled = False
        self.sixes_in_a_row = 0

    def roll_dice(self, player: str) -> int:
        if self.state != "PLAYING":
            return -1
        if player != self.get_current_player():
            return -1
        if self.dice_rolled:
            return -1
            
        roll = random.randint(1, 6)
        self.last_dice_roll = roll
        self.dice_rolled = True
        
        if roll == 6:
            self.sixes_in_a_row += 1
            if self.sixes_in_a_row == 3:
                # 3 sixes = turn forfeited
                self.next_turn()
                return 0
        
        # If no valid moves, auto pass
        if not self.has_valid_moves(player, roll):
            self.next_turn()
            
        return roll

    def has_valid_moves(self, player: str, roll: int) -> bool:
        for pos in self.positions[player]:
            if pos == -1 and roll == 6:
                return True
            if pos != -1 and pos + roll <= TOTAL_CELLS_PER_PLAYER:
                return True
        return False

    def move_token(self, player: str, token_index: int) -> bool:
        if player != self.get_current_player() or not self.dice_rolled:
            return False
            
        pos = self.positions[player][token_index]
        roll = self.last_dice_roll
        
        if pos == -1:
            if roll == 6:
                self.positions[player][token_index] = 1 # Out of home
                self.dice_rolled = False # Gets another turn
                return True
            return False
            
        new_pos = pos + roll
        if new_pos > TOTAL_CELLS_PER_PLAYER:
            return False # Overshot
            
        # Move token
        self.positions[player][token_index] = new_pos
        
        # Check kill
        killed = self._check_and_execute_kill(player, new_pos)
        
        # Check win
        if self._check_win(player):
            self.state = "COMPLETED"
            self.winner = player
            return True
            
        if roll == 6 or killed or new_pos == TOTAL_CELLS_PER_PLAYER:
            # Gets another turn
            self.dice_rolled = False
        else:
            self.next_turn()
            
        return True

    def _check_and_execute_kill(self, current_player: str, global_pos: int) -> bool:
        """Translate global_pos to absolute board pos to check collisions"""
        # Complex global-to-absolute mapping logic goes here
        # Simplified for now
        return False

    def _check_win(self, player: str) -> bool:
        return all(pos == TOTAL_CELLS_PER_PLAYER for pos in self.positions[player])

    def get_state(self) -> dict:
        return {
            "match_id": self.match_id,
            "state": self.state,
            "turn": self.get_current_player(),
            "positions": self.positions,
            "last_dice_roll": self.last_dice_roll,
            "dice_rolled": self.dice_rolled,
            "winner": self.winner
        }

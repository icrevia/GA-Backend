import logging
from typing import Dict, List, Optional
import random

logger = logging.getLogger("GamerzAdda.LudoEngine")

# Ludo Constants
SAFE_CELLS = {0, 8, 13, 21, 26, 34, 39, 47} # Global 0-indexed positions
TOTAL_CELLS_PER_PLAYER = 57 # 51 main track + 6 home stretch (index 57 = finish)

COLOR_OFFSETS = {
    "RED": 0,
    "GREEN": 13,
    "YELLOW": 26,
    "BLUE": 39
}

class LudoEngine:
    def __init__(self, match_id: int, players: List[str]):
        self.match_id = match_id
        self.players = players
        
        # State
        self.turn_index = 0
        self.state = "WAITING"
        self.winner = None
        
        # Player positions: [token1, token2, token3, token4] (1 = start, 57 = finished)
        self.positions: Dict[str, List[int]] = {p: [1, 1, 1, 1] for p in players}
        self.scores: Dict[str, int] = {p: 0 for p in players}
        
        self.last_dice_roll = 0
        self.dice_rolled = False
        self.sixes_in_a_row = 0
        self.end_time_ms = 0

    def get_current_player(self) -> str:
        return self.players[self.turn_index]
        
    def next_turn(self):
        if self.winner:
            return
            
        self.turn_index = (self.turn_index + 1) % len(self.players)
        self.dice_rolled = False
        self.sixes_in_a_row = 0
        self.last_dice_roll = 0

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
        else:
            self.sixes_in_a_row = 0
        
        # If no valid moves, auto pass
        if not self.has_valid_moves(player, roll):
            self.next_turn()
            
        return roll

    def has_valid_moves(self, player: str, roll: int) -> bool:
        for pos in self.positions[player]:
            if pos < TOTAL_CELLS_PER_PLAYER and pos + roll <= TOTAL_CELLS_PER_PLAYER:
                return True
        return False

    def _relative_to_global(self, player: str, rel_pos: int) -> int:
        """Converts relative position (1-51) to global position (0-51)"""
        if rel_pos < 1 or rel_pos > 51:
            return -1 # Home stretch or invalid, not on main track
        
        offset = COLOR_OFFSETS.get(player, 0)
        # Relative pos starts at 1, so offset + (rel_pos - 1)
        global_pos = (offset + (rel_pos - 1)) % 52
        return global_pos

    def _check_and_execute_kill(self, current_player: str, new_rel_pos: int) -> bool:
        """Checks if a kill happened on the main track. Returns True if a token was killed."""
        global_pos = self._relative_to_global(current_player, new_rel_pos)
        if global_pos == -1 or global_pos in SAFE_CELLS:
            return False
            
        killed_anyone = False
        for opp in self.players:
            if opp == current_player:
                continue
            
            for i, opp_pos in enumerate(self.positions[opp]):
                if opp_pos >= 1 and opp_pos <= 51:
                    opp_global = self._relative_to_global(opp, opp_pos)
                    if opp_global == global_pos:
                        # Kill! Deduct points from opponent
                        points_lost = opp_pos - 1
                        self.scores[opp] = max(0, self.scores[opp] - points_lost)
                        # Reset token
                        self.positions[opp][i] = 1
                        killed_anyone = True
                        logger.info(f"Token {i} of {opp} was killed by {current_player} at global pos {global_pos}!")
                        
        return killed_anyone

    def move_token(self, player: str, token_index: int) -> bool:
        if player != self.get_current_player() or not self.dice_rolled:
            return False
            
        pos = self.positions[player][token_index]
        roll = self.last_dice_roll
        
        if pos == TOTAL_CELLS_PER_PLAYER:
            return False # Already finished
            
        new_pos = pos + roll
        if new_pos > TOTAL_CELLS_PER_PLAYER:
            return False # Overshot, exactly 57 is needed
            
        # Move token and score
        self.positions[player][token_index] = new_pos
        self.scores[player] += roll # +1 point per step
        
        # Check kill
        killed = self._check_and_execute_kill(player, new_pos)
        if killed:
            self.scores[player] += 20 # +20 points for kill
            
        # Check if reached home
        if new_pos == TOTAL_CELLS_PER_PLAYER:
            self.scores[player] += 50
            if all(p == TOTAL_CELLS_PER_PLAYER for p in self.positions[player]):
                self.scores[player] += 100 # Bonus for all tokens home
        
        # Give another turn if 6, killed, or reached finish
        if roll == 6 or killed or new_pos == TOTAL_CELLS_PER_PLAYER:
            self.dice_rolled = False
        else:
            self.next_turn()
            
        return True

    def _check_win(self, player: str) -> bool:
        return all(pos == TOTAL_CELLS_PER_PLAYER for pos in self.positions[player])

    def get_state(self) -> dict:
        return {
            "match_id": self.match_id,
            "state": self.state,
            "turn": self.get_current_player(),
            "positions": self.positions,
            "scores": self.scores,
            "last_dice_roll": self.last_dice_roll,
            "dice_rolled": self.dice_rolled,
            "end_time_ms": self.end_time_ms,
            "winner": self.winner
        }

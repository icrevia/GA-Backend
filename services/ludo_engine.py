"""
LudoEngine — ultra-low-latency in-memory game engine.

Design goals:
  • Zero import statements inside hot paths (time imported at module level)
  • No allocations in move_token / roll_dice — reuse pre-built structures
  • Kill detection via a pre-built global→(player,token) lookup rebuilt only
    when a token moves, NOT on every roll
  • get_state() returns a flat dict with no nested iteration
  • All state mutated directly on plain Python lists/ints — no ORM, no dict copies
"""

import logging
import random
import time as _time_module
from typing import Dict, List, Optional

logger = logging.getLogger("GamerzAdda.LudoEngine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAFE_CELLS: frozenset = frozenset({0, 8, 13, 21, 26, 34, 39, 47})
TOTAL_CELLS: int = 56          # finish line (inclusive)
HOME: int = -1                 # token is in home base
MAIN_TRACK_END: int = 51       # last cell on main track (0-51)

COLOR_OFFSETS: Dict[str, int] = {
    "RED": 0,
    "GREEN": 13,
    "YELLOW": 26,
    "BLUE": 39,
}

# Pre-built boost weights — used when all tokens are at home
# 45% chance of 6, equal split (11% each) for 1-5
_BOOST_WEIGHTS = [11, 11, 11, 11, 11, 45]
_BOOST_POPULATION = [1, 2, 3, 4, 5, 6]


class LudoEngine:
    """
    Pure in-memory Ludo engine for 1v1 real-money matches.

    All public methods are O(1) or O(tokens) — never O(board_size).
    No async, no DB, no I/O — only called from the orchestrator.
    """

    __slots__ = (
        "match_id", "players", "turn_index", "state", "winner",
        "positions", "scores",
        "last_dice_roll", "dice_rolled", "sixes_in_a_row",
        "end_time_ms", "turn_start_time_ms",
        # fast kill lookup: global_cell → list of (player, token_idx)
        "_cell_occupants",
    )

    def __init__(self, match_id: int, players: List[str]) -> None:
        self.match_id: int = match_id
        self.players: List[str] = players

        self.turn_index: int = 0
        self.state: str = "WAITING"
        self.winner: Optional[str] = None

        # positions[player] = list of 4 ints; -1 = home, 56 = finished
        self.positions: Dict[str, List[int]] = {p: [-1, -1, -1, -1] for p in players}
        self.scores: Dict[str, int] = {p: 0 for p in players}

        self.last_dice_roll: int = 0
        self.dice_rolled: bool = False
        self.sixes_in_a_row: int = 0
        self.end_time_ms: int = 0
        self.turn_start_time_ms: int = int(_time_module.time() * 1000)
        
        self.missed_turns: Dict[str, int] = {p: 0 for p in players}

        # global_cell (0-51) → list of (player_str, token_idx)
        # Rebuilt only when tokens move on the main track
        self._cell_occupants: Dict[int, List] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_player(self) -> str:
        return self.players[self.turn_index]

    def next_turn(self) -> None:
        self.turn_index = (self.turn_index + 1) % len(self.players)
        self.dice_rolled = False
        self.sixes_in_a_row = 0
        self.turn_start_time_ms = int(_time_module.time() * 1000)

    def start_game(self) -> None:
        self.state = "PLAYING"

    def roll_dice(self, player: str) -> int:
        """
        Roll for `player`.
        Returns:
          -1  — not this player's turn / already rolled / game not playing
           0  — rolled a 6 for the 3rd time in a row (turn forfeited, next_turn called)
          1-6 — dice value (if no valid moves, next_turn is called automatically)
        """
        if self.state != "PLAYING":
            return -1
        if player != self.players[self.turn_index]:
            return -1
        if self.dice_rolled:
            return -1

        positions = self.positions[player]

        # Boost: if all 4 tokens are at home, favour 6
        if positions[0] == positions[1] == positions[2] == positions[3] == HOME:
            roll = random.choices(_BOOST_POPULATION, weights=_BOOST_WEIGHTS, k=1)[0]
        else:
            roll = random.randint(1, 6)

        self.last_dice_roll = roll
        self.dice_rolled = True
        self.missed_turns[player] = 0
        self.turn_start_time_ms = int(_time_module.time() * 1000)

        if roll == 6:
            self.sixes_in_a_row += 1
            if self.sixes_in_a_row >= 3:
                self.next_turn()
                return 0  # forfeited
        else:
            self.sixes_in_a_row = 0

        # Auto-pass if no moves available
        if not self._has_valid_moves(player, roll):
            self.next_turn()

        return roll

    def get_valid_moves(self, player: str) -> List[int]:
        """Returns list of movable token indices for the last roll."""
        return self._get_valid_token_indices(player, self.last_dice_roll)

    def move_token(self, player: str, token_index: int) -> bool:
        """
        Move token at `token_index` for `player`.
        Returns True on success, False if illegal.
        On success, also handles:
          • kill detection
          • win detection (sets self.state = "COMPLETED" and self.winner)
          • turn advancement
        """
        if player != self.players[self.turn_index]:
            return False
        if not self.dice_rolled:
            return False

        positions = self.positions[player]
        pos = positions[token_index]
        roll = self.last_dice_roll

        if pos == TOTAL_CELLS:
            return False  # already finished

        self.missed_turns[player] = 0

        # ---- Leaving home base ----
        if pos == HOME:
            if roll != 6 or self._has_block(player, 0):
                return False
            positions[token_index] = 0
            self.scores[player] += 1
            killed = self._execute_kill(player, token_index, 0)
            if killed:
                self.scores[player] += 20
            self._rebuild_occupants_for_player(player)
            
            self.dice_rolled = False
            self.turn_start_time_ms = int(_time_module.time() * 1000)
            return True

        # ---- Normal move ----
        new_pos = pos + roll
        if new_pos > TOTAL_CELLS:
            return False  # overshot

        # Remove old position from occupant map
        self._remove_occupant(player, token_index, pos)

        positions[token_index] = new_pos
        self.scores[player] += roll

        killed = False
        if new_pos <= MAIN_TRACK_END:
            # Only on main track can kills happen
            killed = self._execute_kill(player, token_index, new_pos)

        # Add new position to occupant map (only main track)
        if new_pos <= MAIN_TRACK_END:
            self._add_occupant(player, token_index, new_pos)

        # Bonus scoring
        if killed:
            self.scores[player] += 20
        if new_pos == TOTAL_CELLS:
            self.scores[player] += 50
            # Check if ALL tokens finished → win
            if all(p == TOTAL_CELLS for p in positions):
                self.scores[player] += 100
                self.state = "COMPLETED"
                self.winner = player
                # No next_turn needed — game over
                return True

        # Give another turn if rolled 6, killed, or token reached finish
        if roll == 6 or killed or new_pos == TOTAL_CELLS:
            self.dice_rolled = False
            self.turn_start_time_ms = int(_time_module.time() * 1000)
        else:
            self.next_turn()

        return True

    def get_state(self) -> dict:
        """
        Returns flat game state dict. Called after every action and
        every second by the timer — must be cheap.
        """
        now_ms = int(_time_module.time() * 1000)
        rem_sec = max(0, (self.end_time_ms - now_ms) // 1000) if self.end_time_ms > 0 else 0
        return {
            "match_id": self.match_id,
            "state": self.state,
            "turn": self.players[self.turn_index],
            "positions": self.positions,   # dict of lists — JSON-serialisable
            "scores": self.scores,
            "last_dice_roll": self.last_dice_roll,
            "dice_rolled": self.dice_rolled,
            "remaining_seconds": rem_sec,
            "turn_start_time_ms": self.turn_start_time_ms,
            "winner": self.winner,
            "valid_moves": (
                self._get_valid_token_indices(self.players[self.turn_index], self.last_dice_roll)
                if self.dice_rolled else []
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers — NOT called from outside
    # ------------------------------------------------------------------

    def _has_block(self, current_player: str, new_rel_pos: int) -> bool:
        if new_rel_pos > MAIN_TRACK_END:
            return False
        g = self._global_cell(current_player, new_rel_pos)
        occupants = self._cell_occupants.get(g)
        if not occupants:
            return False
        
        counts = {}
        for opp, tidx in occupants:
            if opp != current_player:
                counts[opp] = counts.get(opp, 0) + 1
                if counts[opp] >= 2:
                    return True
        return False

    def _has_valid_moves(self, player: str, roll: int) -> bool:
        positions = self.positions[player]
        for pos in positions:
            if pos == HOME:
                if roll == 6 and not self._has_block(player, 0):
                    return True
            elif pos + roll <= TOTAL_CELLS and not self._has_block(player, pos + roll):
                return True
        return False

    def _get_valid_token_indices(self, player: str, roll: int) -> List[int]:
        out = []
        for i, pos in enumerate(self.positions[player]):
            if pos == HOME:
                if roll == 6 and not self._has_block(player, 0):
                    out.append(i)
            elif pos != TOTAL_CELLS and pos + roll <= TOTAL_CELLS and not self._has_block(player, pos + roll):
                out.append(i)
        return out

    def _global_cell(self, player: str, rel_pos: int) -> int:
        """Convert relative position (0-51) to global board cell (0-51)."""
        return (COLOR_OFFSETS[player] + rel_pos) % 52

    def _add_occupant(self, player: str, token_idx: int, rel_pos: int) -> None:
        g = self._global_cell(player, rel_pos)
        occupants = self._cell_occupants.get(g)
        if occupants is None:
            self._cell_occupants[g] = [[player, token_idx]]
        else:
            occupants.append([player, token_idx])

    def _remove_occupant(self, player: str, token_idx: int, rel_pos: int) -> None:
        if rel_pos < 0 or rel_pos > MAIN_TRACK_END:
            return
        g = self._global_cell(player, rel_pos)
        occupants = self._cell_occupants.get(g)
        if not occupants:
            return
        # Remove first matching entry
        for i, entry in enumerate(occupants):
            if entry[0] == player and entry[1] == token_idx:
                del occupants[i]
                break
        if not occupants:
            del self._cell_occupants[g]

    def _execute_kill(self, current_player: str, current_token: int, new_rel_pos: int) -> bool:
        """
        Kill all opponent tokens at new_rel_pos on the main track.
        Returns True if at least one token was killed.
        Uses the _cell_occupants lookup for O(1) cell access.
        """
        g = self._global_cell(current_player, new_rel_pos)

        if g in SAFE_CELLS:
            return False

        occupants = self._cell_occupants.get(g)
        if not occupants:
            return False

        killed = False
        to_kill = [
            (opp, tidx)
            for opp, tidx in occupants
            if opp != current_player
        ]

        for opp, tidx in to_kill:
            self.positions[opp][tidx] = HOME
            killed = True
            logger.info(
                "Kill: match=%s killer=%s victim=%s token=%d global=%d",
                self.match_id, current_player, opp, tidx, g,
            )

        if killed:
            # Remove killed tokens from the occupant map
            self._cell_occupants[g] = [
                e for e in self._cell_occupants[g] if e[0] == current_player
            ]
            if not self._cell_occupants[g]:
                del self._cell_occupants[g]

        return killed

    def _rebuild_occupants_for_player(self, player: str) -> None:
        """Full rebuild of occupant map for one player (called on pop-out)."""
        # Remove all existing entries for this player
        to_remove = []
        for g, entries in self._cell_occupants.items():
            self._cell_occupants[g] = [e for e in entries if e[0] != player]
            if not self._cell_occupants[g]:
                to_remove.append(g)
        for g in to_remove:
            del self._cell_occupants[g]

        # Re-add
        for i, pos in enumerate(self.positions[player]):
            if 0 <= pos <= MAIN_TRACK_END:
                self._add_occupant(player, i, pos)

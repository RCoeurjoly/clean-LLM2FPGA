"""Single-source physical layout for TinyStories family model memories."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Sequence


@dataclass(frozen=True)
class TokenCodePlan:
    """Row-aligned eight-lane token weight banks, including explicit padding."""

    vocab: int
    hidden: int
    lanes: int
    words_per_row: int
    rows_per_bank: int
    bank_count: int
    bank_depth: int

    @property
    def logical_shape(self) -> tuple[int, int]:
        return self.vocab, self.hidden

    @property
    def physical_bank_shape(self) -> tuple[int, int]:
        return self.bank_count, self.bank_depth

    @property
    def padding(self) -> int:
        return self.bank_count * self.bank_depth - self.vocab * self.words_per_row

    def location(self, token: int, column: int) -> tuple[int, int, int]:
        """Map a logical code to exactly one bank, word, and byte lane."""
        if type(token) is not int or not 0 <= token < self.vocab:
            raise ValueError("token index outside logical shape")
        if type(column) is not int or not 0 <= column < self.hidden:
            raise ValueError("hidden column outside logical shape")
        bank, row = divmod(token, self.rows_per_bank)
        return bank, row * self.words_per_row + column // self.lanes, column % self.lanes


def build_token_code_plan(*, vocab: int, hidden: int, bank_depth: int = 16384) -> TokenCodePlan:
    lanes = 8
    if type(vocab) is not int or vocab < 1 or type(hidden) is not int or hidden < 1:
        raise ValueError("token-code geometry requires positive integer vocabulary and hidden width")
    if hidden % lanes:
        raise ValueError("token-code hidden width must be divisible by eight lanes")
    if type(bank_depth) is not int or bank_depth < hidden // lanes:
        raise ValueError("token-code bank depth cannot hold one complete row")
    words_per_row = hidden // lanes
    maximum_rows = bank_depth // words_per_row
    rows_per_bank = 1 << (maximum_rows.bit_length() - 1)
    bank_count = (vocab + rows_per_bank - 1) // rows_per_bank
    return TokenCodePlan(vocab, hidden, lanes, words_per_row, rows_per_bank,
                         bank_count, bank_depth)


def materialize_token_code_bank(plan: TokenCodePlan, codes: Sequence[int], bank: int) -> list[int]:
    """Pack one bank directly so large family models need not rebuild every bank."""
    if len(codes) != plan.vocab * plan.hidden:
        raise ValueError("token-code count differs from logical shape")
    if type(bank) is not int or not 0 <= bank < plan.bank_count:
        raise ValueError("token-code bank outside physical shape")
    words = [0] * plan.bank_depth
    first = bank * plan.rows_per_bank
    for token in range(first, min(first + plan.rows_per_bank, plan.vocab)):
        for column in range(plan.hidden):
            raw_code = codes[token * plan.hidden + column]
            if not isinstance(raw_code, Integral) or not -128 <= raw_code <= 127:
                raise ValueError(f"token-code ({token}, {column}) is outside signed INT8")
            code = int(raw_code)
            word = (token - first) * plan.words_per_row + column // plan.lanes
            words[word] |= (code & 0xFF) << (8 * (column % plan.lanes))
    return words


def materialize_token_code_banks(plan: TokenCodePlan, codes: Sequence[int]) -> list[list[int]]:
    return [materialize_token_code_bank(plan, codes, bank)
            for bank in range(plan.bank_count)]


def read_token_code(plan: TokenCodePlan, banks: Sequence[Sequence[int]],
                    token: int, column: int) -> int:
    bank, word, lane = plan.location(token, column)
    byte = (banks[bank][word] >> (8 * lane)) & 0xFF
    return byte - 256 if byte & 0x80 else byte

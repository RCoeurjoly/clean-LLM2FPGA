"""Storage and counter bounds for the frozen TinyStories generation modes.
 
Computing sixteen-step geometry does not establish that all compiler phases or
the board wrapper support it. Consumers must wire every dependent dimension.
"""
from typing import NamedTuple
 
 
class GenerationGeometry(NamedTuple):
    row_capacity: int
    row_address_width: int
    row_counter_width: int
    context_length_width: int
    step_counter_width: int
    output_slots: int
    token_index_width: int
    real_context_lengths: tuple[int, ...]
 
 
def generation_geometry(steps: int) -> GenerationGeometry:
    if type(steps) is not int or steps not in (1, 2, 16):
        raise ValueError("generation steps must be an integer in (1, 2, 16)")
    rows = max(8, 4 + steps)
    return GenerationGeometry(
        row_capacity=rows,
        row_address_width=(rows - 1).bit_length(),
        row_counter_width=rows.bit_length(),
        context_length_width=rows.bit_length(),
        step_counter_width=max(2, steps.bit_length()),
        output_slots=max(2, steps),
        token_index_width=max(1, (steps - 1).bit_length()),
        real_context_lengths=tuple(range(4, 4 + steps)),
    )
 

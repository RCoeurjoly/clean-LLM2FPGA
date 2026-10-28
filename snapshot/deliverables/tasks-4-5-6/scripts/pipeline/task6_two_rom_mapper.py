#!/usr/bin/env python3
"""Select BRAM for c_proj in the committed native-memory mapping pass."""
from scripts.pipeline import map_flattened_memory_checkpoints as mapper

mapper.IMMUTABLE_LUTRAM_NAMES -= {"c_proj_weight_codes_i8.mem"}
mapper.LUTRAM_NAMES -= {"c_proj_weight_codes_i8.mem"}

if __name__ == "__main__":
    mapper.main()

# Bridging RTL and Theorem Proving: LLM-Assisted Translation from Verilog to Lean

**COS 598B: Formal Methods with-and-for Machine Learning (Spring 2026)**  
Princeton University

**Authors:**  
- SooHyuk Cho (soohyuk.cho@princeton.edu)  
- Yu-Wei Fan (yf9172@princeton.edu)

---

## Overview

This project explores automated translation of Verilog RTL designs into formal Lean 4 specifications using large language models. Given a Verilog hardware module, the pipeline produces a Lean 4 artifact that encodes the module's behavior as a formally checkable model, complete with theorem declarations that capture key functional properties.

The translation targets one of three fixed schemas:
- **Schema A** — Combinational logic: `eval : Inputs → Outputs`
- **Schema B** — Sequential logic: `State`, `Input`, `Output`, `init`, `step`, `out`
- **Schema C** — Controller-style designs with inductive FSM states and `Array`-based memory

We evaluate two LLMs on a four-tier benchmark of increasing complexity:

| Tier | Design Type | # Modules |
|------|-------------|-----------|
| Tier 1 | Combinational (gates, muxes, encoders) | 9 |
| Tier 2 | Simple sequential (counters, shift registers, FSMs) | 8 |
| Tier 3 | Controller-style (FIFOs, arbiters, circular buffers) | 12 |
| Tier 4 | Complex datapaths (FPU, divider, reorder buffer) | 9 |

![alt text](pipeline_diagram.png "Pipeline Diagram")

The pipeline supports two modes:
- **System A (One-shot):** Single-prompt Lean generation from Verilog + schema
- **System D (Iterative Repair):** Compiler feedback fed back to the model for up to 3 repair iterations

---

## Key Results

### One-Shot Compilation (Sys A)

| Tier | GPT-5.4 | Gemini 2.5 Pro |
|------|---------|----------------|
| Tier 1 | 9/9 (100%) | 9/9 (100%) |
| Tier 2 | 8/8 (100%) | 8/8 (100%) |
| Tier 3 | 6/12 (50%) | 9/12 (75%) |
| Tier 4 | 5/9 (56%) | 2/9 (22%) |
| **Total** | **28/38 (73.7%)** | **28/38 (73.7%)** |

### After Iterative Repair (Sys D)

| Model | One-shot | After Iter 1 | After Iter 2 | After Iter 3 | Final |
|-------|----------|-------------|-------------|-------------|-------|
| GPT-5.4 | 28/38 (73.7%) | 36/38 (94.7%) | 38/38 (100%) | — | **100%** |
| Gemini 2.5 Pro | 28/38 (73.7%) | 31/38 (81.6%) | 35/38 (92.1%) | 36/38 (94.7%) | **94.7%** |

All 38 modules satisfy schema conformance checks in both one-shot and repaired outputs.

---

## Repository Structure

```
.
├── Verilog Designs/          # Source Verilog benchmarks (Tier 1–4)
│   ├── Tier 1/
│   ├── Tier 2/
│   ├── Tier 3/
│   └── Tier 4/
├── Leans-Chat/               # GPT-5.4 one-shot Lean outputs (Sys A)
│   ├── Tier 1/
│   ├── Tier 2/
│   ├── Tier 3/
│   └── Tier 4/
├── Leans-Gemmini/            # Gemini 2.5 Pro one-shot Lean outputs (Sys A)
│   └── Tier 1–4/
├── Leans-Chat-Repair/        # GPT-5.4 repair outputs (Sys D)
│   ├── Iter 1/Tier N/
│   ├── Iter 2/Tier N/
│   └── Iter 3/Tier N/
├── Leans-Gemmini-Repair/     # Gemini 2.5 Pro repair outputs (Sys D)
│   └── Iter 1–3/Tier N/
├── Leans-Handcrafted/        # Manually written reference Lean specs
├── prompts/
│   ├── prompt_A_oneshot.md   # One-shot generation prompt (Sys A)
│   ├── prompt_A_fewshot.md   # Few-shot variant
│   └── prompt_D_repair.md    # Iterative repair prompt (Sys D)
├── check.py                  # Evaluation checker script
├── schema.md                 # Target schema definitions (A/B/C)
├── pipeline_diagram.pdf      # System architecture diagram
├── 7_results.tex             # LaTeX source for results section
└── logs/                     # Auto-generated check logs
    ├── Tier1/, Tier2/, ...    # Per-module logs
    ├── chat_tierAll_iter0.log
    ├── gemmini_tierAll_iter0.log
    └── ...
```

---

## Reproducing the Results

### Prerequisites

- **Lean 4** (tested with Lean 4.29): Install via [elan](https://github.com/leanprover/elan)
- **Python 3.8+**
- Lean `Std` library available in your environment

Verify your setup:
```bash
lean --version
python3 --version
```

### Step 1: Generate Lean Artifacts (Manual LLM Step)

> **Note:** LLM generation requires manual interaction. You must use GPT-5.4 or Gemini 2.5 Pro directly via their respective interfaces or APIs. There is no automated API call script included.

For **one-shot generation (Sys A)**:
1. Open `prompts/prompt_A_oneshot.md`
2. For each Verilog file in `Verilog Designs/Tier N/`, paste the Verilog source into the prompt and query the model
3. Save the output `.lean` file to `Leans-Chat/Tier N/` (for GPT-5.4) or `Leans-Gemmini/Tier N/` (for Gemini 2.5 Pro)

For **iterative repair (Sys D)**:
1. Run `check.py` to identify non-compiling files and collect compiler error messages
2. Open `prompts/prompt_D_repair.md`, paste the original Lean file + compiler error
3. Save the repaired output to `Leans-Chat-Repair/Iter 1/Tier N/` (or `Iter 2/`, `Iter 3/`)
4. Repeat up to 3 iterations for files that still do not compile

### Step 2: Run the Checker

The `check.py` script compiles each Lean file and reports compilation status, sorry-freeness, schema conformance, and theorem count.

**Check one-shot results:**
```bash
# GPT-5.4 one-shot (all tiers)
python3 check.py --source chat

# Gemini 2.5 Pro one-shot (all tiers)
python3 check.py --source gemmini

# Handcrafted reference
python3 check.py --source handcrafted

# Specific tier only
python3 check.py --source chat --tier 3
```

**Check iterative repair results:**
```bash
# All repair iterations for GPT-5.4
python3 check.py --source chat_repair

# Specific iteration
python3 check.py --source chat_repair --iter 1
python3 check.py --source gemmini_repair --iter 2

# Treat iter 0 as the base one-shot run
python3 check.py --source chat_repair --iter 0
```

**Save per-module logs:**
```bash
python3 check.py --source chat --logs
```

Stats summaries are written automatically to `logs/` on every run (e.g., `logs/chat_tierAll_iter0.log`).

---

## Checker CLI Reference

| Flag | Description |
|------|-------------|
| `--source {handcrafted,chat,gemmini,chat_repair,gemmini_repair}` | Which Lean artifact directory to check |
| `--tier {1,2,3,4}` | Restrict to a single benchmark tier |
| `--iter N` | Check a specific repair iteration (1–3); `--iter 0` checks base one-shot files |
| `--repair` | Check all repair sources (chat_repair + gemmini_repair) |
| `--logs` | Write per-module compile logs to `logs/` subdirectory |

---

## Benchmark Design Summary

The benchmark spans four complexity tiers, each with two Verilog variants per design (verA/verB) where applicable:

| Tier | Modules | Schema | Key Features |
|------|---------|--------|--------------|
| 1 | ALU, Comparator, Decoder, Encoder, Mux | A (comb.) | Pure Boolean/BitVec operations |
| 2 | Accumulator, Counter, ShiftReg, FSM | B (seq.) | Clocked state registers, enable signals |
| 3 | FIFO variants, Arbiters (RR/PQ/lottery/token) | B/C (seq.) | Array memory, inductive FSM states |
| 4 | FPU (sp/dp add/mul/div), Divider, ROB | B/C (seq.) | Multi-stage datapaths, FP classification |

---

## Highlights

- **100% schema conformance** across all 38 modules for both models in one-shot generation
- **GPT-5.4 achieves 100% compile rate** after at most 2 repair iterations; all Tier 4 failures resolved in a single pass
- **Gemini 2.5 Pro reaches 94.7%** after 3 iterations; 2 Tier 4 modules (`fpu_dp_add`, `rob`) remain unresolved due to structural formalization issues
- **Different failure strategies:** GPT-5.4 generates structurally incorrect proofs (compile errors, sorry-free); Gemini falls back to explicit `sorry` placeholders in Tier 4
- **Theorem count analysis:** GPT-5.4 preserves theorem density through repair (avg 8.0 theorems/file for Tier 4 failures); Gemini trims theorems under repair pressure (avg 1.9 → 1.3)
- **Three dominant failure patterns identified:** invalid Lean array API usage, `native_decide` on symbolic goals, missing `Decidable` instances for user-defined predicates

---

## Citation

If you use this benchmark or evaluation framework, please cite:

```
SooHyuk Cho and Yu-Wei Fan. "Bridging RTL and Theorem Proving: LLM-Assisted Translation from Verilog to Lean."
COS 598B Final Project, Princeton University, Spring 2026.
```

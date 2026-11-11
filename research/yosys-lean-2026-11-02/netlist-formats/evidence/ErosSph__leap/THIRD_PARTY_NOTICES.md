# Third-party source notices

The release contains selected RTL files, not complete upstream repositories.
The files are copied byte-for-byte from the experiment source subsets. Their
copyright, license and public author notices have been preserved. Exact
commits and file lists are in `config/benchmarks.json`; exact release hashes
are in `theorems/source_hashes.json`.

| Directory / corresponding model | Upstream | License notice in sources |
| --- | --- | --- |
| `aes` | [secworks/aes](https://github.com/secworks/aes) | BSD 2-Clause; Secworks Sweden AB |
| `picorv32` | [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32) | ISC; Claire Xenia Wolf |
| `modexp` | [secworks/modexp](https://github.com/secworks/modexp) | BSD 2-Clause; Assured AB / respective source authors |
| `serv` | [olofk/serv](https://github.com/olofk/serv) | ISC; Olof Kindgren and respective source authors |
| `ethmac` | [freecores/ethmac](https://github.com/freecores/ethmac) | LGPL 2.1 or later; respective OpenCores authors |
| `zipcpu` | [ZipCPU/zipcpu](https://github.com/ZipCPU/zipcpu) | GPL 3 or later; Gisselquist Technology, LLC |
| `dma_axi` | [freecores/dma_axi](https://github.com/freecores/dma_axi) | GNU LGPL; Provartec LTD; source headers do not select a version |

The table summarizes notices; individual file notices govern. In particular,
this repository's MIT license does **not** relicense these RTL designs.
Corresponding generated Lean models and source-derived verification artifacts
under `theorems/` are distributed subject to applicable upstream terms as
well. The original authors do not endorse this framework.

Complete GNU GPL/LGPL texts are provided in `LICENSES/`. The full BSD/ISC
notices occur in source headers; SERV's upstream ISC text is also included as
`LICENSES/SERV-ISC.txt`. Compiler dependencies are installed separately, not
vendored: [slang/pyslang](https://github.com/MikePopoloski/slang) and
[Lean 4](https://github.com/leanprover/lean4) retain their own licenses.

The benchmark RTL sources in `benchmarks/`, the model sources in
`theorems/models/`, the reference proofs and the translation code are included
in editable source form. The frozen benchmark models are preserved from the
experiment; no claim is made that a new translator invocation reproduces
their bytes exactly.

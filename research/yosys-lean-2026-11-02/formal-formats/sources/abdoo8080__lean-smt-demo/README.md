# Lean-SMT Tactic Demo

This project demonstrates how to include and use a full-featured version of the `smt` tactic as a dependency in a Lean project. The `smt` tactic is a powerful tool for proving theorems in Lean, but it is not included in the standard library. This project includes the `smt` tactic as a dependency and demonstrates how to use it to prove theorems.

## Setup

To download the necessary dependencies and Mathlib cache, run the following command:

```sh
lake update
```

The above command does not build dependencies needed to run the example. However, opening `Demo.lean` in VS Code or Emacs should trigger the build, which take some time. To track the build progress, run the following command from the terminal:

```sh
lake build
```

Loading the dependencies in `Demo.lean` after building the dependencies should be much faster.

{ inputs, system, tools }:
import ../direct {
  inherit inputs system tools;
  quantizedRecipe = ./quantized.nix;
  checkRecipe = ./check.nix;
  arithmeticRecipe = ./arithmetic.nix;
  extraPythonPackages = ps: [ (ps.callPackage ./torchao.nix {}) ];
}

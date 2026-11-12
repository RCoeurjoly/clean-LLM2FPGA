{ buildPythonPackage, fetchurl, torch }:
# The toolchain's TorchAO 0.14.1 predates this config's version=2 support.
# This pure-Python wheel works with the pinned PyTorch 2.9.1 CPU operators.
buildPythonPackage {
  pname = "torchao";
  version = "0.15.0";
  format = "wheel";
  dontBuild = true;
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/f6/3b/6b9d5618720f63dbc2e2509cd6b57aae9c0d61b738d1d2172f4d5d9efaab/torchao-0.15.0-py3-none-any.whl";
    hash = "sha256-PzgSZ2BI74oqDp1JLRLYlxunp+uxb1SqVvaQQU4TDSw=";
  };
  dependencies = [ torch ];
  pythonImportsCheck = [ "torchao" ];
}

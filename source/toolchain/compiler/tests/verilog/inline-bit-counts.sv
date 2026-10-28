// Verification-tool regression: inline counts must ignore unused VPI bits.
module tb;
  logic [3:0] one_cold = 4'b1110;
  logic [11:0] one_hot = 12'b0000_0010_0000;
  initial begin
    if (($countones(~one_cold) == 1) !== 1'b1)
      $fatal(1, "inline complement count is incorrect");
    if (($onehot(~one_cold) == 1) !== 1'b1)
      $fatal(1, "inline onehot is incorrect");
    if (($countbits(one_cold, 1'b1) == 3) !== 1'b1)
      $fatal(1, "inline countbits is incorrect");
    if (($countones(one_hot) == 1) !== 1'b1)
      $fatal(1, "12-bit inline count is incorrect");
    $display("INLINE BIT COUNTS PASS");
    $finish;
  end
endmodule

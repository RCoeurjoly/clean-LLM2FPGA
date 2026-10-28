// Compile this primitive wrapper with Yosys's native Verilog frontend, which
// preserves the real-valued MMCM parameters. Slang imports its port boundary.
module tinystories_1m_ypcb_clock (
    input wire clk_50mhz,
    output wire clk_30mhz,
    output wire locked
);
  wire feedback, feedback_buffered, unbuffered;
  // 50 * 18 / 1 = 900 MHz VCO; 900 / 30 = 30 MHz output.
  MMCME2_ADV #(
      .CLKIN1_PERIOD(20.0), .DIVCLK_DIVIDE(1),
      .CLKFBOUT_MULT_F(18.0), .CLKOUT0_DIVIDE_F(30.0),
      .STARTUP_WAIT("FALSE"), .COMPENSATION("ZHOLD")
  ) mmcm (
      .CLKIN1(clk_50mhz), .CLKFBIN(feedback_buffered),
      .CLKFBOUT(feedback), .CLKOUT0(unbuffered),
      .CLKIN2(1'b0), .CLKINSEL(1'b1),
      .DADDR(7'd0), .DCLK(1'b0), .DEN(1'b0), .DI(16'd0), .DWE(1'b0),
      .PSCLK(1'b0), .PSEN(1'b0), .PSINCDEC(1'b0),
      .LOCKED(locked), .PWRDWN(1'b0), .RST(1'b0));
  BUFG feedback_buf (.I(feedback), .O(feedback_buffered));
  BUFG output_buf (.I(unbuffered), .O(clk_30mhz));
endmodule

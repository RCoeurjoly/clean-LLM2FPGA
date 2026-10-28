// Physical on-chip shell for the native graph main ABI. Runtime memories,
// clocking and the JTAG host read path are part of complete-board accounting.
module graph_tinystories_ypcb_top (
    input wire clk,
    output wire [2:0] led
`ifdef BSCAN_SIM
    ,input wire scan_drck, scan_reset, scan_sel, scan_shift, scan_capture,
    input wire scan_update, scan_tdi, output wire scan_tdo
`endif
);
  wire accelerator_clk, clock_locked;
`ifdef BSCAN_SIM
  assign accelerator_clk = clk;
  assign clock_locked = 1'b1;
`else
  tinystories_1m_ypcb_clock clock_generator (
      .clk_50mhz(clk), .clk_30mhz(accelerator_clk), .locked(clock_locked));
`endif
  wire core_start, core_reset, core_valid, done, tap_reset;
  wire [17:0] host_index;
  wire [15:0] host_piece;
  wire [15:0] prompt_0, prompt_1, prompt_2, prompt_3;
  wire [63:0] response_word;
  wire async_reset = tap_reset | !clock_locked;
  (* ASYNC_REG = "TRUE" *) reg [1:0] reset_sync;
  always @(posedge accelerator_clk or posedge async_reset) begin
    if (async_reset) reset_sync <= 2'b11;
    else reset_sync <= {reset_sync[0], 1'b0};
  end
  wire sys_reset = reset_sync[1];
  tinystories_1m_jtag_csr #(.TOKEN_INDEX_WIDTH(18), .TOKEN_READ_LATENCY(3)) csr (
      .sys_clk(accelerator_clk), .sys_rst(sys_reset),
      .core_start(core_start), .core_reset(core_reset), .core_valid(core_valid),
      .core_token_index(host_index), .core_token_id(host_piece), .core_done(done),
      .prompt_0(prompt_0), .prompt_1(prompt_1), .prompt_2(prompt_2), .prompt_3(prompt_3),
      .model_init_done(1'b1), .tap_reset(tap_reset), .response_word(response_word)
`ifdef BSCAN_SIM
      ,.scan_drck(scan_drck), .scan_reset(scan_reset), .scan_sel(scan_sel),
      .scan_shift(scan_shift), .scan_capture(scan_capture), .scan_update(scan_update),
      .scan_tdi(scan_tdi), .scan_tdo(scan_tdo)
`endif
  );
  graph_onchip_runtime runtime (
      .clk(accelerator_clk), .reset(sys_reset | core_reset), .start(core_start),
      .prompt_0(prompt_0), .prompt_1(prompt_1), .prompt_2(prompt_2), .prompt_3(prompt_3),
      .host_index(host_index), .host_piece(host_piece), .done(done));
  assign led = {done, core_valid, clock_locked};
endmodule

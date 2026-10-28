`timescale 1ns/1ps

// JTAG-controlled YPCB top for the compiler-generated BRAM production kernel.
// Model memories are initialized by the bitstream; the JTAG CSR supplies the
// start/reset/read-token control plane without introducing DDR3 or PCIe.
module tinystories_1m_ypcb_bram_production_top #(
    parameter integer TOKEN_INDEX_WIDTH = 1
) (
    input wire clk,
    output wire [2:0] led
`ifdef BSCAN_SIM
    ,input wire scan_drck, input wire scan_reset, input wire scan_sel,
    input wire scan_shift, input wire scan_capture, input wire scan_update,
    input wire scan_tdi, output wire scan_tdo
`endif
);
  (* keep *) wire accelerator_clk;
  wire clock_locked;
`ifdef BSCAN_SIM
  assign accelerator_clk = clk;
  assign clock_locked = 1'b1;
`else
  tinystories_1m_ypcb_clock clock_generator (
      .clk_50mhz(clk), .clk_30mhz(accelerator_clk), .locked(clock_locked));
`endif
  wire start, valid, reset, done;
  wire [TOKEN_INDEX_WIDTH-1:0] token_index;
  (* keep *) wire [15:0] token_id;
  wire [15:0] prompt_0, prompt_1, prompt_2, prompt_3;
  (* keep *) wire [63:0] response_word;
  wire csr_reset;
  wire tap_reset;
  wire csr_done = done;
  wire csr_valid = valid;
  // All immutable source memories are initialized by the configured image's
  // synthesis-time $readmemh initializers.  There is no runtime weight load
  // transaction in this BRAM-only milestone.
  wire model_init_done = 1'b1;
  // The TAP reset is asserted during the standard five-TMS-high JTAG reset
  // sequence.  Feed it into the system-clock domain so synthesized CSR state
  // is initialized deterministically; relying on FPGA FF INIT values left the
  // hardware-only path uninitialized.
  wire async_reset = tap_reset | !clock_locked;
  (* ASYNC_REG = "TRUE" *) reg [1:0] reset_sync;
  always @(posedge accelerator_clk or posedge async_reset) begin
    if (async_reset) reset_sync <= 2'b11;
    else reset_sync <= {reset_sync[0], 1'b0};
  end
  wire sys_reset = reset_sync[1];

  tinystories_1m_jtag_csr #(.TOKEN_INDEX_WIDTH(TOKEN_INDEX_WIDTH)) csr (
      .sys_clk(accelerator_clk), .sys_rst(sys_reset), .core_start(start), .core_reset(csr_reset),
      .core_valid(valid), .core_token_index(token_index),
      .prompt_0(prompt_0), .prompt_1(prompt_1), .prompt_2(prompt_2), .prompt_3(prompt_3),
      .core_token_id(token_id), .core_done(csr_done),
      .model_init_done(model_init_done), .tap_reset(tap_reset)
`ifdef BSCAN_SIM
      , .scan_drck(scan_drck), .scan_reset(scan_reset), .scan_sel(scan_sel),
      .scan_shift(scan_shift), .scan_capture(scan_capture), .scan_update(scan_update),
      .scan_tdi(scan_tdi), .scan_tdo(scan_tdo)
`endif
      ,
      .response_word(response_word));
  assign reset = csr_reset | sys_reset;
  (* keep_hierarchy *) main core (
      .clk(accelerator_clk),
      .reset(reset),
      .start(start),
      .valid(csr_valid),
      .prompt_0(prompt_0), .prompt_1(prompt_1), .prompt_2(prompt_2), .prompt_3(prompt_3),
      .token_index(token_index),
      .token_id(token_id),
      .done(done));
  assign led = {done, valid, accelerator_clk};
endmodule

`timescale 1ns/1ps

// Minimal YPCB JTAG CSR bridge for the compiler-generated BRAM kernel.
//
// The bridge deliberately uses a fixed 64-bit transaction rather than the
// kev-gpt packet stream: this keeps the compiler milestone small and makes the
// board ABI independently testable.  A transaction is shifted LSB-first:
//   command[63:60] op, command[17:16] prompt index, command[15:0] value.
//   1 = load prompt word, 2 = start, 3 = status, 4 = select/read token.
//   5 = reset, 6 = read stored prompt word (response[15:0], upper bits zero).
// This replaces the legacy overlapping prompt-index/value encoding.
// Responses are captured from response_word with status in [63:60] and the
// selected token in [15:0].  Model weights remain immutable BRAM init data.
module tinystories_1m_jtag_csr #(
    parameter integer PROMPT_WORDS = 4,
    parameter integer TOKEN_INDEX_WIDTH = 1,
    parameter integer TOKEN_READ_LATENCY = 1
) (
    input  wire        sys_clk,
    input  wire        sys_rst,
    output reg         core_start,
    output reg         core_reset,
    output wire        core_valid,
    output reg  [TOKEN_INDEX_WIDTH-1:0]  core_token_index,
    output wire [15:0] prompt_0,
    output wire [15:0] prompt_1,
    output wire [15:0] prompt_2,
    output wire [15:0] prompt_3,
    input  wire [15:0] core_token_id,
    input  wire        core_done,
    // The production image initializes immutable model memories from
    // $readmemh at configuration time.  Keep this explicit in the ABI so a
    // host can distinguish an initialized image from a merely responsive
    // shell.
    input  wire        model_init_done,
    output reg  [63:0] response_word,
    output wire        tap_reset
`ifdef BSCAN_SIM
    ,input wire        scan_drck,
    input  wire        scan_reset,
    input  wire        scan_sel,
    input  wire        scan_shift,
    input  wire        scan_capture,
    input  wire        scan_update,
    input  wire        scan_tdi,
    output wire        scan_tdo
`endif
);
`ifndef BSCAN_SIM
  wire scan_drck, scan_reset, scan_sel, scan_shift, scan_capture, scan_update;
  wire scan_tdi, scan_tdo, scan_runtest, scan_tck, scan_tms;
  BSCANE2 #(.DISABLE_JTAG("FALSE"), .JTAG_CHAIN(1)) bscan (
      .CAPTURE(scan_capture), .DRCK(scan_drck), .RESET(scan_reset),
      .RUNTEST(scan_runtest), .SEL(scan_sel), .SHIFT(scan_shift), .TCK(scan_tck),
      .TDI(scan_tdi), .TDO(scan_tdo), .TMS(scan_tms), .UPDATE(scan_update));
  wire unused_scan_runtest = scan_runtest;
  wire unused_scan_tck = scan_tck;
  wire unused_scan_tms = scan_tms;
`endif

  reg [63:0] scan_shift_reg;
  reg [6:0] scan_shift_count;
  reg scan_bit60;
`ifdef BSCAN_DIAGNOSTIC
  reg [7:0] diag_capture_count;
  reg [7:0] diag_shift_count;
  reg [7:0] diag_update_count;
`endif
  /* verilator lint_off UNUSEDSIGNAL */
  reg [63:0] command_word;
  /* verilator lint_on UNUSEDSIGNAL */
  reg        command_toggle;
  reg        command_toggle_q1, command_toggle_q2;
  reg        run_active;
  reg        start_pending;
  reg        done_latched;
  reg        token_read_pending;
  localparam READ_COUNT_WIDTH = TOKEN_READ_LATENCY < 2 ? 1 : $clog2(TOKEN_READ_LATENCY + 1);
  reg [READ_COUNT_WIDTH-1:0] token_read_remaining;
  reg [15:0] prompt_words [0:PROMPT_WORDS-1];
  integer i;

  assign core_valid = run_active;
  assign tap_reset = scan_reset;
  assign prompt_0 = prompt_words[0];
  assign prompt_1 = prompt_words[1];
  assign prompt_2 = prompt_words[2];
  assign prompt_3 = prompt_words[3];
  assign scan_tdo = scan_shift_reg[0];

  // DRCK clocks only Capture/Shift. It is held high during Update-DR.
  always @(posedge scan_drck or posedge scan_reset) begin
    if (scan_reset) begin
      scan_shift_reg <= 64'd0;
      scan_shift_count <= 7'd0;
      scan_bit60 <= 1'b0;
`ifdef BSCAN_DIAGNOSTIC
      diag_capture_count <= 8'd0;
      diag_shift_count <= 8'd0;
`endif
    end else if (scan_sel && scan_capture) begin
`ifdef BSCAN_DIAGNOSTIC
      // Diagnostic captures bypass the CSR response path entirely.  The
      // marker and event counters make physical bit alignment observable even
      // when UPDATE command decoding is being investigated.
      scan_shift_reg <= {8'hd3, diag_capture_count, diag_shift_count,
                         diag_update_count, 32'hc0de5a17};
`elsif BSCAN_SIM_PHYSICAL
      // Exercise the same response preload as the physical primitive through
      // the explicit BSCAN_SIM ports.
      scan_shift_reg <= response_word;
`elsif BSCAN_SIM
      scan_shift_reg <= response_word;
`else
      // The host samples TDO before each shift edge. Pre-shifting here caused
      // a measured one-bit-right response on YPCB for STATUS and loopback.
      scan_shift_reg <= response_word;
`endif
      scan_shift_count <= 7'd0;
`ifdef BSCAN_DIAGNOSTIC
      diag_capture_count <= diag_capture_count + 8'd1;
`endif
    end else if (scan_sel && scan_shift) begin
      scan_shift_reg <= {scan_tdi, scan_shift_reg[63:1]};
      if (scan_shift_count == 7'd60)
        scan_bit60 <= scan_tdi;
      scan_shift_count <= scan_shift_count + 7'd1;
`ifdef BSCAN_DIAGNOSTIC
      diag_shift_count <= diag_shift_count + 8'd1;
`endif
    end
  end

  // Latch the stable shifted command on the primitive's UPDATE edge. Waiting
  // for a DRCK edge here loses the transaction on actual BSCANE2 hardware.
  always @(posedge scan_update or posedge scan_reset) begin
    if (scan_reset) begin
      command_word <= 64'd0;
      command_toggle <= 1'b0;
`ifdef BSCAN_DIAGNOSTIC
      diag_update_count <= 8'd0;
`endif
    end else if (scan_sel) begin
      // Physical BSCANE2 may omit the final DRCK shift when TMS exits
      // Shift-DR. The last TDI bit is still present at UPDATE, so restore
      // command bit 63 explicitly instead of losing the opcode MSB.
      command_word <= {scan_shift_reg[63:61], scan_bit60, scan_shift_reg[59:0]};
      command_toggle <= ~command_toggle;
`ifdef BSCAN_DIAGNOSTIC
      diag_update_count <= diag_update_count + 8'd1;
`endif
    end
  end

  // System clock domain: synchronize the update event and execute the CSR.
  always @(posedge sys_clk) begin
    if (sys_rst) begin
      command_toggle_q1 <= 1'b0;
      command_toggle_q2 <= 1'b0;
      core_start <= 1'b0;
      core_reset <= 1'b0;
      core_token_index <= 1'b0;
      run_active <= 1'b0;
      start_pending <= 1'b0;
      done_latched <= 1'b0;
      token_read_pending <= 1'b0;
      token_read_remaining <= 0;
      response_word <= 64'd0;
      for (i = 0; i < PROMPT_WORDS; i = i + 1) prompt_words[i] <= 16'd0;
    end else begin
      command_toggle_q1 <= command_toggle;
      command_toggle_q2 <= command_toggle_q1;
      core_reset <= 1'b0;
      if (start_pending) begin
        // Present start only after run_active has become visible on the
        // generated core's valid input.  This avoids a one-cycle valid/start
        // skew at the CDC boundary. Calyx requires go held until done;
        // this is not a one-cycle start pulse.
        core_start <= 1'b1;
        start_pending <= 1'b0;
      end
      // The index changes with a nonblocking assignment. One edge suffices
      // for the original combinational mux; clocked runtime storage selects
      // a larger TOKEN_READ_LATENCY before its value is captured.
      if (token_read_pending) begin
        if (token_read_remaining > 1) token_read_remaining <= token_read_remaining - 1'b1;
        else begin
          response_word <= {4'h0, 12'd0, 16'd0, 16'd0, core_token_id};
          token_read_pending <= 1'b0;
          token_read_remaining <= 0;
        end
      end
      if (core_done) begin
        core_start <= 1'b0;
        run_active <= 1'b0;
        done_latched <= 1'b1;
        response_word <= {4'h0, 12'd0, 16'd0, 16'd0, 16'd0};
      end
      if (command_toggle_q2 != command_toggle_q1) begin
        case (command_word[63:60])
          4'h1: begin
            prompt_words[command_word[17:16]] <= command_word[15:0];
            response_word <= {4'h0, 12'd0, 16'd0, 16'd0, command_word[15:0]};
          end
          4'h2: begin
            run_active <= 1'b1;
            start_pending <= 1'b1;
            done_latched <= 1'b0;
            response_word <= {4'h1, 12'd0, 48'd0};
          end
          4'h5: begin
            core_start <= 1'b0;
            core_reset <= 1'b1;
            run_active <= 1'b0;
            start_pending <= 1'b0;
            done_latched <= 1'b0;
            token_read_pending <= 1'b0;
            token_read_remaining <= 0;
            for (i = 0; i < PROMPT_WORDS; i = i + 1) prompt_words[i] <= 16'd0;
            response_word <= {4'h0, 60'd0};
          end
          4'h3: begin
            // status[59] = immutable model image initialized,
            // status[58] = completion latched, status[57] = currently busy.
`ifdef BSCAN_DIAGNOSTIC
            response_word <= {4'h2, model_init_done, done_latched, run_active,
                              1'b0, diag_capture_count, diag_shift_count,
                              diag_update_count, 16'd0, prompt_words[0]};
`else
            response_word <= {4'h2, model_init_done, done_latched, run_active,
                              9'd0, 32'd0, prompt_words[0]};
`endif
          end
          4'h4: begin
            core_token_index <= command_word[TOKEN_INDEX_WIDTH-1:0];
            token_read_pending <= 1'b1;
            token_read_remaining <= TOKEN_READ_LATENCY;
          end
          4'h6: response_word <= {48'd0, prompt_words[command_word[17:16]]};
          // Diagnostic-only command loopback; production clients never use
          // opcode 7.  It exposes the exact word captured at UPDATE.
          4'h7: response_word <= command_word;
          default: response_word <= {4'hf, 60'd0};
        endcase
      end
    end
  end
endmodule

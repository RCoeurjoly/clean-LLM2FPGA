// Single outstanding native read across unrelated accelerator and UI clocks.
// Toggle synchronizers transfer ownership of stable bundled payloads. Request
// address is held until UI retirement; response data is held until core consumes
// it. No second request is accepted before the UI acknowledges retirement.
//
// Physical integration MUST constrain the request/response payload paths to
// settle before their destination samples them, and retain ASYNC_REG stages.
// Digital simulation does not establish metastability or physical timing.
// reset is global and must also reset/drain the native controller read port;
// resetting only one domain while a request is outstanding is unsupported.
module closure_ddr_read_cdc #(
  parameter integer FRAME_DATA_WIDTH = 576,
  parameter integer FRAME_ADDRESS_WIDTH = 25
)(
  input wire core_clk, ui_clk, reset, ui_memory_ready,
  output wire core_memory_ready,
  output reg fault,
  input wire core_cmd_valid,
  output wire core_cmd_ready,
  input wire core_cmd_we,
  input wire [FRAME_ADDRESS_WIDTH-1:0] core_cmd_addr,
  output wire core_rdata_valid,
  input wire core_rdata_ready,
  output reg [FRAME_DATA_WIDTH-1:0] core_rdata,
  output wire ui_cmd_valid,
  input wire ui_cmd_ready,
  output wire ui_cmd_we,
  output reg [FRAME_ADDRESS_WIDTH-1:0] ui_cmd_addr,
  input wire ui_rdata_valid,
  output wire ui_rdata_ready,
  input wire [FRAME_DATA_WIDTH-1:0] ui_rdata
);
  localparam [1:0] CORE_IDLE=0, CORE_WAIT=1, CORE_OUTPUT=2, CORE_RETIRE=3;
  localparam [1:0] UI_IDLE=0, UI_COMMAND=1, UI_RESPONSE=2, UI_RETIRE=3;
  reg [1:0] core_state, ui_state;
  reg request_toggle, consumed_toggle, response_toggle, retired_toggle;
  reg [FRAME_ADDRESS_WIDTH-1:0] request_payload;
  reg [FRAME_DATA_WIDTH-1:0] response_payload;

  // Reset deasserts synchronously in each domain after two local clock edges.
  (* ASYNC_REG = "TRUE" *) reg [1:0] core_reset_pipe, ui_reset_pipe;
  wire core_reset = core_reset_pipe[1];
  wire ui_reset = ui_reset_pipe[1];
  always @(posedge core_clk or posedge reset)
    if (reset) core_reset_pipe <= 2'b11;
    else core_reset_pipe <= {core_reset_pipe[0],1'b0};
  always @(posedge ui_clk or posedge reset)
    if (reset) ui_reset_pipe <= 2'b11;
    else ui_reset_pipe <= {ui_reset_pipe[0],1'b0};

  (* ASYNC_REG = "TRUE" *) reg request_meta, request_sync;
  (* ASYNC_REG = "TRUE" *) reg consumed_meta, consumed_sync;
  (* ASYNC_REG = "TRUE" *) reg response_meta, response_sync;
  (* ASYNC_REG = "TRUE" *) reg retired_meta, retired_sync;
  (* ASYNC_REG = "TRUE" *) reg ready_meta, ready_sync;

  always @(posedge ui_clk) begin
    if (ui_reset) begin
      request_meta <= 0; request_sync <= 0;
      consumed_meta <= 0; consumed_sync <= 0;
    end else begin
      request_meta <= request_toggle; request_sync <= request_meta;
      consumed_meta <= consumed_toggle; consumed_sync <= consumed_meta;
    end
  end
  always @(posedge core_clk) begin
    if (core_reset) begin
      response_meta <= 0; response_sync <= 0;
      retired_meta <= 0; retired_sync <= 0;
      ready_meta <= 0; ready_sync <= 0;
    end else begin
      response_meta <= response_toggle; response_sync <= response_meta;
      retired_meta <= retired_toggle; retired_sync <= retired_meta;
      ready_meta <= ui_memory_ready; ready_sync <= ready_meta;
    end
  end

  assign core_memory_ready = ready_sync && !core_reset && !reset && !fault;
  assign core_cmd_ready = core_state == CORE_IDLE && core_memory_ready;
  assign core_rdata_valid = core_state == CORE_OUTPUT && !core_reset && !reset && !fault;
  assign ui_cmd_valid = ui_state == UI_COMMAND && !ui_reset && !reset;
  assign ui_cmd_we = 1'b0;
  assign ui_rdata_ready = ui_state == UI_RESPONSE && !ui_reset && !reset;

`ifndef SYNTHESIS
  initial if (FRAME_DATA_WIDTH < 64 || FRAME_DATA_WIDTH % 64 != 0 ||
      FRAME_ADDRESS_WIDTH < 1 || FRAME_ADDRESS_WIDTH > 32)
    $fatal(1, "invalid native DDR CDC geometry");
`endif

  always @(posedge core_clk) begin
    if (core_reset) begin
      core_state <= CORE_IDLE;
      request_toggle <= 0;
      consumed_toggle <= 0;
      request_payload <= 0;
      core_rdata <= 0;
      fault <= 0;
    end else if (!fault) begin
      case (core_state)
        CORE_IDLE: if (core_cmd_valid) begin
          if (core_cmd_we) fault <= 1;
          else if (core_cmd_ready) begin
            request_payload <= core_cmd_addr;
            request_toggle <= !request_toggle;
            core_state <= CORE_WAIT;
          end
        end
        CORE_WAIT: if (response_sync == request_toggle) begin
          core_rdata <= response_payload;
          core_state <= CORE_OUTPUT;
        end
        CORE_OUTPUT: if (core_rdata_ready) begin
          consumed_toggle <= request_toggle;
          core_state <= CORE_RETIRE;
        end
        CORE_RETIRE: if (retired_sync == request_toggle)
          core_state <= CORE_IDLE;
        default: fault <= 1;
      endcase
    end
  end

  always @(posedge ui_clk) begin
    if (ui_reset) begin
      ui_state <= UI_IDLE;
      ui_cmd_addr <= 0;
      response_payload <= 0;
      response_toggle <= 0;
      retired_toggle <= 0;
    end else begin
      case (ui_state)
        UI_IDLE: if (request_sync != retired_toggle && ui_memory_ready) begin
          ui_cmd_addr <= request_payload;
          ui_state <= UI_COMMAND;
        end
        UI_COMMAND: if (ui_cmd_ready) ui_state <= UI_RESPONSE;
        UI_RESPONSE: if (ui_rdata_valid) begin
          response_payload <= ui_rdata;
          response_toggle <= request_sync;
          ui_state <= UI_RETIRE;
        end
        UI_RETIRE: if (consumed_sync == response_toggle) begin
          retired_toggle <= response_toggle;
          ui_state <= UI_IDLE;
        end
        default: ui_state <= UI_IDLE;
      endcase
    end
  end
endmodule

// One native controller port: verified image loading, then immutable inference.
// Loader owns every command/write/response until its final readback completes.
// Only then can the CDC bridge accept accelerator requests. The ownership bit
// is local to ui_clk; readiness crosses to core_clk through the existing bridge.
// No model answers are embedded: streamed input frames are checked by readback.
//
// Reset must reset/drain the native controller as well as both clock domains.
// CDC payload constraints and ASYNC_REG retention remain integration obligations.
// ui_loaded/ui_load_fault are UI-domain status; core_memory_ready/core_fault are
// core-domain status. Physical controller/PHY qualification is separate.
module closure_ddr_loaded_port #(
  parameter integer FRAME_DATA_WIDTH = 576,
  parameter integer FRAME_ADDRESS_WIDTH = 25,
  parameter integer FRAME_COUNT = 1
)(
  input wire core_clk, ui_clk, reset, controller_ready,
  input wire input_valid,
  output wire input_ready,
  input wire [FRAME_DATA_WIDTH-1:0] input_data,
  output wire [FRAME_ADDRESS_WIDTH-1:0] frame_index,
  output wire ui_loaded, ui_load_fault, core_fault, core_memory_ready,
  input wire core_cmd_valid,
  output wire core_cmd_ready,
  input wire core_cmd_we,
  input wire [FRAME_ADDRESS_WIDTH-1:0] core_cmd_addr,
  output wire core_rdata_valid,
  input wire core_rdata_ready,
  output wire [FRAME_DATA_WIDTH-1:0] core_rdata,
  output wire cmd_valid,
  input wire cmd_ready,
  output wire cmd_we,
  output wire [FRAME_ADDRESS_WIDTH-1:0] cmd_addr,
  output wire wdata_valid,
  input wire wdata_ready,
  output wire [FRAME_DATA_WIDTH-1:0] wdata,
  output wire [FRAME_DATA_WIDTH/8-1:0] wdata_we,
  input wire rdata_valid,
  output wire rdata_ready,
  input wire [FRAME_DATA_WIDTH-1:0] rdata
);
  wire inference_owner = ui_loaded && !ui_load_fault;
  wire load_cmd_valid, load_cmd_we, load_wdata_valid, load_rdata_ready;
  wire [FRAME_ADDRESS_WIDTH-1:0] load_cmd_addr;
  wire [FRAME_DATA_WIDTH-1:0] load_wdata;
  wire [FRAME_DATA_WIDTH/8-1:0] load_wdata_we;
  wire read_cmd_valid, read_cmd_we, read_rdata_ready;
  wire [FRAME_ADDRESS_WIDTH-1:0] read_cmd_addr;

  closure_ddr_image_loader #(
    .FRAME_DATA_WIDTH(FRAME_DATA_WIDTH),
    .FRAME_ADDRESS_WIDTH(FRAME_ADDRESS_WIDTH),.FRAME_COUNT(FRAME_COUNT)
  ) loader (
    .clk(ui_clk),.reset(reset),.controller_ready(controller_ready),
    .input_valid(input_valid),.input_ready(input_ready),.input_data(input_data),
    .frame_index(frame_index),.loaded(ui_loaded),.fault(ui_load_fault),
    .cmd_valid(load_cmd_valid),.cmd_ready(cmd_ready && !inference_owner),
    .cmd_we(load_cmd_we),.cmd_addr(load_cmd_addr),
    .wdata_valid(load_wdata_valid),.wdata_ready(wdata_ready && !inference_owner),
    .wdata(load_wdata),.wdata_we(load_wdata_we),
    .rdata_valid(rdata_valid && !inference_owner),
    .rdata_ready(load_rdata_ready),.rdata(rdata)
  );
  closure_ddr_read_cdc #(
    .FRAME_DATA_WIDTH(FRAME_DATA_WIDTH),.FRAME_ADDRESS_WIDTH(FRAME_ADDRESS_WIDTH)
  ) reader (
    .core_clk(core_clk),.ui_clk(ui_clk),.reset(reset),
    .ui_memory_ready(controller_ready && inference_owner),
    .core_memory_ready(core_memory_ready),.fault(core_fault),
    .core_cmd_valid(core_cmd_valid),.core_cmd_ready(core_cmd_ready),
    .core_cmd_we(core_cmd_we),.core_cmd_addr(core_cmd_addr),
    .core_rdata_valid(core_rdata_valid),.core_rdata_ready(core_rdata_ready),
    .core_rdata(core_rdata),.ui_cmd_valid(read_cmd_valid),
    .ui_cmd_ready(cmd_ready && inference_owner),.ui_cmd_we(read_cmd_we),
    .ui_cmd_addr(read_cmd_addr),.ui_rdata_valid(rdata_valid && inference_owner),
    .ui_rdata_ready(read_rdata_ready),.ui_rdata(rdata)
  );
  assign cmd_valid = inference_owner ? read_cmd_valid : load_cmd_valid;
  assign cmd_we = inference_owner ? read_cmd_we : load_cmd_we;
  assign cmd_addr = inference_owner ? read_cmd_addr : load_cmd_addr;
  assign wdata_valid = load_wdata_valid && !inference_owner;
  assign wdata = load_wdata;
  assign wdata_we = load_wdata_we;
  assign rdata_ready = inference_owner ? read_rdata_ready : load_rdata_ready;
endmodule

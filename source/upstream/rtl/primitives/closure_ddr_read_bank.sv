// One shared-clock immutable memory bank and one native controller read port.
// Clients hold address/en through completion, then retire en independently.
// Completed clients do not block other clients. Only one native request is
// outstanding; round-robin arbitration prevents a recurring client starving
// another. Faults require coordinated reset of this bank and controller port.
module closure_ddr_read_bank #(
  parameter integer CLIENTS = 1,
  parameter integer FRAME_DATA_WIDTH = 576,
  parameter integer FRAME_ADDRESS_WIDTH = 25,
  parameter integer FRAME_COUNT = 1,
  parameter [CLIENTS*32-1:0] DEPTHS = 1,
  parameter [CLIENTS*32-1:0] BASE_FRAMES = 0
)(
  input wire clk, reset, memory_ready,
  input wire [CLIENTS-1:0] content_en, write_en,
  input wire [CLIENTS*32-1:0] addr,
  output reg [CLIENTS*64-1:0] read_data,
  output wire [CLIENTS-1:0] done,
  output reg fault,
  output wire cmd_valid,
  input wire cmd_ready,
  output wire cmd_we,
  output wire [FRAME_ADDRESS_WIDTH-1:0] cmd_addr,
  input wire rdata_valid,
  output wire rdata_ready,
  input wire [FRAME_DATA_WIDTH-1:0] rdata
);
  localparam integer SLOTS = FRAME_DATA_WIDTH >= 64 ? FRAME_DATA_WIDTH / 64 : 1;
  localparam [1:0] IDLE = 0, ACTIVE = 1, RETIRE = 2, FAILED = 3;
  reg [1:0] state;
  reg [CLIENTS-1:0] completed;
  reg [CLIENTS*32-1:0] accepted_addr;
  integer next_client, active_client;
  reg port_enable;
  reg [31:0] port_addr;
  wire [63:0] port_data;
  wire port_done, port_fault, port_cmd_valid, port_rdata_ready;
  integer selected, i;
  reg violation;

  assign done = fault ? {CLIENTS{1'b0}} : completed;
  assign cmd_valid = port_cmd_valid && !fault;
  assign rdata_ready = port_rdata_ready && !fault;

  closure_ddr_read_port #(.WIDTH(64), .DEPTH(FRAME_COUNT*SLOTS),
    .ADDRESS_WIDTH(32), .FRAME_DATA_WIDTH(FRAME_DATA_WIDTH),
    .FRAME_ADDRESS_WIDTH(FRAME_ADDRESS_WIDTH)) reader(
    .clk(clk), .reset(reset), .memory_ready(memory_ready),
    .content_en(port_enable), .write_en(1'b0), .addr(port_addr),
    .read_data(port_data), .done(port_done), .fault(port_fault),
    .cmd_valid(port_cmd_valid), .cmd_ready(cmd_ready && !fault),
    .cmd_we(cmd_we), .cmd_addr(cmd_addr), .rdata_valid(rdata_valid && !fault),
    .rdata_ready(port_rdata_ready), .rdata(rdata));

  always @* begin
    selected = -1;
    violation = 0;
    // Ascending priority within each half implements a rotating grant point.
    for (i = 0; i < CLIENTS; i = i + 1) begin
      if (i >= next_client && content_en[i] && !completed[i] && selected == -1)
        selected = i;
    end
    for (i = 0; i < CLIENTS; i = i + 1) begin
      if (i < next_client && content_en[i] && !completed[i] && selected == -1)
        selected = i;
      if (content_en[i] && (memory_ready || completed[i] ||
          (state == ACTIVE && i == active_client))) begin
        if (write_en[i] || addr[i*32 +: 32] >= DEPTHS[i*32 +: 32])
          violation = 1;
        if ((completed[i] || (state == ACTIVE && i == active_client)) &&
            addr[i*32 +: 32] != accepted_addr[i*32 +: 32])
          violation = 1;
      end
    end
    if (state == ACTIVE && !content_en[active_client]) violation = 1;
  end

`ifndef SYNTHESIS
  integer geometry_client, geometry_other;
  initial begin
    if (CLIENTS < 1 || FRAME_COUNT < 1 || FRAME_DATA_WIDTH < 64 ||
        FRAME_DATA_WIDTH % 64 != 0 || FRAME_ADDRESS_WIDTH < 1 ||
        FRAME_ADDRESS_WIDTH > 32 || FRAME_COUNT > (64'd1 << FRAME_ADDRESS_WIDTH) ||
        64'd1*FRAME_COUNT*SLOTS > 64'hffffffff)
      $fatal(1, "invalid immutable DDR bank geometry");
    for (geometry_client = 0; geometry_client < CLIENTS; geometry_client = geometry_client + 1) begin
      if (DEPTHS[geometry_client*32 +: 32] == 0 ||
          BASE_FRAMES[geometry_client*32 +: 32] +
          (64'd1*DEPTHS[geometry_client*32 +: 32] + SLOTS-1)/SLOTS > FRAME_COUNT)
        $fatal(1, "DDR client outside allocated image frames");
      for (geometry_other = 0; geometry_other < geometry_client; geometry_other = geometry_other + 1)
        if (BASE_FRAMES[geometry_client*32 +: 32] < BASE_FRAMES[geometry_other*32 +: 32] +
              (64'd1*DEPTHS[geometry_other*32 +: 32]+SLOTS-1)/SLOTS &&
            BASE_FRAMES[geometry_other*32 +: 32] < BASE_FRAMES[geometry_client*32 +: 32] +
              (64'd1*DEPTHS[geometry_client*32 +: 32]+SLOTS-1)/SLOTS)
          $fatal(1, "overlapping immutable DDR client images");
    end
  end
`endif

  always @(posedge clk) begin
    if (reset) begin
      state <= IDLE;
      completed <= 0;
      accepted_addr <= 0;
      next_client <= 0;
      active_client <= 0;
      port_enable <= 0;
      port_addr <= 0;
      read_data <= 0;
      fault <= 0;
    end else if (violation || port_fault) begin
      fault <= 1;
      state <= FAILED;
      port_enable <= 0;
      completed <= 0;
    end else begin
      completed <= completed & content_en;
      case (state)
        IDLE: if (memory_ready && selected != -1) begin
          active_client <= selected;
          next_client <= selected == CLIENTS-1 ? 0 : selected+1;
          accepted_addr[selected*32 +: 32] <= addr[selected*32 +: 32];
          port_addr <= BASE_FRAMES[selected*32 +: 32]*SLOTS + addr[selected*32 +: 32];
          port_enable <= 1;
          state <= ACTIVE;
        end
        ACTIVE: if (port_done) begin
          read_data[active_client*64 +: 64] <= port_data;
          completed[active_client] <= 1;
          port_enable <= 0;
          state <= RETIRE;
        end
        RETIRE: if (!port_done) state <= IDLE;
        FAILED: begin
          completed <= 0;
          port_enable <= 0;
        end
        default: begin
          fault <= 1;
          state <= FAILED;
          completed <= 0;
          port_enable <= 0;
        end
      endcase
    end
  end
endmodule

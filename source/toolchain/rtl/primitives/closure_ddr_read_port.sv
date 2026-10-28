// Immutable logical memory over a controller-native read port.
// Both interfaces use clk. This module does not cross clock domains or arbitrate
// multiple clients. memory_ready gates new transactions; command and response
// handshakes remain independent. Completion is held until content_en retires.
module closure_ddr_read_port #(
  parameter integer WIDTH = 64,
  parameter integer DEPTH = 1,
  parameter integer ADDRESS_WIDTH = 1,
  parameter integer FRAME_DATA_WIDTH = 576,
  parameter integer FRAME_ADDRESS_WIDTH = 25,
  parameter integer BASE_FRAME = 0
)(
  input wire clk, reset, memory_ready,
  input wire content_en, write_en,
  input wire [ADDRESS_WIDTH-1:0] addr,
  output reg [WIDTH-1:0] read_data,
  output reg done, fault,
  output wire cmd_valid,
  input wire cmd_ready,
  output wire cmd_we,
  output reg [FRAME_ADDRESS_WIDTH-1:0] cmd_addr,
  input wire rdata_valid,
  output wire rdata_ready,
  input wire [FRAME_DATA_WIDTH-1:0] rdata
);
  localparam integer SLOTS = FRAME_DATA_WIDTH >= 64 ? FRAME_DATA_WIDTH / 64 : 1;
  localparam [2:0] IDLE = 0, COMMAND = 1, RESPONSE = 2, COMPLETE = 3, FAILED = 4;
  reg [2:0] state;
  reg [ADDRESS_WIDTH-1:0] accepted_addr;
  integer lane;

  assign cmd_valid = state == COMMAND;
  assign cmd_we = 1'b0;
  assign rdata_ready = state == RESPONSE;

`ifndef SYNTHESIS
  initial begin
    if (WIDTH < 1 || WIDTH > 64 || DEPTH < 1 || ADDRESS_WIDTH < 1 ||
        FRAME_DATA_WIDTH < 64 || FRAME_DATA_WIDTH % 64 != 0 ||
        FRAME_ADDRESS_WIDTH < 1 || FRAME_ADDRESS_WIDTH > 32 || BASE_FRAME < 0 ||
        BASE_FRAME + ((64'd1 * DEPTH + SLOTS - 1) / SLOTS) > (64'd1 << FRAME_ADDRESS_WIDTH))
      $fatal(1, "invalid immutable DDR read-port geometry");
  end
`endif

  always @(posedge clk) begin
    if (reset) begin
      state <= IDLE;
      accepted_addr <= 0;
      lane <= 0;
      cmd_addr <= 0;
      read_data <= 0;
      done <= 0;
      fault <= 0;
    end else begin
      case (state)
        IDLE: begin
          done <= 0;
          if (content_en && memory_ready) begin
            if (write_en || addr >= DEPTH) begin
              fault <= 1;
              state <= FAILED;
            end else begin
              accepted_addr <= addr;
              lane <= addr % SLOTS;
              cmd_addr <= BASE_FRAME + addr / SLOTS;
              state <= COMMAND;
            end
          end
        end
        COMMAND: begin
          if (!content_en || write_en || addr != accepted_addr) begin
            fault <= 1;
            state <= FAILED;
          end else if (cmd_ready) begin
            state <= RESPONSE;
          end
        end
        RESPONSE: begin
          if (!content_en || write_en || addr != accepted_addr) begin
            fault <= 1;
            state <= FAILED;
          end else if (rdata_valid) begin
            read_data <= rdata[lane * 64 +: WIDTH];
            done <= 1;
            state <= COMPLETE;
          end
        end
        COMPLETE: begin
          if (!content_en) begin
            done <= 0;
            state <= IDLE;
          end else if (write_en || addr != accepted_addr) begin
            done <= 0;
            fault <= 1;
            state <= FAILED;
          end
        end
        FAILED: begin
          // Protocol violations require a coordinated reset before reuse.
          done <= 0;
        end
        default: begin
          done <= 0;
          fault <= 1;
          state <= FAILED;
        end
      endcase
    end
  end
endmodule

// Stream declared image frames into one controller-native port before inference.
// One frame is retained at a time. Write command/data handshakes are independent;
// acceptance alone does not assert loaded. Each frame is read back and compared
// in full before accepting the next input frame. This checks visibility and lane
// order without retaining the entire model on-chip or embedding expected outputs.
//
// All interfaces use clk. The native port must preserve per-port write/read
// ordering. Physical controller qualification is separate. Reset must also
// reset/drain that port; resetting the loader alone mid-transaction is invalid.
module closure_ddr_image_loader #(
  parameter integer FRAME_DATA_WIDTH = 576,
  parameter integer FRAME_ADDRESS_WIDTH = 25,
  parameter integer FRAME_COUNT = 1
)(
  input wire clk, reset, controller_ready,
  input wire input_valid,
  output wire input_ready,
  input wire [FRAME_DATA_WIDTH-1:0] input_data,
  output reg [FRAME_ADDRESS_WIDTH-1:0] frame_index,
  output reg loaded, fault,
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
  localparam [2:0] IDLE=0, WRITE=1, READ_COMMAND=2, READ_RESPONSE=3,
                   COMPLETE=4, FAILED=5;
  reg [2:0] state;
  reg [FRAME_DATA_WIDTH-1:0] accepted_frame;
  reg command_sent, data_sent;

  assign input_ready = state == IDLE && controller_ready && !reset;
  assign cmd_valid = ((state == WRITE && !command_sent) || state == READ_COMMAND) && !reset;
  assign cmd_we = state == WRITE;
  assign cmd_addr = frame_index;
  assign wdata_valid = state == WRITE && !data_sent && !reset;
  assign wdata = accepted_frame;
  assign wdata_we = {(FRAME_DATA_WIDTH/8){1'b1}};
  assign rdata_ready = state == READ_RESPONSE && !reset;

`ifndef SYNTHESIS
  initial if (FRAME_DATA_WIDTH < 64 || FRAME_DATA_WIDTH % 64 != 0 ||
      FRAME_ADDRESS_WIDTH < 1 || FRAME_ADDRESS_WIDTH > 32 || FRAME_COUNT < 1 ||
      64'd1*FRAME_COUNT > (64'd1 << FRAME_ADDRESS_WIDTH))
    $fatal(1, "invalid DDR image loader geometry");
`endif

  always @(posedge clk) begin
    if (reset) begin
      state <= IDLE;
      accepted_frame <= 0;
      frame_index <= 0;
      command_sent <= 0;
      data_sent <= 0;
      loaded <= 0;
      fault <= 0;
    end else begin
      case (state)
        IDLE: if (input_valid && controller_ready) begin
          accepted_frame <= input_data;
          command_sent <= 0;
          data_sent <= 0;
          state <= WRITE;
        end
        WRITE: begin
          if (cmd_ready) command_sent <= 1;
          if (wdata_ready) data_sent <= 1;
          if ((command_sent || cmd_ready) && (data_sent || wdata_ready))
            state <= READ_COMMAND;
        end
        READ_COMMAND: if (cmd_ready) state <= READ_RESPONSE;
        READ_RESPONSE: if (rdata_valid) begin
          if (rdata !== accepted_frame) begin
            fault <= 1;
            state <= FAILED;
          end else if (64'd1*frame_index + 1 == FRAME_COUNT) begin
            loaded <= 1;
            state <= COMPLETE;
          end else begin
            frame_index <= frame_index + 1;
            state <= IDLE;
          end
        end
        COMPLETE: begin
          // Extra stream data remains backpressured until coordinated reset.
        end
        FAILED: begin
          loaded <= 0;
        end
        default: begin
          loaded <= 0;
          fault <= 1;
          state <= FAILED;
        end
      endcase
    end
  end
endmodule

// Compiler-owned source template, emitted before the hardware compiler.
// The native binding pass proves extension recovers every initializer word.
module $module #(
  parameter WIDTH = 64, SIZE = 16, IDX_SIZE = 4, INIT_ID = 0,
  parameter STORAGE_WIDTH = 8, SIGN_EXTEND = 1
)(
  input wire clk, reset,
  input wire [IDX_SIZE-1:0] addr0,
  input wire content_en, write_en,
  input wire [WIDTH-1:0] write_data,
  output reg [WIDTH-1:0] read_data,
  output reg done
);
  (* ram_style = "$style" *) reg [STORAGE_WIDTH-1:0] mem [0:SIZE-1];
  reg [IDX_SIZE-1:0] pending_addr;
  reg pending_read, pending_request;
  initial begin
    case (INIT_ID)
$initializers
      default: begin
`ifndef SYNTHESIS
        $$fatal(1, "unbound narrowing image");
`endif
      end
    endcase
  end
  always @(posedge clk) begin
    if (reset) begin
      pending_addr <= 0;
      pending_read <= 0;
      pending_request <= 0;
      read_data <= 0;
      done <= 0;
    end else begin
      if ($latency == 1) begin
        done <= content_en;
        if (content_en && !write_en) begin
          if (SIGN_EXTEND) read_data <= $$signed(mem[addr0]);
          else read_data <= mem[addr0];
        end
      end else begin
        done <= pending_request;
        if (pending_read) begin
          if (SIGN_EXTEND) read_data <= $$signed(mem[pending_addr]);
          else read_data <= mem[pending_addr];
        end
      end
      pending_request <= content_en;
      pending_read <= content_en && !write_en;
      if (content_en) pending_addr <= addr0;
`ifndef SYNTHESIS
      if (content_en && addr0 >= SIZE) $$fatal(1, "memory address out of range");
      if (content_en && write_en) $$fatal(1, "immutable memory write");
`endif
    end
  end
endmodule

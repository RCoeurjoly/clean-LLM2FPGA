// Source template owned by the compiler. Substitution supplies declarations,
// attributes and literal image cases before Calyx; no generated SV is edited.
module $module #(
  parameter WIDTH = 32, SIZE = 16, IDX_SIZE = 4,
  parameter INIT_ID = 0, WRITABLE = 1
)(
  input wire clk, reset,
  input wire [IDX_SIZE-1:0] addr0,
  input wire content_en, write_en,
  input wire [WIDTH-1:0] write_data,
  output reg [WIDTH-1:0] read_data,
  output reg done
);
  (* ram_style = "$style" *) reg [WIDTH-1:0] mem [0:SIZE-1];
  reg [IDX_SIZE-1:0] pending_addr;
  reg pending_read, pending_request;
  initial begin
    case (INIT_ID)
      0: begin end
$initializers
      default: begin
`ifndef SYNTHESIS
        $$fatal(1, "unbound initialization image");
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
        if (content_en && !write_en) read_data <= mem[addr0];
      end else begin
        done <= pending_request;
        if (pending_read) read_data <= mem[pending_addr];
      end
      pending_request <= content_en;
      pending_read <= content_en && !write_en;
      if (content_en) pending_addr <= addr0;
      if (content_en && write_en && WRITABLE) mem[addr0] <= write_data;
`ifndef SYNTHESIS
      if (content_en && addr0 >= SIZE) $$fatal(1, "memory address out of range");
      if (content_en && write_en && !WRITABLE) $$fatal(1, "immutable memory write");
`endif
    end
  end
endmodule

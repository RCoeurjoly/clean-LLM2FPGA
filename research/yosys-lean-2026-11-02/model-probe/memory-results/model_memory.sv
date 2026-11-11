module model_memory #(
  parameter WIDTH=32, SIZE=16, ADDR=4, IMAGE=""
)(
  input clk,reset,content_en,write_en,
  input [ADDR-1:0] addr0,
  input [WIDTH-1:0] write_data,
  output reg [WIDTH-1:0] read_data,
  output reg done
);
  (* ram_style="block" *) reg [WIDTH-1:0] mem [0:SIZE-1];
  initial if(IMAGE!="") $readmemh(IMAGE,mem);
  always @(posedge clk) begin
    done <= !reset && content_en;
    if(reset) read_data<=0;
    else if(content_en && !write_en) read_data<=mem[addr0];
    if(!reset && content_en && write_en) mem[addr0]<=write_data;
  end
endmodule

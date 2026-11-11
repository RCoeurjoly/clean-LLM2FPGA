module std_float_fma #(parameter WIDTH=32)(
  input clk,reset,go,
  input [WIDTH-1:0] in0,in1,in2,
  output reg [WIDTH-1:0] out,
  output reg done
);
  localparam E=WIDTH==32?8:11, S=WIDTH-E;
  wire [WIDTH:0] a,b,c,result;
  wire [WIDTH-1:0] ieee;
  fNToRecFN #(E,S) decode_a(in0,a), decode_b(in1,b), decode_c(in2,c);
  mulAddRecFN #(E,S) calculate(.control(1'b1),.op(2'b00),
    .a(a),.b(b),.c(c),.roundingMode(3'b000),.out(result),.exceptionFlags());
  recFNToFN #(E,S) encode(result,ieee);
  always @(posedge clk) begin
    if(reset) done<=0;
    else begin
      done<=go;
      if(go) out<=ieee;
    end
  end
endmodule
module std_float_fma_32(input clk,reset,go,input[31:0]in0,in1,in2,
  output[31:0]out,output done);
  std_float_fma #(32) fma(.*);
endmodule
module std_float_fma_64(input clk,reset,go,input[63:0]in0,in1,in2,
  output[63:0]out,output done);
  std_float_fma #(64) fma(.*);
endmodule

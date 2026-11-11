module std_float_cast #(parameter IN=32, OUT=64)(
  input clk, reset, go,
  input [IN-1:0] in,
  output reg [OUT-1:0] out,
  output reg done
);
  localparam IE=IN==32?8:11, IS=IN-IE, OE=OUT==32?8:11, OS=OUT-OE;
  wire [IN:0] rec_in;
  wire [OUT:0] rec_out;
  wire [OUT-1:0] converted;
  fNToRecFN #(IE,IS) decode(in,rec_in);
  recFNToRecFN #(IE,IS,OE,OS) convert(
    .control(1'b1),.in(rec_in),.roundingMode(3'b000),.out(rec_out),.exceptionFlags());
  recFNToFN #(OE,OS) encode(rec_out,converted);
  always @(posedge clk) begin
    if(reset) done<=0;
    else begin
      done<=go;
      if(go) out<=converted;
    end
  end
endmodule

module std_float_cast_32_64(input clk,reset,go,input[31:0]in,output[63:0]out,output done);
  std_float_cast #(32,64) cast(.*);
endmodule

module std_float_cast_64_32(input clk,reset,go,input[63:0]in,output[31:0]out,output done);
  std_float_cast #(64,32) cast(.*);
endmodule

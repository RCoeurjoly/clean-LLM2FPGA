module ScalarRepro(
  input clk, reset, content_en, write_en,
  input [3:0] addr,
  input [63:0] data,
  output [63:0] scalar_read,
  output [31:0] array_read,
  output scalar_done, array_done
);
  model_memory #(64,1,1,"/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/images/mem_186.hex") one(
    .clk(clk),.reset(reset),.content_en(content_en),.write_en(write_en),
    .addr0(1'b0),.write_data(data),.read_data(scalar_read),.done(scalar_done));
  model_memory #(32,16,4,"") many(
    .clk(clk),.reset(reset),.content_en(content_en),.write_en(write_en),
    .addr0(addr),.write_data(data[31:0]),.read_data(array_read),.done(array_done));
endmodule

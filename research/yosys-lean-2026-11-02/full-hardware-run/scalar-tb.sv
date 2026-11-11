module tb;
  reg clk=0,reset=1,content_en=0,write_en=0;
  reg [3:0] addr=3;
  reg [63:0] data=0;
  wire [63:0] scalar_read, mapped_scalar;
  wire [31:0] array_read, mapped_array;
  wire scalar_done,array_done,mapped_scalar_done,mapped_array_done;
  ScalarRepro original(.*);
  ScalarReproLowered mapped(.clk(clk),.reset(reset),.content_en(content_en),
    .write_en(write_en),.addr(addr),.data(data),.scalar_read(mapped_scalar),
    .array_read(mapped_array),.scalar_done(mapped_scalar_done),.array_done(mapped_array_done));
  always #5 clk=~clk;
  task check;
    begin
      if(scalar_read!==mapped_scalar || array_read!==mapped_array ||
         scalar_done!==mapped_scalar_done || array_done!==mapped_array_done)
        $fatal(1,"scalar memory normalization changed outputs");
    end
  endtask
  initial begin
    repeat(2) @(negedge clk);
    check(); reset=0; content_en=1; write_en=1; data=64'h0123456789abcdef;
    @(negedge clk); check(); write_en=0;
    @(negedge clk); check();
    if(scalar_read!==64'h0123456789abcdef || array_read!==32'h89abcdef)
      $fatal(1,"first write/read failed");
    addr=4;write_en=1;data=64'hfedcba9876543210;
    @(negedge clk);check();write_en=0;addr=3;
    @(negedge clk);check();
    if(scalar_read!==64'hfedcba9876543210 || array_read!==32'h89abcdef)
      $fatal(1,"scalar write or neighbouring array word changed");
    content_en=0;
    @(negedge clk);check();reset=1;content_en=1;write_en=1;data=0;
    @(negedge clk);check();reset=0;write_en=0;
    @(negedge clk);check();
    if(scalar_read!==64'hfedcba9876543210 || array_read!==32'h89abcdef)
      $fatal(1,"reset incorrectly wrote memory");
    $display("SCALAR_NORMALIZATION_TRACE_PASS");$finish;
  end
endmodule

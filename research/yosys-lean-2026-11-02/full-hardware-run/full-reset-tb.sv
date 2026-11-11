module full_reset_tb;
  reg clk=0, reset=1, go=0, tokens_write=0, logits_read=0;
  reg [4:0] tokens_address=0;
  reg [63:0] tokens_data=0;
  reg [20:0] logits_address=0;
  wire [31:0] logits_data;
  wire logits_valid, done;
  model_top dut(.*);
  task sample(input integer index, input bit clock, input bit rst);
    reset=rst;
    clk=clock;
    #1;
    $display("FULL_RESET %0d %h %b %b", index, logits_data, logits_valid, done);
  endtask
  initial begin
    sample(0,0,1);
    sample(1,1,1);
    sample(2,0,1);
    sample(3,1,1);
    sample(4,0,0);
    sample(5,1,0);
    sample(6,0,0);
    sample(7,1,0);
    $finish;
  end
endmodule

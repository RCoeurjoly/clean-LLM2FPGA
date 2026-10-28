// Map only unsigned word-aligned indexed selection; other cases retain default techmap.
(* techmap_celltype = "$shiftx" *)
module _00_closure_shiftx(A, B, Y);
  parameter A_SIGNED=0, B_SIGNED=0, A_WIDTH=1, B_WIDTH=1, Y_WIDTH=1;
  parameter [B_WIDTH-1:0] _TECHMAP_CONSTMSK_B_=0;
  parameter [B_WIDTH-1:0] _TECHMAP_CONSTVAL_B_=0;
  function integer known_low_zeros;
    input integer unused;
    integer k;
    begin
      known_low_zeros=0;
      for(k=0;k<B_WIDTH;k=k+1)
        if(k==known_low_zeros && _TECHMAP_CONSTMSK_B_[k] && !_TECHMAP_CONSTVAL_B_[k])
          known_low_zeros=known_low_zeros+1;
    end
  endfunction
  localparam LOW_RAW=known_low_zeros(0);
  localparam LOW=LOW_RAW < $clog2(A_WIDTH) ? LOW_RAW : $clog2(A_WIDTH);
  localparam STRIDE=2**LOW;
  localparam WORDS=(A_WIDTH+STRIDE-1)/STRIDE;
  localparam SELECT_BITS=(B_WIDTH-LOW)<$clog2(WORDS) ? B_WIDTH-LOW : $clog2(WORDS);
  localparam PACKED_WIDTH=STRIDE*(2**SELECT_BITS);
  wire _TECHMAP_FAIL_=B_SIGNED!=0 || A_WIDTH<1 || Y_WIDTH>STRIDE;
  input [A_WIDTH-1:0] A;
  input [B_WIDTH-1:0] B;
  output [Y_WIDTH-1:0] Y;
  wire [PACKED_WIDTH-1:0] packed_data;
  wire [STRIDE-1:0] selected_word;
  generate
    if(PACKED_WIDTH>=A_WIDTH) begin
      assign packed_data={{(PACKED_WIDTH-A_WIDTH){1'bx}},A};
    end else begin
      assign packed_data=A[PACKED_WIDTH-1:0];
    end
    if(SELECT_BITS==0) begin
      assign selected_word=packed_data;
    end else begin
      \$bmux #(.WIDTH(STRIDE),.S_WIDTH(SELECT_BITS)) select_word
        (.A(packed_data),.S(B[LOW+:SELECT_BITS]),.Y(selected_word));
    end
    if(B_WIDTH>LOW+SELECT_BITS) begin
      assign Y=(|B[B_WIDTH-1:LOW+SELECT_BITS]) ? {Y_WIDTH{1'bx}} : selected_word[Y_WIDTH-1:0];
    end else begin
      assign Y=selected_word[Y_WIDTH-1:0];
    end
  endgenerate
  wire [1023:0] _TECHMAP_DO_00_="CONSTMAP; opt_expr -mux_undef -mux_bool -fine; opt_clean";
endmodule

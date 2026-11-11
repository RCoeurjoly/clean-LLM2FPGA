`include "HardFloat_consts.vi"
module std_divSqrtFN #(
    parameter expWidth = 8, sigWidth = 24, numWidth = 32
)(
    input clk, reset, go,
    input [(`floatControlWidth-1):0] control,
    input sqrtOp,
    input [(expWidth+sigWidth-1):0] left, right,
    input [2:0] roundingMode,
    output logic [(expWidth+sigWidth-1):0] out,
    output logic [4:0] exceptionFlags,
    output logic done
);
    wire [(expWidth+sigWidth):0] a, b, result;
    wire [(expWidth+sigWidth-1):0] ieee;
    wire ready, valid;
    wire [4:0] flags;
    logic busy;
    wire start = go && !busy && !done;
    fNToRecFN #(expWidth, sigWidth) recode_a(.in(left), .out(a));
    fNToRecFN #(expWidth, sigWidth) recode_b(.in(right), .out(b));
    divSqrtRecFN_small #(expWidth, sigWidth, 0) calculate(
        .nReset(~reset), .clock(clk), .control(control),
        .inReady(ready), .inValid(start), .sqrtOp(sqrtOp),
        .a(a), .b(b), .roundingMode(roundingMode),
        .outValid(valid), .sqrtOpOut(), .out(result), .exceptionFlags(flags)
    );
    recFNToFN #(expWidth, sigWidth) decode(.in(result), .out(ieee));
    always_ff @(posedge clk) begin
        if (reset) begin
            busy <= 0;
            done <= 0;
            out <= 0;
            exceptionFlags <= 0;
        end else begin
            if (!go) done <= 0;
            if (start && ready) busy <= 1;
            if (valid) begin
                busy <= 0;
                done <= 1;
                out <= ieee;
                exceptionFlags <= flags;
            end
        end
    end
endmodule

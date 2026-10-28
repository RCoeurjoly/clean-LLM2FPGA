// Owned adapter for the native pipeline-v2 main interface. No model arithmetic
// or model weights live here. All three runtime memories retain 64-bit words.
// Host read index: tokens, then logits, then prompt, four 16-bit pieces/word.
module graph_onchip_runtime #(
    parameter integer LOGIT_WORDS = 50257,
    parameter integer LOGIT_ADDR = $clog2(LOGIT_WORDS) > 0 ? $clog2(LOGIT_WORDS) : 1
) (
    input wire clk, reset, start,
    input wire [15:0] prompt_0, prompt_1, prompt_2, prompt_3,
    input wire [17:0] host_index,
    output wire [15:0] host_piece,
    output reg done
);
  localparam IDLE=0, LOAD=1, DRAIN0=2, DRAIN1=3, RUN=4, WAIT_LOW=5;
  reg [2:0] state;
  reg [1:0] load_index;
  reg [15:0] prompt_snapshot [0:3];
  wire native_go = state == RUN;
  wire native_done;
  wire host_access = state == IDLE || state == WAIT_LOW;
  wire [15:0] host_word = host_index[17:2];
  wire token_selected = host_word < 16;
  wire logit_selected = host_word >= 16 && host_word < 16 + LOGIT_WORDS;
  wire prompt_selected = host_word >= 16 + LOGIT_WORDS && host_word < 20 + LOGIT_WORDS;
  wire [15:0] host_logit_addr = logit_selected ? host_word - 16 : 0;
  wire [15:0] host_prompt_addr = prompt_selected ? host_word - 16 - LOGIT_WORDS : 0;

  wire prompt_clk, prompt_reset, prompt_content_en, prompt_write_en, prompt_done;
  wire [1:0] prompt_addr0;
  wire [63:0] prompt_write_data, prompt_read_data;
  wire generated_tokens_clk, generated_tokens_reset, generated_tokens_content_en;
  wire generated_tokens_write_en, generated_tokens_done;
  wire [3:0] generated_tokens_addr0;
  wire [63:0] generated_tokens_write_data, generated_tokens_read_data;
  wire last_logits_clk, last_logits_reset, last_logits_content_en;
  wire last_logits_write_en, last_logits_done;
  wire [LOGIT_ADDR-1:0] last_logits_addr0;
  wire [63:0] last_logits_write_data, last_logits_read_data;

  // Snapshot once; later host CSR writes affect the next invocation. Preload
  // all four words, then drain the memory handshake before starting the core.
  always @(posedge clk) begin
    if (reset) begin
      state <= IDLE;
      load_index <= 0;
      done <= 0;
      prompt_snapshot[0] <= 0; prompt_snapshot[1] <= 0;
      prompt_snapshot[2] <= 0; prompt_snapshot[3] <= 0;
    end else begin
      done <= 0;
      case (state)
        IDLE: if (start) begin
          prompt_snapshot[0] <= prompt_0; prompt_snapshot[1] <= prompt_1;
          prompt_snapshot[2] <= prompt_2; prompt_snapshot[3] <= prompt_3;
          load_index <= 0;
          state <= LOAD;
        end
        LOAD: if (load_index == 3) state <= DRAIN0;
              else load_index <= load_index + 1'b1;
        DRAIN0: state <= DRAIN1;
        DRAIN1: state <= RUN;
        RUN: if (native_done) begin done <= 1; state <= WAIT_LOW; end
        WAIT_LOW: if (!start) state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end

  closure_lutram_memory #(.WIDTH(64), .SIZE(4), .IDX_SIZE(2), .INIT_ID(0), .WRITABLE(1)) prompt_memory (
      .clk(prompt_clk), .reset(reset | prompt_reset),
      .addr0(native_go ? prompt_addr0 : state == LOAD ? load_index : host_prompt_addr[1:0]),
      .content_en(native_go ? prompt_content_en : state == LOAD || (host_access && prompt_selected)),
      .write_en(native_go ? prompt_write_en : state == LOAD),
      .write_data(native_go ? prompt_write_data : {48'd0, prompt_snapshot[load_index]}),
      .read_data(prompt_read_data), .done(prompt_done));
  closure_lutram_memory #(.WIDTH(64), .SIZE(16), .IDX_SIZE(4), .INIT_ID(0), .WRITABLE(1)) token_memory (
      .clk(generated_tokens_clk), .reset(reset | generated_tokens_reset),
      .addr0(native_go ? generated_tokens_addr0 : host_word[3:0]),
      .content_en(native_go ? generated_tokens_content_en : host_access && token_selected),
      .write_en(native_go && generated_tokens_write_en), .write_data(generated_tokens_write_data),
      .read_data(generated_tokens_read_data), .done(generated_tokens_done));
  closure_bram_memory #(.WIDTH(64), .SIZE(LOGIT_WORDS), .IDX_SIZE(LOGIT_ADDR), .INIT_ID(0), .WRITABLE(1)) logit_memory (
      .clk(last_logits_clk), .reset(reset | last_logits_reset),
      .addr0(native_go ? last_logits_addr0 : host_logit_addr[LOGIT_ADDR-1:0]),
      .content_en(native_go ? last_logits_content_en : host_access && logit_selected),
      .write_en(native_go && last_logits_write_en), .write_data(last_logits_write_data),
      .read_data(last_logits_read_data), .done(last_logits_done));

  wire [63:0] host_value = !host_access ? 64'd0 :
      token_selected ? generated_tokens_read_data : logit_selected ? last_logits_read_data :
      prompt_selected ? prompt_read_data : 64'd0;
  assign host_piece = host_value >> (16 * host_index[1:0]);

  // The native linker renames the emitted main module before adding this
  // shell, so the completed board top can use the mapper's main entry name.
  graph_native_main core (
      .clk(clk), .reset(reset), .go(native_go), .done(native_done),
      .prompt_clk(prompt_clk), .prompt_reset(prompt_reset), .prompt_addr0(prompt_addr0),
      .prompt_content_en(prompt_content_en), .prompt_write_en(prompt_write_en),
      .prompt_write_data(prompt_write_data), .prompt_read_data(prompt_read_data), .prompt_done(prompt_done),
      .generated_tokens_clk(generated_tokens_clk), .generated_tokens_reset(generated_tokens_reset),
      .generated_tokens_addr0(generated_tokens_addr0), .generated_tokens_content_en(generated_tokens_content_en),
      .generated_tokens_write_en(generated_tokens_write_en), .generated_tokens_write_data(generated_tokens_write_data),
      .generated_tokens_read_data(generated_tokens_read_data), .generated_tokens_done(generated_tokens_done),
      .last_logits_clk(last_logits_clk), .last_logits_reset(last_logits_reset), .last_logits_addr0(last_logits_addr0),
      .last_logits_content_en(last_logits_content_en), .last_logits_write_en(last_logits_write_en),
      .last_logits_write_data(last_logits_write_data), .last_logits_read_data(last_logits_read_data), .last_logits_done(last_logits_done));
`ifndef SYNTHESIS
  initial if (LOGIT_WORDS < 1 || LOGIT_WORDS > 65516 ||
              LOGIT_ADDR != ($clog2(LOGIT_WORDS) > 0 ? $clog2(LOGIT_WORDS) : 1))
    $fatal(1, "unsupported on-chip runtime geometry");
`endif
endmodule

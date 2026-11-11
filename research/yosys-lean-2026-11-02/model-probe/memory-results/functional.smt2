(declare-datatype model_memory_Inputs
  (
    (model_memory_Inputs
      (model_memory_Inputs_clk (_ BitVec 1))
      (model_memory_Inputs_reset (_ BitVec 1))
      (model_memory_Inputs_content_en (_ BitVec 1))
      (model_memory_Inputs_write_en (_ BitVec 1))
      (model_memory_Inputs_addr0 (_ BitVec 4))
      (model_memory_Inputs_write_data (_ BitVec 32))
      (model_memory_Inputs_c2l_undefined_0 (_ BitVec 32))
      (model_memory_Inputs_c2l_undefined_1 (_ BitVec 4)))))
(declare-datatype model_memory_Outputs
  (
    (model_memory_Outputs
      (model_memory_Outputs_read_data (_ BitVec 32))
      (model_memory_Outputs_done (_ BitVec 1)))))
(declare-datatype model_memory_State
  (
    (model_memory_State
      (model_memory_State_$auto$clk2fflogic.cc_188_execute$39 (_ BitVec 1))
      (model_memory_State_$auto$clk2fflogic.cc_203_execute$43 (_ BitVec 32))
      (model_memory_State_$auto$clk2fflogic.cc_209_execute$47 (_ BitVec 32))
      (model_memory_State_$auto$clk2fflogic.cc_206_execute$45 (_ BitVec 4))
      (model_memory_State_mem (Array (_ BitVec 4) (_ BitVec 32)))
      (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$55
        (_ BitVec 1))
      (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$65
        (_ BitVec 1))
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$53 (_ BitVec 32))
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$51 (_ BitVec 32))
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$63 (_ BitVec 1))
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$61 (_ BitVec 1)))
  ))
(declare-datatypes ((Pair 2)) ((par (X Y) ((pair (first X) (second Y))))))
(define-fun
  model_memory
  ((inputs model_memory_Inputs) (state model_memory_State))
  (Pair model_memory_Outputs model_memory_State)
  (let ((clk (model_memory_Inputs_clk inputs))) ; (_ BitVec 1)
  (let ((reset_0 (model_memory_Inputs_reset inputs))) ; (_ BitVec 1)
  (let
    (($logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$11_Y
      (bvnot reset_0))) ; (_ BitVec 1)
  (let ((content_en (model_memory_Inputs_content_en inputs))) ; (_ BitVec 1)
  (let
    (($logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$12$_Y
      (bvand
        $logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$11_Y
        content_en))) ; (_ BitVec 1)
  (let ((write_en (model_memory_Inputs_write_en inputs))) ; (_ BitVec 1)
  (let
    (($logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$13$_Y
      (bvand
        $logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$12$_Y
        write_en))) ; (_ BitVec 1)
  (let
    (($0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_EN_31_0_$5
      (ite
        (=
          $logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$13$_Y
          #b1)
        #b11111111111111111111111111111111
        #b00000000000000000000000000000000))) ; (_ BitVec 32)
  (let ((c2l_undefined_0 (model_memory_Inputs_c2l_undefined_0 inputs))) ; (_ BitVec 32)
  (let ((write_data (model_memory_Inputs_write_data inputs))) ; (_ BitVec 32)
  (let
    (($0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_DATA_31_0_$4
      (ite
        (=
          $logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$13$_Y
          #b1)
        write_data
        c2l_undefined_0))) ; (_ BitVec 32)
  (let ((c2l_undefined_1 (model_memory_Inputs_c2l_undefined_1 inputs))) ; (_ BitVec 4)
  (let ((addr0 (model_memory_Inputs_addr0 inputs))) ; (_ BitVec 4)
  (let
    (($0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_ADDR_3_0_$3
      (ite
        (=
          $logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$13$_Y
          #b1)
        addr0
        c2l_undefined_1))) ; (_ BitVec 4)
  (let ((n16 (model_memory_State_mem state))) ; (Array (_ BitVec 4) (_ BitVec 32))
  (let
    (($auto$clk2fflogic.cc_206_execute$45$_Q
      (model_memory_State_$auto$clk2fflogic.cc_206_execute$45 state))) ; (_ BitVec 4)
  (let ((n18 (select n16 $auto$clk2fflogic.cc_206_execute$45$_Q))) ; (_ BitVec 32)
  (let
    (($auto$clk2fflogic.cc_203_execute$43$_Q
      (model_memory_State_$auto$clk2fflogic.cc_203_execute$43 state))) ; (_ BitVec 32)
  (let
    (($auto$clk2fflogic.cc_188_execute$39$_Q
      (model_memory_State_$auto$clk2fflogic.cc_188_execute$39 state))) ; (_ BitVec 1)
  (let ((n21 (concat clk $auto$clk2fflogic.cc_188_execute$39$_Q))) ; (_ BitVec 2)
  (let (($auto$clk2fflogic.cc_200_execute$40$_Y (ite (= n21 #b10) #b1 #b0))) ; (_ BitVec 1)
  (let
    (($auto$clk2fflogic.cc_212_execute$48$_Y
      (ite
        (= $auto$clk2fflogic.cc_200_execute$40$_Y #b1)
        $auto$clk2fflogic.cc_203_execute$43$_Q
        #b00000000000000000000000000000000))) ; (_ BitVec 32)
  (let ((n25 (bvnot $auto$clk2fflogic.cc_212_execute$48$_Y))) ; (_ BitVec 32)
  (let ((n26 (bvand n18 n25))) ; (_ BitVec 32)
  (let
    (($auto$clk2fflogic.cc_209_execute$47$_Q
      (model_memory_State_$auto$clk2fflogic.cc_209_execute$47 state))) ; (_ BitVec 32)
  (let
    ((n28
      (bvand
        $auto$clk2fflogic.cc_209_execute$47$_Q
        $auto$clk2fflogic.cc_212_execute$48$_Y))) ; (_ BitVec 32)
  (let ((n29 (bvor n26 n28))) ; (_ BitVec 32)
  (let ((n30 (store n16 $auto$clk2fflogic.cc_206_execute$45$_Q n29))) ; (Array (_ BitVec 4) (_ BitVec 32))
  (let
    (($auto$clk2fflogic.cc_108_sample_data$51$_Q
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$51 state))) ; (_ BitVec 32)
  (let
    (($auto$clk2fflogic.cc_108_sample_data$53$_Q
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$53 state))) ; (_ BitVec 32)
  (let
    (($auto$clk2fflogic.cc_92_sample_control_edge$55$_Q
      (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$55 state))) ; (_ BitVec 1)
  (let ((n34 (concat $auto$clk2fflogic.cc_92_sample_control_edge$55$_Q clk))) ; (_ BitVec 2)
  (let (($auto$rtlil.cc_3393_Eqx$57 (ite (= n34 #b01) #b1 #b0))) ; (_ BitVec 1)
  (let
    ((read_data
      (ite
        (= $auto$rtlil.cc_3393_Eqx$57 #b1)
        $auto$clk2fflogic.cc_108_sample_data$53$_Q
        $auto$clk2fflogic.cc_108_sample_data$51$_Q))) ; (_ BitVec 32)
  (let ((mem$_RD_DATA (select n30 addr0))) ; (_ BitVec 32)
  (let
    (($logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_15$8_Y
      (bvnot write_en))) ; (_ BitVec 1)
  (let
    (($logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_15$9$_Y
      (bvand
        content_en
        $logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_15$8_Y))) ; (_ BitVec 1)
  (let
    (($procmux$27$_Y
      (ite
        (=
          $logic_and$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_15$9$_Y
          #b1)
        mem$_RD_DATA
        read_data))) ; (_ BitVec 32)
  (let
    (($0_read_data_31_0_
      (ite (= reset_0 #b1) #b00000000000000000000000000000000 $procmux$27$_Y))) ; (_ BitVec 32)
  (let
    (($logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_13$6_Y
      (bvnot reset_0))) ; (_ BitVec 1)
  (let
    (($0_done_0_0_
      (bvand
        $logic_not$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_13$6_Y
        content_en))) ; (_ BitVec 1)
  (let
    (($auto$clk2fflogic.cc_108_sample_data$61$_Q
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$61 state))) ; (_ BitVec 1)
  (let
    (($auto$clk2fflogic.cc_108_sample_data$63$_Q
      (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$63 state))) ; (_ BitVec 1)
  (let
    (($auto$clk2fflogic.cc_92_sample_control_edge$65$_Q
      (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$65 state))) ; (_ BitVec 1)
  (let ((n48 (concat $auto$clk2fflogic.cc_92_sample_control_edge$65$_Q clk))) ; (_ BitVec 2)
  (let (($auto$rtlil.cc_3393_Eqx$67 (ite (= n48 #b01) #b1 #b0))) ; (_ BitVec 1)
  (let
    ((done
      (ite
        (= $auto$rtlil.cc_3393_Eqx$67 #b1)
        $auto$clk2fflogic.cc_108_sample_data$63$_Q
        $auto$clk2fflogic.cc_108_sample_data$61$_Q))) ; (_ BitVec 1)
  (pair
    (model_memory_Outputs
      read_data ; read_data
      done ; done
    )
    (model_memory_State
      clk ; $auto$clk2fflogic.cc:188:execute$39
      $0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_EN_31_0_$5 ; $auto$clk2fflogic.cc:203:execute$43
      $0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_DATA_31_0_$4 ; $auto$clk2fflogic.cc:209:execute$47
      $0$memwr$_mem$/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/memory.sv_16$1_ADDR_3_0_$3 ; $auto$clk2fflogic.cc:206:execute$45
      n30 ; mem
      clk ; $auto$clk2fflogic.cc:92:sample_control_edge$55
      clk ; $auto$clk2fflogic.cc:92:sample_control_edge$65
      $0_read_data_31_0_ ; $auto$clk2fflogic.cc:108:sample_data$53
      read_data ; $auto$clk2fflogic.cc:108:sample_data$51
      $0_done_0_0_ ; $auto$clk2fflogic.cc:108:sample_data$63
      done ; $auto$clk2fflogic.cc:108:sample_data$61
    ))))))))))))))))))))))))))))))))))))))))))))))))))
(declare-const model_memory-initial model_memory_State)
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_188_execute$39
      model_memory-initial)
    #b1))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_203_execute$43
      model_memory-initial)
    #b00000000000000000000000000000000))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_209_execute$47
      model_memory-initial)
    #b00000000000000000000000000000000))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_206_execute$45
      model_memory-initial)
    #b0000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0000)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0001)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0010)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0011)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0100)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0101)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0110)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b0111)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1000)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1001)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1010)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1011)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1100)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1101)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1110)
    #b00000000000000000000000000000000))
(assert
  (=
    (select (model_memory_State_mem model_memory-initial) #b1111)
    #b00000000000000000000000000000000))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$55
      model_memory-initial)
    #b1))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_92_sample_control_edge$65
      model_memory-initial)
    #b1))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$53
      model_memory-initial)
    #b00000000000000000000000000000000))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$51
      model_memory-initial)
    #b00000000000000000000000000000000))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$63
      model_memory-initial)
    #b0))
(assert
  (=
    (model_memory_State_$auto$clk2fflogic.cc_108_sample_data$61
      model_memory-initial)
    #b0))

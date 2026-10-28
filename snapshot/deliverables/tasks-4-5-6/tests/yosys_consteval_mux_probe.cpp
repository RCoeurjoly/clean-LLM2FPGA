#include "kernel/yosys.h"
#include "kernel/consteval.h"
#include <cstdio>
USING_YOSYS_NAMESPACE
PRIVATE_NAMESPACE_BEGIN
struct ConstEvalProbePass : Pass {
 ConstEvalProbePass():Pass("consteval_probe","compare mux constant evaluation"){}
 void execute(std::vector<std::string> args, RTLIL::Design *design) override {
  extra_args(args,1,design);
  RTLIL::State states[]={RTLIL::State::S0,RTLIL::State::S1,RTLIL::State::Sx,RTLIL::State::Sz};
  int case_id=0;
  for(int kind=0;kind<4;kind++) for(int w : {1,4,64}) {
   if(kind>=2 && w!=1)continue;
   int selectors=kind==1?3:1,patterns=kind==1?64:4;
   for(int pattern=0;pattern<patterns;pattern++)for(int a_mode=0;a_mode<5;a_mode++) {
    auto mod=design->addModule(RTLIL::IdString("\\probe"+std::to_string(case_id)));
    auto u=mod->addWire("\\u",w),y=mod->addWire("\\y",w);
    auto cell=mod->addCell("\\mux",kind==0?ID($mux):kind==1?ID($pmux):kind==2?ID($_MUX_):ID($_NMUX_));
    RTLIL::SigSpec a=a_mode==4?RTLIL::SigSpec(u):RTLIL::SigSpec(RTLIL::Const(states[a_mode],w));
    RTLIL::SigSpec b;
    for(int lane=0;lane<selectors;lane++)b.append(lane==1?RTLIL::SigSpec(u):RTLIL::SigSpec(RTLIL::Const(states[(a_mode+lane+1)%4],w)));
    std::vector<RTLIL::State> selection;
    for(int lane=0;lane<selectors;lane++)selection.push_back(states[(pattern>>(lane*2))&3]);
    cell->setPort(ID::A,a);cell->setPort(ID::B,b);cell->setPort(ID::S,pattern==patterns-1 && a_mode==4 ? RTLIL::SigSpec(mod->addWire("\\sel",selectors)) : RTLIL::SigSpec(RTLIL::Const(selection)));cell->setPort(ID::Y,y);
    if(kind<2)cell->parameters[ID::WIDTH]=w;
    if(kind==1)cell->parameters[ID::S_WIDTH]=selectors;
    ConstEval ce(mod);RTLIL::SigSpec result(y),undef;bool known=ce.eval(result,undef);
    std::printf("PROBE %d known=%d result=%s undef=%s\n",case_id,known,log_signal(result).c_str(),log_signal(undef).c_str());
    case_id++;
   }
  }
  std::printf("PROBE_CASES %d\n",case_id);
 }
} ConstEvalProbePass;
PRIVATE_NAMESPACE_END

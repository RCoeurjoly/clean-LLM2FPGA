// Verification-only observer for the generated public-interface testbench.
// The event loop matches Verilator's --main driver. Reads only testbench
// counters/write bitmaps; never changes model state or supplies model answers.
// A wall-clock stop is an unfinished diagnostic (124), not numerical acceptance.
#include "verilated.h"
#include "Vtb.h"
#include "Vtb___024root.h"
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>

static bool option(int argc, char** argv, const char* prefix,
                   unsigned long long& value) {
    for (int i=1; i<argc; ++i) {
        if (std::strncmp(argv[i],prefix,std::strlen(prefix))) continue;
        const char* text=argv[i]+std::strlen(prefix);
        if (!*text || *text<'0' || *text>'9') return false;
        char* end=nullptr;
        const auto parsed=std::strtoull(text,&end,10);
        if (*end || !parsed || parsed>7200000) return false;
        value=parsed;
    }
    return true;
}

int main(int argc, char** argv) {
    unsigned long long seconds=60, every=64, capture_steps=0, stop_steps=16;
    if (!option(argc,argv,"+OBSERVER_SECONDS=",seconds) ||
        !option(argc,argv,"+OBSERVER_EVERY_EVALS=",every) ||
        !option(argc,argv,"+OBSERVER_CAPTURE_STEPS=",capture_steps) ||
        !option(argc,argv,"+OBSERVER_STOP_AFTER_STEPS=",stop_steps) ||
        capture_steps>1 || stop_steps>16 || (stop_steps<16 && !capture_steps)) {
        std::fprintf(stderr,"OBSERVER invalid observation options\n");return 2;
    }
    Verilated::debug(0);
    const std::unique_ptr<VerilatedContext> context{new VerilatedContext};
    context->threads(1);context->commandArgs(argc,argv);
    const std::unique_ptr<Vtb> model{new Vtb{context.get(),""}};
    const auto start=std::chrono::steady_clock::now();
    unsigned long long evals=0;
    auto report=[&]() {
        const auto milliseconds=std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now()-start).count();
        const auto& written=model->rootp->tb__DOT__last_logits_model__DOT__written;
        unsigned count=0;
        for (size_t i=0;i<written.size();++i) count+=__builtin_popcount(written[i]);
        std::fprintf(stderr,"PROGRESS wall_ms=%lld evals=%llu cycles=%llu time=%llu tokens_mask=%04x logits_written=%u",
            static_cast<long long>(milliseconds),evals,
            static_cast<unsigned long long>(model->rootp->tb__DOT__cycles),
            static_cast<unsigned long long>(context->time()),
            static_cast<unsigned>(model->rootp->tb__DOT__generated_tokens_model__DOT__written),count);
#ifdef LLM2FPGA_FULL_GENERATION_FSM
        std::fprintf(stderr," main_fsm=%u ctx4_fsm=%u",
            static_cast<unsigned>(model->rootp->tb__DOT__dut__DOT__main_1_instance__DOT__fsm_out),
            static_cast<unsigned>(model->rootp->tb__DOT__dut__DOT__main_1_instance__DOT__generation_ctx_4_main_instance__DOT__fsm_out));
#endif
        std::fprintf(stderr,"\n");std::fflush(stderr);
    };
    auto last_report=start;
    unsigned captured=0;
    unsigned long long captured_tokens[16]={};
    bool expired=false;
    while (!context->gotFinish()) {
        model->eval();++evals;
        if (capture_steps) {
            const auto* state=model->rootp;
            const unsigned mask=state->tb__DOT__generated_tokens_model__DOT__written;
            const unsigned before=(1u<<captured)-1;
            const unsigned next=captured<16?before|(1u<<captured):before;
            if (mask!=before && mask!=next) {
                std::fprintf(stderr,"OBSERVER out-of-order token visibility\n");return 2;
            }
            for (unsigned i=0;i<captured;++i) {
                if (state->tb__DOT__generated_tokens_model__DOT__memory[i]!=captured_tokens[i]) {
                    std::fprintf(stderr,"OBSERVER completed token changed\n");return 2;
                }
            }
            if (mask!=before) {
                const auto& logits=state->tb__DOT__last_logits_model__DOT__memory;
                const auto& written=state->tb__DOT__last_logits_model__DOT__written;
                unsigned count=0;
                for (size_t i=0;i<written.size();++i)count+=__builtin_popcount(written[i]);
                if (count!=logits.size()) {
                    std::fprintf(stderr,"OBSERVER token before complete logits\n");return 2;
                }
                char name[64];std::snprintf(name,sizeof(name),"step-%02u.hex",captured);
                FILE* file=std::fopen(name,"wx");
                if (!file) {std::perror("OBSERVER snapshot open");return 2;}
                bool valid=true;
                for (size_t i=0;i<logits.size();++i)
                    if (std::fprintf(file,"%016llx\n",static_cast<unsigned long long>(logits[i]))<0)valid=false;
                if (std::fclose(file))valid=false;
                if (!valid) {std::fprintf(stderr,"OBSERVER snapshot write failed\n");return 2;}
                captured_tokens[captured]=state->tb__DOT__generated_tokens_model__DOT__memory[captured];
                std::printf("STEP %u %016llx %zu %s\n",captured,captured_tokens[captured],logits.size(),name);
                std::fflush(stdout);
                ++captured;
                if (captured==stop_steps && stop_steps<16) {
                    report();
                    std::fprintf(stderr,"OBSERVER diagnostic step limit; generation unfinished\n");return 125;
                }
            }
        }
        if (evals%every==0) {
            const auto now=std::chrono::steady_clock::now();
            if (every==1 || now-last_report>=std::chrono::seconds(10)) {
                report();last_report=now;
            }
            if (now-start>=std::chrono::seconds(seconds)) {expired=true;break;}
        }
        if (!model->eventsPending()) break;
        context->time(model->nextTimeSlot());
    }
    report();
    if (expired || !context->gotFinish()) {
        std::fprintf(stderr,"OBSERVER unfinished; no numerical acceptance\n");return expired?124:2;
    }
    model->final();context->statsPrintSummary();return 0;
}

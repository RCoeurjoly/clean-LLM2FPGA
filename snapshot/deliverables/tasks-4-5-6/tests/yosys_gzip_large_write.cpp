#include "kernel/gzip.cc"
int main(int argc, char **argv) {
 Yosys::gzip_ostream out;
 if (!out.open(argv[1])) return 2;
 const std::string block(1024, 'x');
 for (int i=0; i<2097153; ++i) out << block;
 out.flush();
 return !out.good();
}

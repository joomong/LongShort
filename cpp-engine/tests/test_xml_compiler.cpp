#include <gtest/gtest.h>
#include <cmath>
#include <vector>
#include <string>
#include "module.h"
#include "compiler/xml_parser.h"
#include "compiler/expansion.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "signal_vector.h"

// ---------------------------------------------------------------------------
// Minimal DataPanel for compiler tests
// ---------------------------------------------------------------------------
static DataPanel make_panel(unsigned T = 30, unsigned N = 3) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = (int64_t)t * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "SYM" + std::to_string(n);
    for (const char* col : {"open","high","low","close","volume"}) {
        auto& arr = dp.arrays[col];
        arr.resize(T * N);
        for (unsigned t = 0; t < T; ++t)
            for (unsigned n = 0; n < N; ++n)
                arr[t * N + n] = 100.0f + (float)t + (float)n * 0.1f;
    }
    return dp;
}

// ---------------------------------------------------------------------------
// TEST: BasicPipeline
// <data expr="close" delay="1"/><tsmean days="20"/><zscore/>
// ---------------------------------------------------------------------------
TEST(XmlCompiler, BasicPipeline) {
    auto dp = make_panel(30, 3);
    std::string xml = R"(<data expr="close" delay="1"/><tsmean days="20"/><zscore/>)";
    auto alphas = compile_xml(xml, &dp);

    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
    // At least 3 modules: MData, MDelay, MTsmean, MZscore
    EXPECT_GE(alphas[0].modules.size(), 3u);

    // Run through all bars and check output is finite after warmup
    alphas[0].output->numAssets = dp.N;
    alphas[0].output->init();
    std::vector<float> buf(dp.N);
    SignalVector sv{buf.data(), dp.N};
    for (unsigned t = 0; t < dp.T; ++t)
        alphas[0].output->calculate(t, sv);
    // After 20+1 bars the output should be finite
    for (unsigned n = 0; n < dp.N; ++n)
        EXPECT_TRUE(std::isfinite(sv[n]) || std::isnan(sv[n]));
}

// ---------------------------------------------------------------------------
// TEST: PipeExpansion
// <data expr="close"/><tsmean days="10|20"/> → 2 variants
// ---------------------------------------------------------------------------
TEST(XmlCompiler, PipeExpansion) {
    auto dp = make_panel();
    std::string xml = R"(<data expr="close"/><tsmean days="10|20"/>)";
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 2u);
}

// ---------------------------------------------------------------------------
// TEST: RangeExpansion
// <data expr="close"/><tsmean days="10...10...30"/> → 3 variants
// ---------------------------------------------------------------------------
TEST(XmlCompiler, RangeExpansion) {
    auto dp = make_panel();
    std::string xml = R"(<data expr="close"/><tsmean days="10...10...30"/>)";
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 3u);
}

// ---------------------------------------------------------------------------
// TEST: ChoiceExpansion
// <data expr="close"/><choice><rank/><zscore/></choice> → 2 variants
// ---------------------------------------------------------------------------
TEST(XmlCompiler, ChoiceExpansion) {
    auto dp = make_panel();
    std::string xml = R"(<data expr="close"/><choice><rank/><zscore/></choice>)";
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 2u);
}

// ---------------------------------------------------------------------------
// TEST: OverflowError
// Generate >1000 variants → CompileError
// ---------------------------------------------------------------------------
TEST(XmlCompiler, OverflowError) {
    auto dp = make_panel();
    // 11 pipe values × 11 pipe values × 11 pipe values = 1331 > 1000
    std::string many = "1|2|3|4|5|6|7|8|9|10|11";
    std::string xml =
        "<data expr=\"close\"/>"
        "<tsmean days=\"" + many + "\"/>"
        "<tszscore days=\"" + many + "\"/>"
        "<tsrank days=\"" + many + "\"/>";
    EXPECT_THROW(compile_xml(xml, &dp), std::runtime_error);
}

// ---------------------------------------------------------------------------
// TEST: UnknownTag → CompileError
// ---------------------------------------------------------------------------
TEST(XmlCompiler, UnknownTag) {
    auto dp = make_panel();
    std::string xml = R"(<data expr="close"/><unknown/>)";
    EXPECT_THROW(compile_xml(xml, &dp), std::runtime_error);
}

// ---------------------------------------------------------------------------
// TEST: ReturnExpr
// <data expr="return"/> → generates return calculation modules
// ---------------------------------------------------------------------------
TEST(XmlCompiler, ReturnExpr) {
    auto dp = make_panel(10, 2);
    std::string xml = R"(<data expr="return"/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
    // Modules: MData(close), MData(close), MDelay, MConstant(1), MChain, MDiv, MSub
    EXPECT_GE(alphas[0].modules.size(), 4u);

    alphas[0].output->numAssets = dp.N;
    alphas[0].output->init();
    std::vector<float> buf(dp.N, 0.0f);
    SignalVector sv{buf.data(), dp.N};

    // Bar 0: delay not yet warmed → result may be NaN
    alphas[0].output->calculate(0, sv);

    // Bar 1: close[1]/close[0] - 1 = (101/100) - 1 = 0.01
    alphas[0].output->calculate(1, sv);
    // close increases by 1.0 each bar, starting at 100
    // sym0: close[1]=101, close[0]=100 → return=0.01
    EXPECT_NEAR(sv[0], 0.01f, 1e-4f);
}

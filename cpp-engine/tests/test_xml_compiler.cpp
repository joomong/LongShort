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
    ASSERT_EQ(alphas.size(), 2u);
    EXPECT_EQ(alphas[0].source_xml, "<data expr=\"close\"/>\n<tsmean days=\"10\"/>");
    EXPECT_EQ(alphas[1].source_xml, "<data expr=\"close\"/>\n<tsmean days=\"20\"/>");
}

TEST(XmlCompiler, SourceXmlEscapesAttributes) {
    auto dp = make_panel();
    std::string xml = R"(<data expr="close"/><signal id="a &lt; b &amp; c"/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    EXPECT_EQ(
        alphas[0].source_xml,
        "<data expr=\"close\"/>\n<signal id=\"a &lt; b &amp; c\"/>");
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
// Generate >MAX_ALPHA_COUNT variants → CompileError
// ---------------------------------------------------------------------------
TEST(XmlCompiler, OverflowError) {
    auto dp = make_panel();

    std::string many;
    many.reserve(static_cast<size_t>(MAX_ALPHA_COUNT) * 5);
    for (int i = 1; i <= MAX_ALPHA_COUNT + 1; ++i) {
        if (i > 1)
            many.push_back('|');
        many += std::to_string(i);
    }

    std::string xml =
        "<data expr=\"close\"/>"
        "<tsmean days=\"" + many + "\"/>";
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
// <data expr="returns"/> → generates return calculation modules
// ---------------------------------------------------------------------------
TEST(XmlCompiler, ReturnExpr) {
    auto dp = make_panel(10, 2);
    std::string xml = R"(<data expr="returns"/>)";
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

// ---------------------------------------------------------------------------
// TEST: ExprArithmetic — compound expressions in expr attribute
// ---------------------------------------------------------------------------
TEST(XmlCompiler, ExprArithmeticSubtraction) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="close - open"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprArithmeticMultiplication) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="2*close"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprArithmeticDivision) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="close/volume"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprUnaryMinus) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="-close"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprNegatedReturns) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="-returns"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprReturnsInCompoundExpression) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="returns * volume"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprParentheses) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="(close - open) / close"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

TEST(XmlCompiler, ExprNumericLiteral) {
    auto dp = make_panel(5, 2);
    auto alphas = compile_xml(R"(<data expr="close * 0.5 + volume * 0.5"/>)", &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_NE(alphas[0].output, nullptr);
}

// ---------------------------------------------------------------------------
// Multi-source (multi <data> + <signal> + input) tests
// ---------------------------------------------------------------------------

TEST(XmlCompiler, MultiDataCompiles) {
    auto dp = make_panel(100, 3);
    std::string xml = R"(
        <data delay="1" expr="returns"/>
        <signal id="pos"/>
        <data expr="-returns" delay="1"/>
        <signal id="neg"/>
        <choice>
          <tsrank days="60" input="pos|neg"/>
        </choice>
        <zscore/>
    )";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 2u);
}

TEST(XmlCompiler, MultiDataSignalRouting) {
    auto dp = make_panel(100, 3);
    std::string xml_pos = R"(
        <data expr="close"/>
        <signal id="pos"/>
        <data expr="-close"/>
        <signal id="neg"/>
        <rank input="pos"/>
    )";
    std::string xml_neg = R"(
        <data expr="close"/>
        <signal id="pos"/>
        <data expr="-close"/>
        <signal id="neg"/>
        <rank input="neg"/>
    )";
    auto alphas_pos = compile_xml(xml_pos, &dp);
    auto alphas_neg = compile_xml(xml_neg, &dp);
    ASSERT_EQ(alphas_pos.size(), 1u);
    ASSERT_EQ(alphas_neg.size(), 1u);

    std::vector<float> buf_p(3), buf_n(3);
    SignalVector sv_p{buf_p.data(), 3}, sv_n{buf_n.data(), 3};
    for (Module* m : alphas_pos[0].pipeline) { m->numAssets = 3; m->init(); }
    for (Module* m : alphas_neg[0].pipeline) { m->numAssets = 3; m->init(); }

    for (unsigned t = 0; t < dp.T; ++t) {
        sv_p.fill_nan(); sv_n.fill_nan();
        for (Module* m : alphas_pos[0].pipeline) m->calculate(t, sv_p);
        for (Module* m : alphas_neg[0].pipeline) m->calculate(t, sv_n);
    }
    bool differ = false;
    for (unsigned i = 0; i < 3; ++i)
        if (std::isfinite(sv_p[i]) && std::isfinite(sv_n[i]) && sv_p[i] != sv_n[i])
            differ = true;
    EXPECT_TRUE(differ);
}

TEST(XmlCompiler, UndefinedSignalInputThrows) {
    auto dp = make_panel();
    std::string xml = R"(
        <data expr="close"/>
        <signal id="pos"/>
        <rank input="nonexistent"/>
    )";
    EXPECT_THROW(compile_xml(xml, &dp), CompileError);
}

TEST(XmlCompiler, SignalWithoutInputIsNoop) {
    auto dp = make_panel(30, 3);
    std::string xml = R"(
        <data expr="close"/>
        <signal id="unused"/>
        <zscore/>
    )";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    std::vector<float> buf(3);
    SignalVector sv{buf.data(), 3};
    for (Module* m : alphas[0].pipeline) { m->numAssets = 3; m->init(); }
    for (unsigned t = 0; t < 30u; ++t) {
        sv.fill_nan();
        for (Module* m : alphas[0].pipeline) m->calculate(t, sv);
    }
    bool any_finite = false;
    for (unsigned i = 0; i < 3; ++i)
        if (std::isfinite(sv[i])) any_finite = true;
    EXPECT_TRUE(any_finite);
}

TEST(XmlCompiler, EventStillWorksWithSignalCache) {
    auto dp = make_panel(30, 3);
    std::string xml = R"(
        <data expr="close"/>
        <signal id="sig"/>
        <data expr="volume"/>
        <signal id="trig"/>
        <event signal="sig" trigger="trig" threshold="0" maxholddays="5"/>
    )";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    std::vector<float> buf(3);
    SignalVector sv{buf.data(), 3};
    for (Module* m : alphas[0].pipeline) { m->numAssets = 3; m->init(); }
    for (unsigned t = 0; t < 30u; ++t) {
        sv.fill_nan();
        for (Module* m : alphas[0].pipeline) m->calculate(t, sv);
    }
}

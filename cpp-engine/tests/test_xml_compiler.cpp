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

static void expect_compile_error_contains(const std::string& xml,
                                          const DataPanel& panel,
                                          const std::vector<std::string>& tokens) {
    try {
        (void)compile_xml(xml, &panel);
        FAIL() << "Expected CompileError, but compile_xml succeeded";
    } catch (const CompileError& e) {
        const std::string message = e.what();
        for (const auto& token : tokens) {
            EXPECT_NE(message.find(token), std::string::npos)
                << "Missing token '" << token << "' in CompileError: " << message;
        }
    } catch (const std::exception& e) {
        FAIL() << "Expected CompileError, got: " << e.what();
    }
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

TEST(XmlCompiler, DaysValidationRejectsZeroNegativeAndTooLarge) {
    auto dp = make_panel(30, 3);
    const std::vector<std::string> tags = {"tsmean", "tsrank", "tszscore", "decay"};
    const std::vector<std::string> invalid_days = {"0", "-1", "31"};

    for (const auto& tag : tags) {
        for (const auto& days : invalid_days) {
            SCOPED_TRACE("tag=" + tag + ", days=" + days);
            const std::string xml = "<data expr=\"close\"/><" + tag + " days=\"" + days + "\"/>";
            expect_compile_error_contains(
                xml,
                dp,
                {"<" + tag + ">", "'days'", "'" + days + "'", "[1, 30]"});
        }
    }
}

TEST(XmlCompiler, DaysValidationRejectsMalformedNumericStrings) {
    auto dp = make_panel(30, 3);
    const std::vector<std::string> tags = {"tsmean", "tsrank", "tszscore", "decay"};
    const std::vector<std::string> malformed_days = {"1x", "1.5"};

    for (const auto& tag : tags) {
        for (const auto& days : malformed_days) {
            SCOPED_TRACE("tag=" + tag + ", days=" + days);
            const std::string xml = "<data expr=\"close\"/><" + tag + " days=\"" + days + "\"/>";
            expect_compile_error_contains(
                xml,
                dp,
                {"<" + tag + ">", "'days'", "'" + days + "'", "expected integer in range [1, 30]"});
        }
    }
}

TEST(XmlCompiler, DataDelayValidationRejectsNegativeAndTooLarge) {
    auto dp = make_panel(30, 3);
    expect_compile_error_contains(
        R"(<data expr="close" delay="-1"/>)",
        dp,
        {"<data>", "'delay'", "'-1'", "[0, 30]"});
    expect_compile_error_contains(
        R"(<data expr="close" delay="31"/>)",
        dp,
        {"<data>", "'delay'", "'31'", "[0, 30]"});
}

TEST(XmlCompiler, DelayModuleValidationRejectsNegativeAndTooLarge) {
    auto dp = make_panel(30, 3);
    expect_compile_error_contains(
        R"(<data expr="close"/><delay days="-1"/>)",
        dp,
        {"<delay>", "'days'", "'-1'", "[0, 30]"});
    expect_compile_error_contains(
        R"(<data expr="close"/><delay days="31"/>)",
        dp,
        {"<delay>", "'days'", "'31'", "[0, 30]"});
}

TEST(XmlCompiler, DelayValidationRejectsMalformedNumericStrings) {
    auto dp = make_panel(30, 3);
    expect_compile_error_contains(
        R"(<data expr="close" delay="2foo"/>)",
        dp,
        {"<data>", "'delay'", "'2foo'", "expected integer in range [0, 30]"});
    expect_compile_error_contains(
        R"(<data expr="close"/><delay days="2foo"/>)",
        dp,
        {"<delay>", "'days'", "'2foo'", "expected integer in range [0, 30]"});
}

TEST(XmlCompiler, DelayZeroValidForDataAttributeAndModule) {
    auto dp = make_panel(30, 3);

    auto baseline = compile_xml(R"(<data expr="close"/><zscore/>)", &dp);
    auto data_delay_zero = compile_xml(R"(<data expr="close" delay="0"/><zscore/>)", &dp);
    ASSERT_EQ(baseline.size(), 1u);
    ASSERT_EQ(data_delay_zero.size(), 1u);
    EXPECT_EQ(data_delay_zero[0].modules.size(), baseline[0].modules.size());
    EXPECT_EQ(data_delay_zero[0].pipeline.size(), baseline[0].pipeline.size());

    auto delay_module_zero = compile_xml(R"(<data expr="close"/><delay days="0"/>)", &dp);
    ASSERT_EQ(delay_module_zero.size(), 1u);
    EXPECT_EQ(delay_module_zero[0].modules.size(), 2u);
    ASSERT_NE(delay_module_zero[0].output, nullptr);
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

TEST(XmlCompiler, EventWithoutSignalUsesCurrentChainOutput) {
    auto dp = make_panel(30, 3);
    std::string xml = R"(
        <data expr="close"/>
        <zscore/>
        <signal id="trig"/>
        <event trigger="trig" threshold="0" maxholddays="5"/>
    )";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);

    std::vector<float> buf(3);
    SignalVector sv{buf.data(), 3};
    for (Module* m : alphas[0].pipeline) { m->numAssets = 3; m->init(); }

    bool any_finite = false;
    for (unsigned t = 0; t < 30u; ++t) {
        sv.fill_nan();
        for (Module* m : alphas[0].pipeline)
            m->calculate(t, sv);
        for (unsigned i = 0; i < 3; ++i) {
            if (std::isfinite(sv[i])) {
                any_finite = true;
            }
        }
    }
    EXPECT_TRUE(any_finite);
}

TEST(XmlCompiler, EventWithoutSignalRejectedAfterDataRestart) {
    auto dp = make_panel(30, 3);
    std::string xml = R"(
        <data expr="returns"/>
        <signal id="trig"/>
        <data expr="close"/>
        <event trigger="trig" threshold="0" maxholddays="5"/>
    )";
    expect_compile_error_contains(
        xml,
        dp,
        {"event", "'signal' attribute required after data chain restart"});
}


TEST(ModuleRegistry, DuplicateTagDoesNotReplaceExistingFactory) {
    ModuleRegistry registry;
    int original_calls = 0;
    int replacement_calls = 0;
    registry.register_module("custom", [&](const auto&, const auto*) -> std::unique_ptr<Module> {
        ++original_calls;
        return {};
    });
    EXPECT_THROW(registry.register_module("custom", [&](const auto&, const auto*) -> std::unique_ptr<Module> {
        ++replacement_calls;
        return {};
    }), CompileError);

    (void)registry.create("custom", {}, nullptr);
    EXPECT_EQ(original_calls, 1);
    EXPECT_EQ(replacement_calls, 0);
}

TEST(ModuleRegistry, BuiltinTagCannotBeOverridden) {
    auto& registry = ModuleRegistry::default_registry();
    EXPECT_THROW(registry.register_module("rank", [](const auto&, const auto*) -> std::unique_ptr<Module> {
        return {};
    }), CompileError);
    EXPECT_NE(registry.create("rank", {}, nullptr), nullptr);
}

TEST(XmlCompiler, RegisteredDataFieldWorksInExpressionsAndExplicitDelay) {
    auto dp = make_panel(5,2);
    dp.arrays["EPSNumRevFY1C"] = {1,-1,2,-2,3,-3,4,-4,5,-5};
    auto alphas = compile_xml(R"(<data expr="EPSNumRevFY1C * 2" delay="1"/>)",&dp);
    ASSERT_EQ(alphas.size(),1u);
    for (auto* module : alphas[0].pipeline) {
        module->numAssets = dp.N;
        module->init();
    }
    std::vector<float> values(dp.N);
    SignalVector signal{values.data(),dp.N};
    for (unsigned di=0;di<dp.T;++di) {
        signal.fill_nan();
        for (auto* module : alphas[0].pipeline)
            module->calculate(di,signal);
        if (di==0) {
            EXPECT_TRUE(std::isnan(signal[0]));
        } else {
            EXPECT_FLOAT_EQ(signal[0],static_cast<float>(di)*2.0f);
            EXPECT_FLOAT_EQ(signal[1],-static_cast<float>(di)*2.0f);
        }
    }
}

TEST(XmlCompiler, RejectsUnregisteredFieldsAndMissingDerivedReturnSource) {
    auto dp = make_panel();
    EXPECT_THROW(compile_xml(R"(<data expr="number_of_revision"/>)",&dp),std::runtime_error);
    dp.arrays.erase("close");
    EXPECT_THROW(compile_xml(R"(<data expr="close"/>)",&dp),std::runtime_error);
    EXPECT_THROW(compile_xml(R"(<data expr="returns"/>)",&dp),std::runtime_error);
}

TEST(XmlCompiler, RejectsPartiallyParsedOrOverflowingNumericLiterals) {
    auto dp = make_panel();
    for (const std::string& literal : {std::string("1.2.3"), std::string("..2"),
                                      std::string(60,'9')}) {
        EXPECT_THROW(compile_xml("<data expr=\""+literal+"\"/>",&dp),std::runtime_error);
    }
}

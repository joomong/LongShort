// Built with the generated registry of tests/fixtures/discovered_modules, which
// replaces the engine's own discovered-module registry in this binary only.
#include <gtest/gtest.h>
#include <string>
#include <vector>
#include "xml_compiler.h"
#include "simulation_data.h"
#include "modules/Rank.h"
#include "UniverseGroup.h"

using discovered_fixture::MUniverseGroup;

static DataPanel make_panel() {
    DataPanel dp;
    dp.T = 5;
    dp.N = 3;
    dp.timestamps.resize(dp.T);
    for (unsigned t = 0; t < dp.T; ++t) dp.timestamps[t] = (int64_t)t * 86400000LL;
    dp.symbols = {"A", "B", "C"};
    for (const char* col : {"close", "sector", "industry"}) {
        auto& arr = dp.arrays[col];
        arr.assign(static_cast<size_t>(dp.T) * dp.N, 1.0f);
    }
    return dp;
}

static MUniverseGroup* custom(const CompiledAlpha& alpha) {
    for (const auto& module : alpha.modules)
        if (auto* m = dynamic_cast<MUniverseGroup*>(module.get())) return m;
    return nullptr;
}

static const std::vector<uint8_t>* rank_universe(const CompiledAlpha& alpha) {
    for (const auto& module : alpha.modules)
        if (auto* m = dynamic_cast<MRank*>(module.get())) return m->universe;
    return nullptr;
}

TEST(DiscoveredModules, ReceiveTheRunSelectedUniverseLikeBuiltIns) {
    auto dp = make_panel();
    const std::vector<uint8_t> top_a(15, 1), top_b(15, 0);
    const std::string xml = R"(<data expr="close"/><rank/><universegroup/>)";
    auto a = compile_xml(xml, &dp, &top_a);
    auto b = compile_xml(xml, &dp, &top_b);
    ASSERT_EQ(a.size(), 1u);
    ASSERT_EQ(b.size(), 1u);
    ASSERT_NE(custom(a[0]), nullptr);
    EXPECT_EQ(custom(a[0])->universe, &top_a);
    EXPECT_EQ(custom(a[0])->universe, rank_universe(a[0]));
    EXPECT_EQ(custom(b[0])->universe, &top_b);
    EXPECT_EQ(custom(b[0])->universe, rank_universe(b[0]));
    EXPECT_NE(custom(a[0])->universe, custom(b[0])->universe);
    EXPECT_EQ(custom(a[0])->panel, &dp);

    auto none = compile_xml(xml, &dp);
    EXPECT_EQ(custom(none[0])->universe, nullptr);
    EXPECT_EQ(rank_universe(none[0]), nullptr);
}

TEST(DiscoveredModules, GroupParameterIsValidatedLikeBuiltInGroups) {
    auto dp = make_panel();
    auto chosen = compile_xml(R"(<data expr="close"/><universegroup group=" industry " window="4"/>)", &dp);
    EXPECT_EQ(std::string(custom(chosen[0])->group), "industry");
    EXPECT_EQ(custom(chosen[0])->group.name(), "industry");
    EXPECT_EQ(static_cast<int>(custom(chosen[0])->window), 4);

    auto absent = compile_xml(R"(<data expr="close"/><universegroup/>)", &dp);
    EXPECT_EQ(custom(absent[0])->group.name(), "");
    EXPECT_EQ(static_cast<int>(custom(absent[0])->window), 3);

    auto expect_error = [](const std::string& xml, const DataPanel& panel, const std::string& text) {
        try {
            (void)compile_xml(xml, &panel);
            ADD_FAILURE() << "expected CompileError for " << xml;
        } catch (const CompileError& e) {
            EXPECT_EQ(std::string(e.what()), text);
        }
    };
    expect_error(R"(<data expr="close"/><universegroup group="country"/>)", dp,
                 "Unsupported universegroup group 'country' (supported: sector, industry)");
    expect_error(R"(<data expr="close"/><universegroup group=""/>)", dp,
                 "Unsupported universegroup group '' (supported: sector, industry)");
    auto no_industry = make_panel();
    no_industry.arrays.erase("industry");
    expect_error(R"(<data expr="close"/><universegroup group="industry"/>)", no_industry,
                 "universegroup group 'industry' needs data field 'industry' (add base: {cache, groups} to the config)");
    expect_error(R"(<data expr="close"/><universegroup grp="sector"/>)", dp,
                 "Unknown attribute 'grp' for <universegroup>");
}

namespace {
struct MNumericFixture : Module {
    IntParameter days{7};
    FloatParameter factor{0.5f};
    void init() override {}
    void calculate(unsigned, SignalVector&) override {}
};
struct MEmptyFixture : Module {
    void init() override {}
    void calculate(unsigned, SignalVector&) override {}
};
}
REGISTER_MODULE(EmptyFixture, MEmptyFixture, "empty_fixture")

TEST(DiscoveredModules, CppParametersRetainDefaultsAndValidateValues) {
    ModuleRegistry registry;
    register_type<MNumericFixture>(registry, "numeric")
        .parameter("days", &MNumericFixture::days)
        .parameter("factor", &MNumericFixture::factor);
    auto plain = registry.create("numeric", {}, nullptr);
    auto* defaults = dynamic_cast<MNumericFixture*>(plain.get());
    ASSERT_NE(defaults, nullptr);
    EXPECT_EQ(static_cast<int>(defaults->days), 7);
    EXPECT_FLOAT_EQ(static_cast<float>(defaults->factor), 0.5f);
    auto configured = registry.create("numeric", {{"days", " 4 "}, {"factor", "-1.25"}}, nullptr);
    auto* values = dynamic_cast<MNumericFixture*>(configured.get());
    EXPECT_EQ(static_cast<int>(values->days), 4);
    EXPECT_FLOAT_EQ(static_cast<float>(values->factor), -1.25f);
    EXPECT_THROW(registry.create("numeric", {{"days", "1.5"}}, nullptr), CompileError);
    EXPECT_THROW(registry.create("numeric", {{"factor", "nan"}}, nullptr), CompileError);
    EXPECT_THROW(registry.create("numeric", {{"factor", "1e100"}}, nullptr), CompileError);
    EXPECT_THROW(registry.create("numeric", {{"typo", "1"}}, nullptr), CompileError);
    EXPECT_THROW(register_type<MNumericFixture>(registry, "numeric"), CompileError);
    EXPECT_THROW(register_type<MNumericFixture>(registry, "data"), CompileError);
    EXPECT_THROW(register_type<MNumericFixture>(ModuleRegistry::default_registry(), "rank"), CompileError);
    EXPECT_THROW(register_type<MNumericFixture>(registry, "duplicate_parameter")
        .parameter("days", &MNumericFixture::days).parameter("days", &MNumericFixture::days), CompileError);
}

TEST(DiscoveredModules, RegistrationMacroSupportsNoParameters) {
    ModuleRegistry registry;
    register_EmptyFixture(registry);
    EXPECT_NE(registry.create("empty_fixture", {}, nullptr), nullptr);
    EXPECT_THROW(registry.create("empty_fixture", {{"unexpected", "1"}}, nullptr), CompileError);
}

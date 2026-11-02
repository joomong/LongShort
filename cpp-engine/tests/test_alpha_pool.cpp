#include <gtest/gtest.h>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "api/alpha_pool.h"

namespace fs = std::filesystem;
using alpha_pool::Cache;
using alpha_pool::Series;

namespace {

struct TempPool {
    fs::path dir;
    TempPool() {
        static int counter = 0;
        dir = fs::temp_directory_path() /
              ("longshort_alpha_pool_test_" + std::to_string(static_cast<long long>(::getpid())) + "_" + std::to_string(counter++));
        fs::create_directories(dir);
    }
    ~TempPool() { std::error_code error; fs::remove_all(dir, error); }
    void put(const std::string& name, const std::string& xml) {
        fs::create_directories(dir / name);
        std::ofstream(dir / name / "alpha.xml") << xml;
    }
};

std::vector<double> ramp(std::size_t n, double slope, double start_at = 1.0) {
    std::vector<double> v(n, 0.0);
    for (std::size_t t = 0; t < n; ++t) v[t] = t == 0 ? 0.0 : start_at + slope * static_cast<double>(t);
    return v;
}

}  // namespace

TEST(AlphaPool, ListsAlphaXmlFoldersByName) {
    TempPool pool;
    pool.put("b_alpha", "<data expr=\"close\"/><rank/>");
    pool.put("a_alpha", "<data expr=\"returns\" delay=\"1\"/><rank/>");
    fs::create_directories(pool.dir / "no_xml_here");
    std::ofstream(pool.dir / "README.md") << "# pool\n";
    std::ofstream(pool.dir / "stray.xml") << "<x/>";
    const auto listed = alpha_pool::list(pool.dir);
    ASSERT_EQ(listed.size(), 2u);
    EXPECT_EQ(listed[0].name, "a_alpha");
    EXPECT_EQ(listed[0].xml, "<data expr=\"returns\" delay=\"1\"/><rank/>");
    EXPECT_EQ(listed[1].name, "b_alpha");
    EXPECT_TRUE(alpha_pool::list(pool.dir / "missing").empty());
}

TEST(AlphaPool, CorrelationUsesTheOverlapAfterBothStartAndNeedsMinDays) {
    const auto a = ramp(100, 1.0);         // zero at row 0, then 1 + t
    const auto b = ramp(100, 2.0, 5.0);    // 5 + 2t: perfectly correlated
    std::size_t days = 0;
    EXPECT_NEAR(alpha_pool::correlation(a, alpha_pool::first_active_row(a), b, alpha_pool::first_active_row(b),
                                        0, 100, 60, &days), 1.0, 1e-12);
    EXPECT_EQ(days, 99u);
    std::vector<double> neg(100);
    for (std::size_t t = 0; t < 100; ++t) neg[t] = -a[t];
    EXPECT_NEAR(alpha_pool::correlation(a, 1, neg, 1, 0, 100, 60, &days), -1.0, 1e-12);
    // A window that starts later than both first-active rows only uses the window.
    EXPECT_NEAR(alpha_pool::correlation(a, 1, b, 1, 30, 100, 60, &days), 1.0, 1e-12);
    EXPECT_EQ(days, 70u);
    // Too few overlapping days -> NaN but days still reported.
    EXPECT_TRUE(std::isnan(alpha_pool::correlation(a, 1, b, 1, 50, 100, 60, &days)));
    EXPECT_EQ(days, 50u);
    // Zero variance on one side -> NaN.
    const std::vector<double> flat(100, 3.0);
    EXPECT_TRUE(std::isnan(alpha_pool::correlation(a, 1, flat, 0, 0, 100, 60, &days)));
    // A pool alpha that starts late shortens the overlap.
    EXPECT_NEAR(alpha_pool::correlation(a, 1, b, 80, 0, 100, 10, &days), 1.0, 1e-12);
    EXPECT_EQ(days, 20u);
    EXPECT_EQ(alpha_pool::first_active_row(flat), 0u);
    EXPECT_EQ(alpha_pool::first_active_row(std::vector<double>(5, 0.0)), 5u);
}

TEST(AlphaPool, EqualWeightAveragesOnlyTheAlphasActiveOnEachBar) {
    Series s1; s1.name = "one"; s1.ret = {1.0, 1.0, 1.0, 1.0}; s1.first_active = 0;
    Series s2; s2.name = "two"; s2.ret = {0.0, 0.0, 2.0, 2.0}; s2.first_active = 2;
    const Series ew = alpha_pool::equal_weight({s1, s2});
    ASSERT_EQ(ew.ret.size(), 4u);
    EXPECT_DOUBLE_EQ(ew.ret[0], 1.0);
    EXPECT_DOUBLE_EQ(ew.ret[1], 1.0);
    EXPECT_DOUBLE_EQ(ew.ret[2], 1.5);
    EXPECT_DOUBLE_EQ(ew.ret[3], 1.5);
    EXPECT_EQ(ew.first_active, 0u);
    EXPECT_EQ(alpha_pool::equal_weight({}).ret.size(), 0u);
}

TEST(AlphaPool, CacheRefreshSimulatesNewOrChangedAlphasOnlyAndRemembersFailures) {
    TempPool pool;
    pool.put("a1", "<x1/>");
    pool.put("a2", "<x2/>");
    pool.put("bad", "<bad/>");
    std::map<std::string, int> calls;
    const auto run = [&](const std::string& xml) -> std::vector<double> {
        ++calls[xml];
        if (xml == "<bad/>") throw std::runtime_error("does not compile");
        return xml == "<x1/>" ? std::vector<double>{0.0, 1.0, 1.0, 1.0} : std::vector<double>{0.0, 0.0, 2.0, 2.0};
    };
    Cache cache(pool.dir);
    std::vector<std::string> failures;
    EXPECT_EQ(cache.refresh(run, 2, &failures), 2u);
    EXPECT_EQ(cache.names(), (std::vector<std::string>{"a1", "a2"}));
    ASSERT_EQ(failures.size(), 1u);
    EXPECT_EQ(failures[0].rfind("bad: ", 0), 0u);

    failures.clear();
    EXPECT_EQ(cache.refresh(run, 2, &failures), 0u);   // unchanged listing: nothing runs again
    EXPECT_TRUE(failures.empty());
    EXPECT_EQ(calls["<x1/>"], 1); EXPECT_EQ(calls["<x2/>"], 1); EXPECT_EQ(calls["<bad/>"], 1);

    pool.put("bad", "<x1/>");                           // fixed XML -> simulated once more
    fs::remove_all(pool.dir / "a2");                    // removed from the pool -> dropped
    EXPECT_EQ(cache.refresh(run, 2, &failures), 1u);
    EXPECT_EQ(cache.names(), (std::vector<std::string>{"a1", "bad"}));
    EXPECT_EQ(calls["<x1/>"], 2);
    EXPECT_EQ(calls["<x2/>"], 1);
}

TEST(AlphaPool, CorrelationJsonReportsTheEwCorrelationOrNull) {
    TempPool pool;
    pool.put("p1", "<p1/>");
    pool.put("p2", "<p2/>");
    const auto p1 = ramp(100, 1.0);
    std::vector<double> p2(100);
    for (std::size_t t = 0; t < 100; ++t) p2[t] = 3.0 * p1[t];   // same shape: EW = 2 * p1
    const auto run = [&](const std::string& xml) { return xml == "<p1/>" ? p1 : p2; };
    Cache cache(pool.dir);
    EXPECT_TRUE(cache.correlation_json(p1, 0, 100).isNull());   // empty pool: no column
    cache.refresh(run, 2);
    const Json::Value out = cache.correlation_json(p1, 0, 100);
    ASSERT_TRUE(out.isObject());
    EXPECT_FALSE(out.isMember("alphas"));                         // one number per run, nothing per alpha
    EXPECT_NEAR(out["ew"].asDouble(), 1.0, 1e-12);
    EXPECT_EQ(out["days"].asUInt64(), 99u);
    std::vector<double> mirror(100);
    for (std::size_t t = 0; t < 100; ++t) mirror[t] = -p1[t];
    EXPECT_NEAR(cache.correlation_json(mirror, 0, 100)["ew"].asDouble(), -1.0, 1e-12);
    // A run with no position at all has no overlap: null, zero days.
    const Json::Value none = cache.correlation_json(std::vector<double>(100, 0.0), 0, 100);
    EXPECT_TRUE(none["ew"].isNull());
    EXPECT_EQ(none["days"].asUInt64(), 0u);
    // A short window reports null with the day count it had.
    const Json::Value shortw = cache.correlation_json(p1, 90, 100);
    EXPECT_TRUE(shortw["ew"].isNull());
    EXPECT_EQ(shortw["days"].asUInt64(), 10u);
}

#include <gtest/gtest.h>
#include "alpha_search.h"
#include "parallel_executor.h"

// ---------------------------------------------------------------------------
// Single vs multi-thread determinism
// Same seed → same best result regardless of thread count
// ---------------------------------------------------------------------------

TEST(Determinism, SingleVsMultiThread) {
    std::string xml = R"(<tsmean days="5|10|15|20|25|30"/>)";
    SearchSpace space = extract_search_space(xml);

    auto eval = [](const std::string& decoded_xml) -> double {
        auto pos = decoded_xml.find("days=\"");
        if (pos == std::string::npos) return 0.0;
        pos += 6;
        auto end = decoded_xml.find('"', pos);
        int days = std::stoi(decoded_xml.substr(pos, end - pos));
        // Fitness peaks at days=20
        return -(double)((days - 20) * (days - 20));
    };

    GAConfig cfg1;
    cfg1.population_size = 20;
    cfg1.generations     = 8;
    cfg1.seed            = 42;
    cfg1.n_threads       = 1;

    GAConfig cfg4 = cfg1;
    cfg4.n_threads = 4;

    GAEngine engine1(xml, space, cfg1);
    GAResult r1 = engine1.run(eval);

    GAEngine engine4(xml, space, cfg4);
    GAResult r4 = engine4.run(eval);

    // Both should find the same best XML (days=20)
    EXPECT_EQ(r1.best_xml, r4.best_xml);
}

// ---------------------------------------------------------------------------
// Cache hits: duplicate XMLs in a batch should trigger cache
// ---------------------------------------------------------------------------

TEST(Determinism, CacheHits) {
    int call_count = 0;
    auto eval = [&call_count](const std::string& /*xml*/) -> double {
        ++call_count;
        return 1.0;
    };

    ParallelEvaluator evaluator(1, eval);

    // Batch with duplicates
    std::vector<std::string> batch = {
        "<tsmean days=\"10\"/>",
        "<tsmean days=\"20\"/>",
        "<tsmean days=\"10\"/>",   // duplicate of [0]
        "<tsmean days=\"20\"/>",   // duplicate of [1]
        "<tsmean days=\"10\"/>",   // duplicate of [0]
    };

    std::vector<double> results = evaluator.evaluate_batch(batch);

    // All results should be 1.0
    for (double r : results) {
        EXPECT_DOUBLE_EQ(r, 1.0);
    }

    // Only 2 unique XMLs → call_count == 2, cache_hits == 3
    EXPECT_EQ(call_count, 2);
    EXPECT_EQ(evaluator.cache_hits(), 3u);
}

// ---------------------------------------------------------------------------
// Cache persists across multiple batches
// ---------------------------------------------------------------------------

TEST(Determinism, CachePersistsAcrossBatches) {
    int call_count = 0;
    auto eval = [&call_count](const std::string& /*xml*/) -> double {
        ++call_count;
        return 2.0;
    };

    ParallelEvaluator evaluator(1, eval);

    std::vector<std::string> batch1 = {"<tsmean days=\"10\"/>"};
    std::vector<std::string> batch2 = {"<tsmean days=\"10\"/>"};  // same XML

    evaluator.evaluate_batch(batch1);
    evaluator.evaluate_batch(batch2);

    // First batch: 1 call; second batch: cached → 0 additional calls
    EXPECT_EQ(call_count, 1);
    EXPECT_EQ(evaluator.cache_hits(), 1u);
}

#include <gtest/gtest.h>
#include "ga/search_space.h"
#include "ga/ga_engine.h"

// ---------------------------------------------------------------------------
// SearchSpace extraction
// ---------------------------------------------------------------------------

TEST(GA, SearchSpaceExtractionPipe) {
    // Pipe in days attribute → gene with 3 values
    std::string xml = R"(<tsmean days="10|20|30"/>)";
    SearchSpace space = extract_search_space(xml);

    ASSERT_EQ(space.genes.size(), 1u);
    const auto& gene_values = space.genes[0].values;
    ASSERT_EQ(gene_values.size(), 3u);
    EXPECT_EQ(gene_values[0], "10");
    EXPECT_EQ(gene_values[1], "20");
    EXPECT_EQ(gene_values[2], "30");
}

TEST(GA, SearchSpaceExtractionRange) {
    // Range "20...10...60" → 5 values: 20,30,40,50,60
    std::string xml = R"(<tsmean days="20...10...60"/>)";
    SearchSpace space = extract_search_space(xml);

    ASSERT_EQ(space.genes.size(), 1u);
    const auto& gene_values = space.genes[0].values;
    ASSERT_EQ(gene_values.size(), 5u);
    EXPECT_EQ(gene_values[0], "20");
    EXPECT_EQ(gene_values[4], "60");
}

TEST(GA, SearchSpaceDecodeReconstructsXml) {
    std::string xml = R"(<tsmean days="10|20|30"/>)";
    SearchSpace space = extract_search_space(xml);

    // The only real gene has 3 values; index 1 → "20"
    std::string decoded = space.decode({1});
    EXPECT_NE(decoded.find("20"), std::string::npos);
    // Should not contain the pipe or other values
    EXPECT_EQ(decoded.find('|'), std::string::npos);
}

TEST(GA, SearchSpaceTotalCombinations) {
    // Two genes: 3 × 2 = 6 combinations
    std::string xml = R"(<tsmean days="10|20|30"/><delay n="1|2"/>)";
    SearchSpace space = extract_search_space(xml);

    size_t total = space.total_combinations();
    EXPECT_EQ(total, 6u);
}

// ---------------------------------------------------------------------------
// GA convergence
// ---------------------------------------------------------------------------

TEST(GA, GAConverges) {
    // Single gene with values 10, 20, 30.
    // Fitness function rewards days=20 (index 1) → fitness = 1.0, others = 0.0
    std::string xml = R"(<tsmean days="10|20|30"/>)";
    SearchSpace space = extract_search_space(xml);

    GAConfig cfg;
    cfg.population_size = 20;
    cfg.generations     = 10;
    cfg.seed            = 42;
    cfg.fitness_metric  = "sharpe";

    GAEngine engine(xml, space, cfg);

    auto eval = [](const std::string& decoded_xml) -> double {
        if (decoded_xml.find("days=\"20\"") != std::string::npos) return 1.0;
        return 0.0;
    };

    GAResult result = engine.run(eval);

    EXPECT_NE(result.best_xml.find("days=\"20\""), std::string::npos);
    EXPECT_EQ(result.history.size(), 10u);
}

// ---------------------------------------------------------------------------
// Deterministic seed
// ---------------------------------------------------------------------------

TEST(GA, DeterministicSeed) {
    std::string xml = R"(<tsmean days="10|20|30|40|50"/>)";
    SearchSpace space = extract_search_space(xml);

    GAConfig cfg;
    cfg.population_size = 15;
    cfg.generations     = 5;
    cfg.seed            = 42;
    cfg.n_threads       = 1;

    auto eval = [](const std::string& decoded_xml) -> double {
        // fitness proportional to days value
        auto pos = decoded_xml.find("days=\"");
        if (pos == std::string::npos) return 0.0;
        pos += 6;
        auto end = decoded_xml.find('"', pos);
        int days = std::stoi(decoded_xml.substr(pos, end - pos));
        return (double)days;
    };

    GAEngine engine1(xml, space, cfg);
    GAResult r1 = engine1.run(eval);

    GAEngine engine2(xml, space, cfg);
    GAResult r2 = engine2.run(eval);

    EXPECT_EQ(r1.best_xml, r2.best_xml);
    EXPECT_EQ(r1.history.size(), r2.history.size());
    for (size_t i = 0; i < r1.history.size(); ++i) {
        EXPECT_EQ(r1.history[i].first,  r2.history[i].first);
        EXPECT_DOUBLE_EQ(r1.history[i].second, r2.history[i].second);
    }
}


TEST(GA, RejectsAnEmptyPopulationOrZeroGenerations) {
    const std::string xml=R"(<tsmean days="2|3"/>)";
    const auto space=extract_search_space(xml);
    GAConfig cfg; cfg.population_size=0;
    EXPECT_THROW(GAEngine(xml,space,cfg),std::invalid_argument);
    cfg.population_size=2; cfg.generations=0;
    EXPECT_THROW(GAEngine(xml,space,cfg),std::invalid_argument);
}

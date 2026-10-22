#include <gtest/gtest.h>
#include "ga/search_space.h"
#include "ga/ga_engine.h"
#include "ga/parallel_evaluator.h"
#include <atomic>
#include <condition_variable>
#include <chrono>
#include <mutex>
#include <set>
#include <thread>

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

TEST(GA, SearchSpaceRejectsMalformedRange) {
    try {
        (void)extract_search_space(R"(<tsmean days="5.5...20"/>)");
        FAIL() << "Expected invalid range error";
    } catch (const std::exception& e) {
        EXPECT_EQ(std::string(e.what()),
                  "Invalid range '5.5...20' (expected integers start...end or start...step...end)");
    }
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

TEST(GA, SearchSpaceDecodePutsOneModulePerLine) {
    const std::string xml = R"(<data expr="close" delay="0"/><tsmean days="10|20"/><rank/>)";
    EXPECT_EQ(extract_search_space(xml).decode({1}),
              "<data delay=\"0\" expr=\"close\"/>\n<tsmean days=\"20\"/>\n<rank/>");

    const std::string choices = R"(<data expr="close"/><choice><tsmean days="5"/><delay days="1"/></choice><rank/>)";
    const auto space = extract_search_space(choices);
    ASSERT_EQ(space.total_combinations(), 2u);
    EXPECT_EQ(space.decode({0}), "<data expr=\"close\"/>\n<tsmean days=\"5\"/>\n<rank/>");
}

TEST(GA, SearchSpaceTotalCombinations) {
    // Two genes: 3 × 2 = 6 combinations
    std::string xml = R"(<tsmean days="10|20|30"/><delay days="1|2"/>)";
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

    GAEngine engine(xml, space, cfg);

    auto eval = [](const std::string& decoded_xml) -> double {
        if (decoded_xml.find("days=\"20\"") != std::string::npos) return 1.0;
        return 0.0;
    };

    GAResult result = engine.run(eval);

    EXPECT_NE(result.best_xml.find("days=\"20\""), std::string::npos);
    EXPECT_EQ(result.history.size(), 1u);
    EXPECT_EQ(result.stop_reason, "exhausted");
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

TEST(GA, FortyEightCombinationsFinishInRoundOneWithFirstSearchTwoHundred) {
    const std::string xml = "<tsmean days=\"1...48\"/>";
    GAConfig config; config.first_search = 200; config.n_threads = 20;
    std::set<std::string> evaluated;
    std::mutex mutex;
    std::vector<std::string> events;
    GAEvents callbacks;
    callbacks.round_start = [&](unsigned round, size_t count) {
        EXPECT_EQ(round, 1u); EXPECT_EQ(count, 48u); events.push_back("start");
    };
    callbacks.candidate = [&](const GACandidate& candidate) {
        EXPECT_EQ(candidate.round, 1u); EXPECT_TRUE(candidate.error.empty()); events.push_back("candidate");
    };
    callbacks.round_end = [&](unsigned round, const std::string&, double) {
        EXPECT_EQ(round, 1u); events.push_back("end");
    };
    const auto result = GAEngine(xml, extract_search_space(xml), config).run_detailed([&](const std::string& candidate) {
        std::lock_guard<std::mutex> lock(mutex);
        EXPECT_TRUE(evaluated.insert(candidate).second);
        return 1.0;
    }, callbacks);
    EXPECT_EQ(result.evaluated, 48u); EXPECT_EQ(result.history.size(), 1u);
    EXPECT_EQ(result.stop_reason, "exhausted"); EXPECT_EQ(evaluated.size(), 48u);
    ASSERT_EQ(events.size(), 50u); EXPECT_EQ(events.front(), "start"); EXPECT_EQ(events.back(), "end");
}

TEST(GA, LimitsUniqueEvaluationsAndRoundsWithoutOvershooting) {
    const std::string xml = "<tsmean days=\"1...100\"/>";
    GAConfig config; config.first_search = 7; config.population_size = 5;
    config.stop_alphas = 19; config.generations = 40; config.n_threads = 20;
    std::set<std::string> seen;
    std::mutex mutex;
    std::vector<size_t> sizes;
    GAEvents events; events.round_start = [&](unsigned, size_t size) { sizes.push_back(size); };
    auto evaluate = [&](const std::string& candidate) {
        std::lock_guard<std::mutex> lock(mutex); EXPECT_TRUE(seen.insert(candidate).second); return 1.0;
    };
    const auto result = GAEngine(xml, extract_search_space(xml), config).run_detailed(evaluate, events);
    EXPECT_EQ(result.evaluated, 19u); EXPECT_EQ(result.stop_reason, "stop_alphas");
    EXPECT_EQ(sizes, (std::vector<size_t>{7,5,5,2}));
    config.generations = 1; seen.clear(); sizes.clear();
    const auto limited = GAEngine(xml, extract_search_space(xml), config).run_detailed(evaluate, events);
    EXPECT_EQ(limited.evaluated, 7u); EXPECT_EQ(limited.stop_reason, "stop_round");
    config.stop_alphas = 3; seen.clear(); sizes.clear();
    EXPECT_EQ(GAEngine(xml, extract_search_space(xml), config).run_detailed(evaluate, events).evaluated, 3u);
}

TEST(GA, SerialAndTwentyThreadsSelectTheSameCandidatesAcrossRounds) {
    const std::string xml = "<tsmean days=\"1...100\"/><delay days=\"0|1|2\"/>";
    GAConfig config; config.first_search = 9; config.population_size = 7; config.stop_alphas = 30;
    std::vector<std::pair<size_t, std::string>> serial, parallel;
    auto run = [&](unsigned threads, auto& collected) {
        config.n_threads = threads;
        GAEvents events; events.candidate = [&](const GACandidate& candidate) { collected.emplace_back(candidate.id, candidate.xml); };
        return GAEngine(xml, extract_search_space(xml), config).run_detailed([](const std::string& candidate) {
            return static_cast<double>(candidate.size());
        }, events);
    };
    const auto a = run(1, serial); const auto b = run(20, parallel);
    std::sort(serial.begin(), serial.end()); std::sort(parallel.begin(), parallel.end());
    EXPECT_EQ(serial, parallel); EXPECT_EQ(a.best_xml, b.best_xml); EXPECT_EQ(a.history, b.history);
}

TEST(GA, SingleXmlAndRepeatedChoicesAreEvaluatedOnce) {
    const std::string xml = "<tsmean days=\"2| 2 |3\"/>";
    const auto space = extract_search_space(xml); EXPECT_EQ(space.total_combinations(), 2u);
    GAConfig config;
    EXPECT_EQ(GAEngine(xml, space, config).run([](const std::string&) { return 0.0; }).evaluated, 2u);
    const std::string single = "<tsmean days=\"2\"/>";
    EXPECT_EQ(GAEngine(single, extract_search_space(single), config).run([](const std::string&) { return 0.0; }).evaluated, 1u);
    const std::string choices = "<choice><tsmean days=\"2|3\"/><delay days=\"1|2\"/></choice>";
    EXPECT_EQ(extract_search_space(choices).total_combinations(), 4u);
}

TEST(ParallelEvaluator, UsesAllTwentyWorkersAndSerializesCompletionCallbacks) {
    std::mutex mutex; std::condition_variable ready; unsigned entered = 0;
    std::atomic<bool> timed_out{false};
    const auto controller = std::this_thread::get_id();
    std::vector<std::string> xmls; for (int i=0;i<48;++i) xmls.push_back(std::to_string(i));
    ParallelEvaluator evaluator(20, [&](const std::string&) {
        std::unique_lock<std::mutex> lock(mutex); ++entered; ready.notify_all();
        if (!ready.wait_for(lock, std::chrono::seconds(3), [&] { return entered >= 20; })) timed_out = true;
        return 1.0;
    });
    unsigned completed = 0;
    evaluator.evaluate_results(xmls, [&](size_t, const EvaluationResult&) {
        EXPECT_EQ(std::this_thread::get_id(), controller); ++completed;
    });
    EXPECT_FALSE(timed_out.load()); EXPECT_EQ(entered,48u); EXPECT_EQ(completed,48u);
}

TEST(ParallelEvaluator, IdleWorkerTakesRemainingTasksWhileAnotherIsBusy) {
    std::mutex mutex; std::condition_variable ready; unsigned other_completed = 0;
    bool timed_out = false;
    ParallelEvaluator evaluator(2, [&](const std::string& xml) {
        std::unique_lock<std::mutex> lock(mutex);
        if (xml == "slow") {
            if (!ready.wait_for(lock, std::chrono::seconds(3), [&] { return other_completed == 5; })) timed_out = true;
        } else { ++other_completed; ready.notify_all(); }
        return 1.0;
    });
    evaluator.evaluate_batch({"slow","a","b","c","d","e"});
    EXPECT_FALSE(timed_out); EXPECT_EQ(other_completed,5u);
}

TEST(ParallelEvaluator, ErrorsAreReportedAndCallbacksCanStopWithoutLeakingWorkers) {
    std::atomic<unsigned> calls{0};
    ParallelEvaluator evaluator(4, [&](const std::string& xml) {
        ++calls; if (xml == "bad") throw std::runtime_error("invalid module"); return 1.0;
    });
    auto results = evaluator.evaluate_results({"good","bad","good"});
    ASSERT_EQ(results.size(),3u); EXPECT_EQ(results[1].error,"invalid module"); EXPECT_EQ(calls,2u);
    evaluator.evaluate_batch({"good","bad"}); EXPECT_EQ(calls,2u);
    EXPECT_THROW(evaluator.evaluate_results({"another","next"}, [](size_t,const EvaluationResult&) {
        throw std::runtime_error("disconnected writer");
    }), std::runtime_error);
    ParallelEvaluator stopped(20, [](const std::string&) -> double { throw EvaluationCancelled(); });
    EXPECT_THROW(stopped.evaluate_batch({"a","b","c"}), EvaluationCancelled);
}

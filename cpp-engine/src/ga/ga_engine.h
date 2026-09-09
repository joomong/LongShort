#pragma once
#include <string>
#include <vector>
#include <functional>
#include <random>
#include "search_space.h"

struct GAConfig {
    unsigned population_size  = 50;
    unsigned generations      = 20;
    unsigned tournament_size  = 3;
    double   crossover_rate   = 0.8;
    double   mutation_rate    = 0.1;
    unsigned n_threads        = 0;    // 0 = auto (hardware_concurrency)
    unsigned seed             = 42;
};

struct GAResult {
    std::string best_xml;
    // per-generation record: (best_xml, best_fitness)
    std::vector<std::pair<std::string, double>> history;
};

class GAEngine {
public:
    GAEngine(const std::string& xml_template,
             const SearchSpace& space,
             const GAConfig& config);

    // The caller supplies the objective; GA maximizes the returned scalar.
    using EvalFunc = std::function<double(const std::string& xml)>;
    using ProgressCallback = std::function<void(unsigned gen,
                                                const std::string& xml,
                                                double fitness)>;

    // Run GA, return best result.
    GAResult run(EvalFunc evaluate);

    // Run GA with per-generation progress callback.
    GAResult run_streaming(EvalFunc evaluate, ProgressCallback cb);

private:
    std::string  xml_template_;
    SearchSpace  space_;
    GAConfig     config_;

    using Individual = std::vector<int>;

    Individual random_individual(std::mt19937& rng) const;
    Individual crossover(const Individual& a,
                         const Individual& b,
                         std::mt19937& rng) const;
    Individual mutate(const Individual& ind, std::mt19937& rng) const;
    int        tournament_select(const std::vector<double>& fitness,
                                 std::mt19937& rng) const;

    // Core loop shared by run() and run_streaming()
    GAResult run_impl(EvalFunc evaluate, ProgressCallback* cb);
};

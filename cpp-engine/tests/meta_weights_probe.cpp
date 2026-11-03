// Test-only CLI: reads member returns as CSV on stdin (header = member names, one row per bar,
// `nan` allowed) and prints compute_combine_weights as CSV plus
// "fallback_bars=N first_estimated_bar=M".
#include "meta.h"
#include "module_registration.h"
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>

static CombineMethod parse_method(const std::string& v) {
    if (v == "ew") return CombineMethod::Ew;
    if (v == "ewsign") return CombineMethod::EwSign;
    if (v == "mvo") return CombineMethod::Mvo;
    if (v == "minvar") return CombineMethod::MinVar;
    if (v == "erc") return CombineMethod::Erc;
    if (v == "ivol") return CombineMethod::Ivol;
    if (v == "spca") return CombineMethod::Spca;
    if (v == "glasso") return CombineMethod::Glasso;
    throw std::invalid_argument("unknown --method " + v);
}

static ShrinkMode parse_shrink(const std::string& v) {
    if (v == "diag") return ShrinkMode::Diag;
    if (v == "lw") return ShrinkMode::LedoitWolf;
    if (v == "cc") return ShrinkMode::ConstantCorrelation;
    if (v == "oas") return ShrinkMode::Oas;
    throw std::invalid_argument("unknown --shrink " + v);
}

int main(int argc, char** argv) {
    try {
        CombineSpec spec;
        MemberReturns returns;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (i + 1 >= argc) throw std::invalid_argument("missing value for " + flag);
            const std::string value = argv[++i];
            if (flag == "--method") spec.method = parse_method(value);
            else if (flag == "--days") spec.days = static_cast<unsigned>(std::stoul(value));
            else if (flag == "--shrink") spec.shrink = parse_shrink(value);
            else if (flag == "--delta") spec.delta = module_attributes::parse_float(value, "probe", "delta");
            else if (flag == "--k") spec.k = static_cast<unsigned>(std::stoul(value));
            else if (flag == "--alpha") spec.alpha = module_attributes::parse_float(value, "probe", "alpha");
            else if (flag == "--rebalance") spec.rebalance = static_cast<unsigned>(std::stoul(value));
            else if (flag == "--sim-start") returns.sim_start = static_cast<unsigned>(std::stoul(value));
            else throw std::invalid_argument("unknown flag " + flag);
        }
        std::string line;
        if (!std::getline(std::cin, line)) throw std::invalid_argument("empty input");
        {
            std::stringstream header(line);
            std::string name;
            while (std::getline(header, name, ',')) returns.paths.push_back(name);
        }
        returns.K = static_cast<unsigned>(returns.paths.size());
        while (std::getline(std::cin, line)) {
            if (line.empty()) continue;
            std::vector<double> row;
            std::stringstream cells(line);
            std::string cell;
            while (std::getline(cells, cell, ',')) row.push_back(std::strtod(cell.c_str(), nullptr));
            if (row.size() != returns.K) throw std::invalid_argument("row width differs from the header");
            returns.rows.push_back(std::move(row));
        }
        returns.T = static_cast<unsigned>(returns.rows.size());
        const CombineWeights weights = compute_combine_weights(returns, spec);
        for (const auto& row : weights.rows) {
            for (std::size_t k = 0; k < row.size(); ++k) std::printf(k ? ",%.17g" : "%.17g", row[k]);
            std::printf("\n");
        }
        std::printf("fallback_bars=%u first_estimated_bar=%u\n", weights.fallback_bars, weights.first_estimated_bar);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "meta_weights_probe: %s\n", error.what());
        return 2;
    }
}

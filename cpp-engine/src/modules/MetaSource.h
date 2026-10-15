#pragma once

#include <stdexcept>
#include <string>
#include <vector>

// Yields the combined (EW) row of a <meta> alpha set. The caller materializes the
// T x N matrix and sets `matrix` before run(); the compiler only creates the module.
struct MMetaSource : public Module {
    void init()
    {
        setting_output_to_nan_is_not_necessary();
    }
    void calculate(unsigned di, SignalVector &output)
    {
        if (matrix == nullptr)
            throw std::logic_error("meta set '" + name + "' was not materialized");
        if (di >= matrix->size() || (*matrix)[di].size() < numAssets)
            throw std::logic_error("meta set '" + name + "' matrix has " +
                                   std::to_string(matrix->size()) + " rows x " +
                                   std::to_string(matrix->empty() ? 0 : matrix->front().size()) +
                                   " columns; need T x N");
        const std::vector<float>& row = (*matrix)[di];
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = row[ii];
    }

    const std::vector<std::vector<float>>* matrix = nullptr;
    std::string name;
};

#include <gtest/gtest.h>
#include "simulation_data.h"
#include "loader.h"

TEST(DataPanel, AtAndRow) {
    DataPanel dp;
    dp.T = 3;
    dp.N = 2;
    dp.timestamps = {1000, 2000, 3000};
    dp.symbols = {"BTCUSDT", "ETHUSDT"};
    dp.arrays["close"] = {100.0f, 50.0f, 101.0f, 51.0f, 102.0f, 52.0f};

    EXPECT_FLOAT_EQ(dp.at("close", 0, 0), 100.0f);
    EXPECT_FLOAT_EQ(dp.at("close", 0, 1), 50.0f);
    EXPECT_FLOAT_EQ(dp.at("close", 2, 1), 52.0f);

    const float* row0 = dp.row("close", 0);
    EXPECT_FLOAT_EQ(row0[0], 100.0f);
    EXPECT_FLOAT_EQ(row0[1], 50.0f);

    const float* row2 = dp.row("close", 2);
    EXPECT_FLOAT_EQ(row2[0], 102.0f);
    EXPECT_FLOAT_EQ(row2[1], 52.0f);
}

TEST(DataPanel, AtThrowsOnMissingColumn) {
    DataPanel dp;
    dp.T = 1;
    dp.N = 1;
    EXPECT_THROW(dp.at("nonexistent", 0, 0), std::out_of_range);
}

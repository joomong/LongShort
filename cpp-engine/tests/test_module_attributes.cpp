#include <gtest/gtest.h>
#include "compiler/module_attributes.h"

#include <limits>

TEST(ModuleAttributes, IntegerParserAcceptsWhitespaceSignsAndIntBoundaries) {
    EXPECT_EQ(module_attributes::parse_int(" \t+12\n", "scale", "count"), 12);
    EXPECT_EQ(module_attributes::parse_int("-7", "scale", "count"), -7);
    EXPECT_EQ(module_attributes::parse_int("0", "scale", "count"), 0);
    const int low = std::numeric_limits<int>::min();
    const int high = std::numeric_limits<int>::max();
    EXPECT_EQ(module_attributes::parse_int(std::to_string(low), "scale", "count"), low);
    EXPECT_EQ(module_attributes::parse_int(std::to_string(high), "scale", "count"), high);
}

TEST(ModuleAttributes, IntegerParserRejectsPartialNumbersAndOverflow) {
    const std::vector<std::string> invalid = {
        "", " \t", "12junk", "3.0", "2e2", "0x10", "true", "1 2",
        std::to_string(static_cast<long long>(std::numeric_limits<int>::max()) + 1),
        std::to_string(static_cast<long long>(std::numeric_limits<int>::min()) - 1),
        "9999999999999999999999999999999999999999", std::string("7\0x", 3)
    };
    for (const auto& raw : invalid) {
        SCOPED_TRACE(raw);
        EXPECT_THROW(module_attributes::parse_int(raw, "scale", "count"), CompileError);
    }
}

TEST(ModuleAttributes, FloatParserAcceptsFiniteScientificNotation) {
    EXPECT_DOUBLE_EQ(module_attributes::parse_float(" \t-1.25e-3\n", "scale", "factor"), -0.00125);
    EXPECT_DOUBLE_EQ(module_attributes::parse_float("+2", "scale", "factor"), 2.0);
    EXPECT_DOUBLE_EQ(module_attributes::parse_float(".5", "scale", "factor"), 0.5);
    EXPECT_DOUBLE_EQ(module_attributes::parse_float("0", "scale", "factor"), 0.0);
}

TEST(ModuleAttributes, FloatParserRejectsNonfiniteOverflowAndPartialNumbers) {
    const std::vector<std::string> invalid = {
        "", " \t", "1.5junk", "1.2.3", "1 2", "NaN", "nan(payload)",
        "inf", "-Inf", "+INFINITY", "1e9999", "-1e9999", std::string("1.5\0x", 5)
    };
    for (const auto& raw : invalid) {
        SCOPED_TRACE(raw);
        EXPECT_THROW(module_attributes::parse_float(raw, "scale", "factor"), CompileError);
    }
}

TEST(ModuleAttributes, ParseErrorsIdentifyModuleAttributeAndValue) {
    try {
        (void)module_attributes::parse_int("wrong", "scale", "count");
        FAIL() << "Expected CompileError";
    } catch (const CompileError& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find("<scale>"), std::string::npos);
        EXPECT_NE(message.find("count"), std::string::npos);
        EXPECT_NE(message.find("wrong"), std::string::npos);
    }
}

TEST(ModuleAttributes, KeyValidationAllowsDefaultsAndCompilerInput) {
    EXPECT_NO_THROW(module_attributes::validate_keys({}, {"count", "factor"}, "scale"));
    EXPECT_NO_THROW(module_attributes::validate_keys(
        {{"count", "2"}, {"factor", "1.5"}}, {"count", "factor"}, "scale"));
    EXPECT_NO_THROW(module_attributes::validate_keys(
        {{"input", "saved_signal"}, {"factor", "1.5"}}, {"count", "factor"}, "scale"));
    EXPECT_NO_THROW(module_attributes::validate_keys({{"input", "saved_signal"}}, {}, "scale"));
}

TEST(ModuleAttributes, KeyValidationRejectsTyposWithModuleContext) {
    try {
        module_attributes::validate_keys({{"facotr", "1.5"}}, {"factor"}, "scale");
        FAIL() << "Expected CompileError";
    } catch (const CompileError& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find("facotr"), std::string::npos);
        EXPECT_NE(message.find("<scale>"), std::string::npos);
    }
    EXPECT_THROW(module_attributes::validate_keys({{"Factor", "1.5"}}, {"factor"}, "scale"), CompileError);
}

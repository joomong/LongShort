#include <gtest/gtest.h>
#include "module_registration.h"

#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <type_traits>

TEST(ModuleAttributes, IntegerParserAcceptsWhitespaceSignsAndIntBoundaries) {
    EXPECT_EQ(module_attributes::parse_int(" \t12\n", "scale", "count"), 12);
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

TEST(ModuleAttributes, FloatParserAcceptsPlainDecimals) {
    EXPECT_FLOAT_EQ(module_attributes::parse_float(" \t-0.00125\n", "scale", "factor"), -0.00125f);
    EXPECT_FLOAT_EQ(module_attributes::parse_float("0.5", "scale", "factor"), 0.5);
    EXPECT_FLOAT_EQ(module_attributes::parse_float("-0.5", "scale", "factor"), -0.5);
    EXPECT_FLOAT_EQ(module_attributes::parse_float(".5", "scale", "factor"), 0.5);
    EXPECT_FLOAT_EQ(module_attributes::parse_float("5.", "scale", "factor"), 5.0);
    EXPECT_FLOAT_EQ(module_attributes::parse_float("5", "scale", "factor"), 5.0);
    EXPECT_FLOAT_EQ(module_attributes::parse_float(" 0.25 ", "scale", "factor"), 0.25f);
    EXPECT_FLOAT_EQ(module_attributes::parse_float("0", "scale", "factor"), 0.0);
}

TEST(ModuleAttributes, FloatParserRejectsScientificHexAndPlusWithExactText) {
    for (const std::string raw : {"1e-3", "1E3", "+0.5", "0x1p-3", "inf", "nan", "1.2.3", "", "."}) {
        SCOPED_TRACE(raw);
        try {
            (void)module_attributes::parse_float(raw, "zscore", "eps");
            FAIL() << "Expected CompileError";
        } catch (const CompileError& error) {
            EXPECT_EQ(std::string(error.what()),
                      "Invalid value for <zscore> attribute 'eps': '" + raw + "' (expected decimal number)");
        }
    }
}

TEST(ModuleAttributes, DecimalParserRangeLimits) {
    float value = 0.0f;
    // Overflow without an exponent: from_chars reports result_out_of_range -> rejected.
    EXPECT_FALSE(module_attributes::parse_decimal("1" + std::string(400, '0'), value));
    // Underflow (400 zeros after the point): libstdc++ from_chars reports result_out_of_range
    // instead of rounding to 0, so it is rejected as well (pinned).
    const std::string tiny = "0." + std::string(400, '0') + "1";
    EXPECT_FALSE(module_attributes::parse_decimal(tiny, value));
}

TEST(ModuleAttributes, IntegerParserRejectsSignsExponentsAndFractions) {
    for (const std::string raw : {"+5", "1e3", "5.0", "5x", ""}) {
        SCOPED_TRACE(raw);
        try {
            (void)module_attributes::parse_int(raw, "tsmean", "days");
            FAIL() << "Expected CompileError";
        } catch (const CompileError& error) {
            EXPECT_EQ(std::string(error.what()),
                      "Invalid value for <tsmean> attribute 'days': '" + raw + "' (expected integer within int range)");
        }
    }
    EXPECT_EQ(module_attributes::parse_int("-5", "tsmean", "days"), -5);
    EXPECT_EQ(module_attributes::parse_int("7", "tsmean", "days"), 7);
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

static_assert(std::is_same_v<decltype(FloatParameter::value), float>, "FloatParameter stores float");
static_assert(std::is_same_v<decltype(module_attributes::parse_float("", "", "")), float>);

static std::uint32_t float_bits(float f) {
    std::uint32_t u = 0;
    std::memcpy(&u, &f, sizeof u);
    return u;
}

TEST(ModuleAttributes, FloatParserRoundsOnceDirectlyToFloat) {
    EXPECT_EQ(float_bits(module_attributes::parse_float("0.1", "zscore", "eps")), float_bits(0.1f));
    // Text = exact float midpoint 1 + 2^-24 plus 1e-36. Direct float rounding goes up to
    // 1 + 2^-23. Via double the 1e-36 is lost (rounds to the midpoint exactly), and the
    // float tie then rounds to even = 1.0f. Constructed from the midpoint between 1.0f
    // and nextafterf(1.0f, 2.0f); the double path is asserted below so the case is real.
    const std::string text = "1.000000059604644775390625000000000001";
    const float direct = module_attributes::parse_float(text, "zscore", "eps");
    EXPECT_EQ(float_bits(direct), float_bits(std::nextafter(1.0f, 2.0f)));
    double as_double = 0.0;
    std::from_chars(text.data(), text.data() + text.size(), as_double, std::chars_format::fixed);
    const float via_double = static_cast<float>(as_double);
    EXPECT_EQ(float_bits(via_double), float_bits(1.0f));
}

TEST(ModuleAttributes, FloatParserRejectsDecimalsOutsideFloatRange) {
    // 1e39 > FLT_MAX and 1e-46 < smallest float denormal (~1.4e-45): both fit a double.
    // libstdc++ from_chars<float> reports result_out_of_range for both (underflow is not
    // rounded to 0), so both are rejected with the usual message (pinned).
    const std::string huge = "1" + std::string(39, '0');
    const std::string tiny = "0." + std::string(45, '0') + "1";
    for (const auto& raw : {huge, "-" + huge, tiny}) {
        SCOPED_TRACE(raw);
        try {
            (void)module_attributes::parse_float(raw, "zscore", "eps");
            FAIL() << "Expected CompileError";
        } catch (const CompileError& error) {
            EXPECT_EQ(std::string(error.what()),
                      "Invalid value for <zscore> attribute 'eps': '" + raw + "' (expected decimal number)");
        }
        double as_double = 0.0;
        const auto fits = std::from_chars(raw.data(), raw.data() + raw.size(), as_double, std::chars_format::fixed);
        EXPECT_EQ(fits.ec, std::errc());
    }
}

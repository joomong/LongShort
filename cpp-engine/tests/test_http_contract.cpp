#include <gtest/gtest.h>

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include "meta.h"
#include "loader.h"

#include <algorithm>
#include <cctype>
#include <ctime>
#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <cstdio>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <json/json.h>
#include <yaml-cpp/yaml.h>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <stdexcept>
#include <chrono>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <tuple>
#include <unistd.h>
#include <vector>

namespace fs = std::filesystem;

namespace {
std::string date_string(int64_t epoch_ms) {
    const time_t seconds = epoch_ms / 1000;
    std::tm date{}; gmtime_r(&seconds, &date);
    char text[11]; std::strftime(text, sizeof text, "%Y-%m-%d", &date);
    return text;
}


std::string shell_quote(const std::string& s)
{
    std::string out = "'";
    for (char c : s) {
        if (c == '\'')
            out += "'\\''";
        else
            out.push_back(c);
    }
    out.push_back('\'');
    return out;
}

std::string run_command_capture(const std::string& cmd, int* rc_out = nullptr)
{
    FILE* fp = ::popen(cmd.c_str(), "r");
    if (fp == nullptr)
        return "";

    std::string out;
    char buf[256];
    while (std::fgets(buf, sizeof(buf), fp) != nullptr)
        out += buf;
    const int rc = ::pclose(fp);
    if (rc_out != nullptr)
        *rc_out = rc;
    return out;
}

struct HttpResult {
    int status = 0;
    std::string body;
};

bool parse_json(const std::string& s, Json::Value* out)
{
    Json::CharReaderBuilder rb;
    std::string errs;
    std::istringstream iss(s);
    return Json::parseFromStream(rb, iss, out, &errs);
}

std::string compact_json(const Json::Value& v)
{
    Json::StreamWriterBuilder wb;
    wb["indentation"] = "";
    return Json::writeString(wb, v);
}

HttpResult http_get(int port, const std::string& path)
{
    HttpResult out;
    const fs::path body_file = fs::temp_directory_path() /
        ("longshort_http_get_body_" + std::to_string(::getpid()) + ".json");

    std::ostringstream cmd;
    cmd << "curl -sS -o " << shell_quote(body_file.string())
        << " -w \"%{http_code}\" "
        << shell_quote("http://127.0.0.1:" + std::to_string(port) + path)
        << " 2>/dev/null";

    int rc = 0;
    const std::string status_str = run_command_capture(cmd.str(), &rc);
    if (rc == 0 && !status_str.empty())
        out.status = std::atoi(status_str.c_str());

    std::ifstream ifs(body_file);
    if (ifs.good()) {
        std::ostringstream body_ss;
        body_ss << ifs.rdbuf();
        out.body = body_ss.str();
    }
    std::error_code ec;
    fs::remove(body_file, ec);
    return out;
}

HttpResult http_post_json(int port, const std::string& path, const Json::Value& payload,
                          double max_time_seconds = 0.0)
{
    HttpResult out;
    const fs::path payload_file = fs::temp_directory_path() /
        ("longshort_http_payload_" + std::to_string(::getpid()) + ".json");
    const fs::path body_file = fs::temp_directory_path() /
        ("longshort_http_post_body_" + std::to_string(::getpid()) + ".json");

    {
        Json::StreamWriterBuilder wb;
        wb["indentation"] = "";
        std::ofstream ofs(payload_file);
        ofs << Json::writeString(wb, payload);
    }

    std::ostringstream cmd;
    cmd << "curl -sS -o " << shell_quote(body_file.string())
        << " -w \"%{http_code}\""
        << " -H \"Content-Type: application/json\""
        << " -X POST "
        << shell_quote("http://127.0.0.1:" + std::to_string(port) + path)
        << " --data-binary @" << shell_quote(payload_file.string());
    if (max_time_seconds > 0.0) cmd << " --max-time " << max_time_seconds;
    cmd << " 2>/dev/null";

    int rc = 0;
    const std::string status_str = run_command_capture(cmd.str(), &rc);
    if (rc == 0 && !status_str.empty())
        out.status = std::atoi(status_str.c_str());

    std::ifstream ifs(body_file);
    if (ifs.good()) {
        std::ostringstream body_ss;
        body_ss << ifs.rdbuf();
        out.body = body_ss.str();
    }

    std::error_code ec;
    fs::remove(payload_file, ec);
    fs::remove(body_file, ec);
    return out;
}

HttpResult http_delete(int port, const std::string& path)
{
    HttpResult out;
    const fs::path body_file = fs::temp_directory_path() /
        ("longshort_http_delete_body_" + std::to_string(::getpid()) + ".json");
    std::ostringstream cmd;
    cmd << "curl -sS -o " << shell_quote(body_file.string())
        << " -w \"%{http_code}\" -X DELETE "
        << shell_quote("http://127.0.0.1:" + std::to_string(port) + path)
        << " 2>/dev/null";
    int rc = 0;
    const std::string status_str = run_command_capture(cmd.str(), &rc);
    if (rc == 0 && !status_str.empty())
        out.status = std::atoi(status_str.c_str());
    std::ifstream ifs(body_file);
    if (ifs.good()) {
        std::ostringstream body_ss;
        body_ss << ifs.rdbuf();
        out.body = body_ss.str();
    }
    std::error_code ec;
    fs::remove(body_file, ec);
    return out;
}

fs::path find_server_binary()
{
    if (const char* path = std::getenv("LONGSHORT_TEST_SERVER"); path && *path) {
        if (!fs::is_regular_file(path))
            throw std::runtime_error("LONGSHORT_TEST_SERVER does not name an existing binary");
        return fs::weakly_canonical(path);
    }
    const std::vector<fs::path> candidates = {
        fs::current_path() / "longshort-server",
        fs::current_path().parent_path() / "longshort-server",
    };
    for (const auto& p : candidates) {
        if (fs::exists(p))
            return fs::weakly_canonical(p);
    }
    return {};
}

fs::path find_config_path()
{
    if (const char* path = std::getenv("LONGSHORT_TEST_CONFIG"); path && *path) {
        if (!fs::is_regular_file(path))
            throw std::runtime_error("LONGSHORT_TEST_CONFIG does not name an existing config");
        return fs::weakly_canonical(path);
    }
    return {};
}

class ServerProcess {
public:
    ServerProcess(fs::path bin, fs::path cfg, int port)
        : bin_(std::move(bin)), cfg_(std::move(cfg)), port_(port)
    {
    }

    bool start()
    {
        if (bin_.empty() || cfg_.empty())
            return false;
        if (pid_ > 0)
            return true;

        const pid_t pid = ::fork();
        if (pid < 0)
            return false;

        if (pid == 0) {
            const std::string port_s = std::to_string(port_);
            ::setenv("LONGSHORT_PORT", port_s.c_str(), 1);

            const fs::path log_path = fs::temp_directory_path() / "longshort_http_contract_server.log";
            int fd = ::open(log_path.string().c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
            if (fd >= 0) {
                ::dup2(fd, STDOUT_FILENO);
                ::dup2(fd, STDERR_FILENO);
                ::close(fd);
            }

            ::execl(bin_.c_str(), bin_.c_str(), cfg_.c_str(), static_cast<char*>(nullptr));
            _exit(127);
        }

        pid_ = pid;
        return wait_until_ready();
    }

    void stop()
    {
        if (pid_ <= 0)
            return;
        ::kill(pid_, SIGTERM);
        ::waitpid(pid_, nullptr, 0);
        pid_ = -1;
    }

    ~ServerProcess()
    {
        stop();
    }

    int port() const { return port_; }
    pid_t pid() const { return pid_; }

private:
    bool wait_until_ready()
    {
        for (int i = 0; i < 120; ++i) {
            int status = 0;
            const pid_t rc = ::waitpid(pid_, &status, WNOHANG);
            if (rc == pid_)
                return false;

            HttpResult r = http_get(port_, "/api/health");
            if (r.status == 200) {
                Json::Value j;
                if (parse_json(r.body, &j) && j.isMember("status") && j["status"].asString() == "ok")
                    return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
        return false;
    }

    fs::path bin_;
    fs::path cfg_;
    int port_ = 0;
    pid_t pid_ = -1;
};

// LONGSHORT_TEST_PORT lets parallel checkouts run the suite side by side; the
// strict-policy server below takes the next port.
int test_port()
{
    if (const char* raw = std::getenv("LONGSHORT_TEST_PORT"); raw && *raw) {
        const int port = std::atoi(raw);
        if (port > 0 && port < 65535) return port;
    }
    return 18080;
}

std::string iso_date(int64_t epoch_ms)
{
    const time_t seconds = epoch_ms / 1000;
    struct tm parts;
    gmtime_r(&seconds, &parts);
    char buffer[11];
    strftime(buffer, sizeof(buffer), "%Y-%m-%d", &parts);
    return buffer;
}

class HttpContractTest : public ::testing::Test {
protected:
    static void SetUpTestSuite()
    {
        fs::path bin = find_server_binary();
        fs::path cfg = find_config_path();
        if (bin.empty()) {
            available_ = false;
            skip_reason_ = "longshort-server binary not found";
            return;
        }
        if (cfg.empty()) {
            available_ = false;
            skip_reason_ = "LONGSHORT_TEST_CONFIG not set";
            return;
        }

        // Each run saves alphas into its own temporary alphas_dir, never beside the config.
        run_dir_ = fs::temp_directory_path() / ("longshort_http_alphas_" + std::to_string(::getpid()));
        fs::create_directories(run_dir_);
        std::ifstream config_in(cfg);
        std::ostringstream config_text;
        config_text << config_in.rdbuf();
        Json::Value json_config;
        fs::path run_cfg;
        if (parse_json(config_text.str(), &json_config) && json_config.isObject()) {
            json_config["alphas_dir"] = (run_dir_ / "alphas").string();
            run_cfg = run_dir_ / "config.yaml";  // JSON fixture paths are absolute
            std::ofstream(run_cfg) << compact_json(json_config);
        } else {
            // Beside the original so relative data paths still resolve; replaces any alphas_dir.
            remove_stale_sibling_configs(cfg.parent_path());
            YAML::Node yaml_config = YAML::LoadFile(cfg.string());
            yaml_config["alphas_dir"] = (run_dir_ / "alphas").string();
            YAML::Emitter emitter;
            emitter << yaml_config;
            run_cfg = cfg.parent_path() / (".longshort_http_test_" + std::to_string(::getpid()) + ".yaml");
            std::ofstream(run_cfg) << emitter.c_str() << "\n";
            run_cfg_sibling_ = run_cfg;
        }
        server_ = std::make_unique<ServerProcess>(bin, run_cfg, test_port());
        if (!server_->start()) {
            available_ = false;
            skip_reason_ = "failed to start server (likely sandbox/network restriction)";
            return;
        }
        available_ = true;
        // Fail fast before any save: tests must never write into a configured user alphas_dir.
        Json::Value config;
        const auto response = http_get(server_->port(), "/api/config");
        if (response.status != 200 || !parse_json(response.body, &config) ||
            fs::weakly_canonical(config["alphasDir"].asString()) != fs::weakly_canonical(run_dir_ / "alphas")) {
            setup_error_ = "server alphasDir is not the per-run dir " + (run_dir_ / "alphas").string() +
                           ": " + response.body.substr(0, 200);
            server_->stop();
        }
    }

    void SetUp() override
    {
        if (!setup_error_.empty()) FAIL() << setup_error_;
    }

    // .longshort_http_test_<pid>.yaml left by a killed run whose process is gone.
    static void remove_stale_sibling_configs(const fs::path& dir)
    {
        std::error_code error;
        for (const auto& entry : fs::directory_iterator(dir, error)) {
            const std::string name = entry.path().filename().string();
            const std::string prefix = ".longshort_http_test_", suffix = ".yaml";
            if (name.size() <= prefix.size() + suffix.size() || name.rfind(prefix, 0) != 0 ||
                name.compare(name.size() - suffix.size(), suffix.size(), suffix) != 0)
                continue;
            const std::string pid = name.substr(prefix.size(), name.size() - prefix.size() - suffix.size());
            if (pid.size() > 9 || !std::all_of(pid.begin(), pid.end(), [](unsigned char c) { return std::isdigit(c); }))
                continue;
            if (::kill(static_cast<pid_t>(std::stol(pid)), 0) == 0 || errno != ESRCH) continue;
            if (entry.is_regular_file(error)) fs::remove(entry.path(), error);
        }
    }

    static void TearDownTestSuite()
    {
        if (server_ != nullptr)
            server_->stop();
        server_.reset();
        std::error_code error;
        if (!run_cfg_sibling_.empty()) fs::remove(run_cfg_sibling_, error);
        if (!run_dir_.empty()) fs::remove_all(run_dir_, error);
        run_cfg_sibling_.clear();
        setup_error_.clear();
        run_dir_.clear();
    }

    static HttpResult simulate(const Json::Value& payload)
    {
        return http_post_json(server_->port(), "/api/simulate", payload);
    }

    static std::unique_ptr<ServerProcess> server_;
    static fs::path run_dir_;
    static fs::path run_cfg_sibling_;
    static std::string setup_error_;
    static bool available_;
    static std::string skip_reason_;
};

std::unique_ptr<ServerProcess> HttpContractTest::server_;
fs::path HttpContractTest::run_dir_;
fs::path HttpContractTest::run_cfg_sibling_;
std::string HttpContractTest::setup_error_;
bool HttpContractTest::available_ = false;
std::string HttpContractTest::skip_reason_;

}  // namespace

// A named universe smaller than the panel runs on a compacted panel with successors outside the universe.
TEST(HttpCompactPanel, RunKeepsMembersAndSuccessorsAndTheSuccessorsReceiveTheirPositions) {
    const fs::path cfg = find_config_path();
    const fs::path bin = find_server_binary();
    if (cfg.empty() || bin.empty()) GTEST_SKIP() << "LONGSHORT_TEST_CONFIG not set";
    const fs::path compact = cfg.parent_path() / "compact" / "config.yaml";
    ASSERT_TRUE(fs::exists(compact)) << compact;
    // The server writes alphas/ and .longshort/ here, never beside the fixture.
    const fs::path run_dir = fs::temp_directory_path() / ("longshort_http_compact_" + std::to_string(::getpid()));
    fs::create_directories(run_dir);
    struct RemoveDir { fs::path dir; ~RemoveDir() { std::error_code error; fs::remove_all(dir, error); } } remove_dir{run_dir};
    Json::Value run_config;
    std::ostringstream compact_text;
    compact_text << std::ifstream(compact).rdbuf();
    ASSERT_TRUE(parse_json(compact_text.str(), &run_config));
    run_config["alphas_dir"] = (run_dir / "alphas").string();
    const fs::path run_cfg = run_dir / "config.yaml";  // JSON fixture paths are absolute
    std::ofstream(run_cfg) << compact_json(run_config);
    ServerProcess server(bin, run_cfg, test_port() + 2);
    ASSERT_TRUE(server.start());
    const auto run = [&](const std::string& xml, const char* output) {
        Json::Value request;
        request["xml"] = xml;
        request["universe"] = "SMALL";
        request["simStart"] = "2021-01-01";
        request["isStart"] = "2021-01-01";
        request["osStart"] = "2021-07-01";
        request["output"] = output;
        return request;
    };
    const std::string raw = "<data expr=\"close\" delay=\"1\"/>", ranked = raw + "<rank/>";

    auto series_request = run(raw, "signalSeries");
    for (const char* symbol : {"1", "2", "3", "4", "5", "6"}) series_request["signalPlotSymbols"].append(symbol);
    const auto series_response = http_post_json(server.port(), "/api/simulate", series_request);
    ASSERT_EQ(series_response.status, 200) << series_response.body.substr(0, 300);
    Json::Value series;
    ASSERT_TRUE(parse_json(series_response.body, &series));
    const auto& plot = series["alphas"][0]["signals"]["plot"];
    const Json::ArrayIndex last = plot["values"].size() - 1;
    // An unmasked signal is finite wherever the column exists: 4 is the only dropped symbol.
    for (Json::ArrayIndex k = 0; k < 6; ++k) EXPECT_EQ(plot["values"][last][k].isNull(), k == 3) << k;
    EXPECT_EQ(plot["membership"][last].asString(), "110010");

    const auto full_response = http_post_json(server.port(), "/api/simulate", run(ranked, "full"));
    ASSERT_EQ(full_response.status, 200) << full_response.body.substr(0, 300);
    Json::Value full;
    ASSERT_TRUE(parse_json(full_response.body, &full));
    const auto& alpha = full["alphas"][0];
    Json::Value members(Json::arrayValue);
    for (const char* symbol : {"1", "2", "5"}) members.append(symbol);
    EXPECT_EQ(alpha["signals"]["plot"]["symbols"], members);
    EXPECT_EQ(alpha["missingData"]["successorConversions"].asUInt(), 1u);
    EXPECT_GT(alpha["missingData"]["convertedGross"].asDouble(), 0.0);
    EXPECT_GT(alpha["missingData"]["frozenGrossSum"].asDouble(), 0.0);

    const auto& performance = alpha["performance"];
    const auto positions_on = [&](const std::string& date) {
        for (Json::ArrayIndex i = 0; i < performance["timestamps"].size(); ++i)
            if (iso_date(performance["timestamps"][i].asInt64()) == date) return performance["positionCount"][i].asInt();
        ADD_FAILURE() << date;
        return -1;
    };
    EXPECT_EQ(positions_on("2021-02-28"), 3);
    EXPECT_EQ(positions_on("2021-03-01"), 4);  // members 1, 2, 5 plus the distribution in 3
    EXPECT_EQ(positions_on("2021-03-02"), 3);  // 3 is sold: it is outside the universe
    EXPECT_EQ(positions_on("2021-04-01"), 3);  // 5 ended; 1, 2 plus 6, frozen without a quote
    EXPECT_EQ(positions_on("2021-04-05"), 2);  // 6 quoted again and sold

    // Saved positions are order targets, so they name universe members only: a successor's received weight
    // is not a target. Weights by rank show the compact columns map back to the right symbols.
    Json::Value save;
    save["name"] = "compact_positions";
    save["xml"] = ranked;
    save["level"] = "positions";
    save["metadata"]["simStart"] = "2021-01-01";
    save["metadata"]["simEnd"] = "2021-12-31";
    save["metadata"]["isStart"] = "2021-01-01";
    save["metadata"]["osStart"] = "2021-07-01";
    save["metadata"]["universe"] = "SMALL";
    save["metadata"]["executionPrice"] = "close";
    save["metadata"]["threads"] = 2;
    Json::Value row;
    row["id"] = 1; row["round"] = 0; row["xml"] = ranked;
    save["rows"].append(row);
    const auto saved = http_post_json(server.port(), "/api/alphas", save);
    ASSERT_EQ(saved.status, 200) << saved.body.substr(0, 300);
    auto input = arrow::io::ReadableFile::Open(
        (run_dir / "alphas" / "compact_positions" / "positions" / "1.parquet").string()).ValueOrDie();
    std::unique_ptr<parquet::arrow::FileReader> reader;
    ASSERT_TRUE(parquet::arrow::OpenFile(input, arrow::default_memory_pool(), &reader).ok());
    std::shared_ptr<arrow::Table> table;
    ASSERT_TRUE(reader->ReadTable(&table).ok());
    const auto flat = table->CombineChunksToBatch().ValueOrDie();
    const auto& dates = static_cast<const arrow::Date32Array&>(*flat->GetColumnByName("date"));
    const auto& symbols = static_cast<const arrow::StringArray&>(*flat->GetColumnByName("tradingItemId"));
    const auto& weights = static_cast<const arrow::DoubleArray&>(*flat->GetColumnByName("weight"));
    const int32_t before_end = 18659, after_end = 18732;  // 2021-02-01, 2021-04-15
    std::map<std::string, double> early, late;
    for (int64_t i = 0; i < flat->num_rows(); ++i) {
        EXPECT_NE(symbols.GetString(i), "3");
        EXPECT_NE(symbols.GetString(i), "4");
        EXPECT_NE(symbols.GetString(i), "6");
        if (dates.Value(i) == before_end) early[symbols.GetString(i)] = weights.Value(i);
        if (dates.Value(i) == after_end) late[symbols.GetString(i)] = weights.Value(i);
    }
    ASSERT_EQ(early.size(), 3u);
    EXPECT_NEAR(early.at("1"), 1.0 / 6.0, 1e-6);
    EXPECT_NEAR(early.at("2"), 2.0 / 6.0, 1e-6);
    EXPECT_NEAR(early.at("5"), 3.0 / 6.0, 1e-6);
    ASSERT_EQ(late.size(), 2u);
    EXPECT_NEAR(late.at("1"), 1.0 / 3.0, 1e-6);
    EXPECT_NEAR(late.at("2"), 2.0 / 3.0, 1e-6);
    server.stop();
}

TEST_F(HttpContractTest, ExecutionPriceIsValidatedAndScopedToEachRequest) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["simStart"] = "2024-01-01";
    request["isStart"] = "2024-01-01";
    request["osStart"] = "2025-01-01";
    request["signalPlotMaxCells"] = 1000;
    auto call = [&](const char* price) {
        request["executionPrice"] = price;
        const auto response = simulate(request);
        EXPECT_EQ(response.status, 200) << response.body;
        Json::Value body;
        EXPECT_TRUE(parse_json(response.body, &body));
        EXPECT_EQ(body["context"]["executionPrice"].asString(), price);
        return body;
    };
    const auto close = call("close");
    const auto ohlc4 = call("ohlc4");
    const auto vwap = call("vwap");
    const auto close_again = call("close");
    EXPECT_EQ(close["alphas"][0]["performance"], close_again["alphas"][0]["performance"]);
    EXPECT_EQ(close["alphas"][0]["performance"]["turnover"], ohlc4["alphas"][0]["performance"]["turnover"]);
    EXPECT_NE(close["alphas"][0]["performance"]["cumulativeNet"], ohlc4["alphas"][0]["performance"]["cumulativeNet"]);
    EXPECT_EQ(close["alphas"][0]["performance"]["turnover"], vwap["alphas"][0]["performance"]["turnover"]);
    EXPECT_NE(close["alphas"][0]["performance"]["cumulativeNet"], vwap["alphas"][0]["performance"]["cumulativeNet"]);
    for (const auto& value : {Json::Value("typo"), Json::Value("VWAP"), Json::Value(1), Json::Value(false), Json::Value()}) {
        request["executionPrice"] = value;
        EXPECT_EQ(simulate(request).status, 400);
    }
    Json::Value cfg;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/config").body, &cfg));
    EXPECT_EQ(cfg["executionPrice"].asString(), "close");
    ASSERT_EQ(cfg["executionPrices"].size(), 3u);
    EXPECT_EQ(cfg["executionPrices"][0].asString(), "close");
    EXPECT_EQ(cfg["executionPrices"][1].asString(), "ohlc4");
    EXPECT_EQ(cfg["executionPrices"][2].asString(), "vwap");
}

TEST_F(HttpContractTest, ExplicitDefaultUniverseWorksWithoutASelectableUniverseList) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto config_response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(config_response.status, 200) << config_response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(config_response.body, &config));
    ASSERT_EQ(config["defaultUniverse"].asString(), "ALL");
    ASSERT_TRUE(config["universes"].empty());

    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/><rank/>";
    request["simStart"] = "2024-01-01";
    request["isStart"] = "2024-01-01";
    request["osStart"] = "2025-01-01";
    request["output"] = "performance";
    const auto implicit_response = simulate(request);
    ASSERT_EQ(implicit_response.status, 200) << implicit_response.body;
    Json::Value implicit_result;
    ASSERT_TRUE(parse_json(implicit_response.body, &implicit_result));
    EXPECT_EQ(implicit_result["context"]["universe"], config["defaultUniverse"]);

    request["universe"] = config["defaultUniverse"];
    const auto explicit_response = simulate(request);
    ASSERT_EQ(explicit_response.status, 200) << explicit_response.body;
    Json::Value explicit_result;
    ASSERT_TRUE(parse_json(explicit_response.body, &explicit_result));
    EXPECT_EQ(explicit_result, implicit_result);

    request["universe"] = "UNKNOWN_UNIVERSE";
    EXPECT_EQ(simulate(request).status, 400);
}

TEST_F(HttpContractTest, ConfigListsMetaDirAndSortedMetaNames) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value cfg;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/config").body, &cfg));
    EXPECT_EQ(fs::weakly_canonical(cfg["metaDir"].asString()),
              fs::weakly_canonical(find_config_path().parent_path() / "meta"));
    std::vector<std::string> names;
    for (const auto& name : cfg["metaNames"]) names.push_back(name.asString());
    EXPECT_EQ(names, (std::vector<std::string>{"broken", "empty", "heavy", "nested", "sample", "ten"}));
}

TEST_F(HttpContractTest, SimEndIsInclusiveAndCapsAllOutputsWithoutChangingSharedData) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value req;
    req["xml"] = "<data expr=\"close\"/>";
    req["simStart"] = "2023-01-02";
    req["isStart"] = "2023-01-02";
    req["osStart"] = "2023-10-01";
    req["executionPrice"] = "close";
    req["signalPlotMaxCells"] = 10000;
    auto call = [&] {
        auto response = simulate(req); EXPECT_EQ(response.status, 200) << response.body;
        Json::Value value; EXPECT_TRUE(parse_json(response.body, &value)); return value;
    };
    const auto full = call();
    req["simEnd"] = "2023-06-30";
    const auto capped = call();
    EXPECT_EQ(capped["context"]["simEnd"].asString(), "2023-06-30");
    const auto& times = capped["alphas"][0]["performance"]["timestamps"];
    ASSERT_GT(times.size(), 0u);
    EXPECT_EQ(times[times.size()-1].asInt64(), config_date_ms("2023-06-30"));
    for (const char* key : {"timestamps", "cumulativeNet", "cumulativeGross", "turnover"})
        for (Json::ArrayIndex i=0; i<times.size(); ++i)
            EXPECT_EQ(capped["alphas"][0]["performance"][key][i], full["alphas"][0]["performance"][key][i]);
    ASSERT_EQ(capped["alphas"][0]["yearly"].size(), 1u);
    EXPECT_EQ(capped["alphas"][0]["yearly"][0]["year"].asInt(), 2023);
    for (const auto& date : capped["alphas"][0]["signals"]["plot"]["timestamps"])
        EXPECT_LE(date.asInt64(), config_date_ms("2023-06-30"));
    EXPECT_DOUBLE_EQ(capped["alphas"][0]["summary"]["os"]["pnl"].asDouble(), 0.0);
    req.removeMember("simEnd");
    EXPECT_EQ(call()["alphas"][0]["performance"], full["alphas"][0]["performance"]);
    for (const auto& end : {Json::Value("bad"), Json::Value("2023-02-30"), Json::Value("2023-01-01"), Json::Value(1), Json::Value()}) {
        req["simEnd"] = end;
        EXPECT_EQ(simulate(req).status, 400);
    }
    Json::Value config;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/config").body, &config));
    EXPECT_GT(config["simEnd"].asString(), "2023-06-30");
}

TEST_F(HttpContractTest, SimEndAlsoCapsCombinedSignalsAndSearchContext) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value req;
    req["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    req["simStart"] = "2022-12-20"; req["simEnd"] = "2023-06-30";
    req["isStart"] = "2023-01-02"; req["osStart"] = "2023-05-01";
    const auto response = simulate(req); ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body; ASSERT_TRUE(parse_json(response.body, &body));
    for (const auto& alpha : body["alphas"])
        for (const auto& time : alpha["performance"]["timestamps"])
            EXPECT_LE(time.asInt64(), config_date_ms("2023-06-30"));
    for (const auto& time : body["combined"]["signals"]["plot"]["timestamps"])
        EXPECT_LE(time.asInt64(), config_date_ms("2023-06-30"));
    req["firstSearch"] = 2; req["stopRound"] = 1;
    const auto search = http_post_json(server_->port(), "/api/ga/stream", req);
    ASSERT_EQ(search.status, 200) << search.body;
    std::istringstream stream(search.body); std::string line; bool started=false, completed=false;
    unsigned candidates=0;
    while (std::getline(stream,line)) {
        if (line.empty()) continue;
        Json::Value event; ASSERT_TRUE(parse_json(line,&event));
        const auto type=event["type"].asString();
        ASSERT_NE(type,"error") << line;
        if(type=="search_start") { started=true; EXPECT_EQ(event["context"]["simEnd"].asString(),"2023-06-30"); }
        if(type=="candidate") { ++candidates; EXPECT_TRUE(event["candidate"]["summary"].isObject()); EXPECT_FALSE(event["candidate"].isMember("performance")); }
        if(type=="search_complete") completed=true;
    }
    EXPECT_TRUE(started); EXPECT_TRUE(completed); EXPECT_EQ(candidates,2u);
}

TEST_F(HttpContractTest, MultiAlphaBatchesContainPerAlphaXml)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3\"/>";
    req["isStart"] = "2021-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("alphas"));
    ASSERT_TRUE(body["alphas"].isArray());
    ASSERT_EQ(body["alphas"].size(), 2u);

    std::set<std::string> xmls;
    bool has_days_2 = false;
    bool has_days_3 = false;
    for (Json::ArrayIndex i = 0; i < body["alphas"].size(); ++i) {
        const Json::Value& b = body["alphas"][i];
        ASSERT_TRUE(b.isMember("xml"));
        const std::string x = b["xml"].asString();
        EXPECT_EQ(x.find('|'), std::string::npos);
        if (x.find("days=\"2\"") != std::string::npos)
            has_days_2 = true;
        if (x.find("days=\"3\"") != std::string::npos)
            has_days_3 = true;
        xmls.insert(x);
    }
    EXPECT_EQ(xmls.size(), 2u);
    EXPECT_TRUE(has_days_2);
    EXPECT_TRUE(has_days_3);
}

TEST_F(HttpContractTest, MultiAlphaDefaultDoesNotIncludeSignalPlot)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3\"/>";
    req["isStart"] = "2021-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("alphas"));
    ASSERT_TRUE(body["alphas"].isArray());
    ASSERT_EQ(body["alphas"].size(), 2u);

    for (Json::ArrayIndex i = 0; i < body["alphas"].size(); ++i) {
        const Json::Value& b = body["alphas"][i];
        EXPECT_FALSE(b["signals"].isMember("plot"));
        EXPECT_FALSE(b["signals"].isMember("range"));
        EXPECT_FALSE(b["signals"].isMember("distribution"));
        EXPECT_FALSE(b["signals"].isMember("logDistribution"));
    }
}



TEST_F(HttpContractTest, MultiAlphaBatchesSortedByIsSharpeDesc)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1|2|3|4\"/><rank/>";
    req["isStart"] = "2021-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("alphas"));
    ASSERT_TRUE(body["alphas"].isArray());
    ASSERT_EQ(body["alphas"].size(), 4u);

    std::set<int> indices;
    double prev = std::numeric_limits<double>::infinity();
    for (Json::ArrayIndex i = 0; i < body["alphas"].size(); ++i) {
        const Json::Value& b = body["alphas"][i];
        ASSERT_TRUE(b.isMember("id"));
        ASSERT_TRUE(b.isMember("summary"));
        ASSERT_TRUE(b["summary"].isMember("is"));
        ASSERT_TRUE(b["summary"]["is"].isMember("sharpe"));

        const int idx = b["id"].asInt();
        indices.insert(idx);

        const double s = b["summary"]["is"]["sharpe"].asDouble();
        ASSERT_TRUE(std::isfinite(s));
        EXPECT_LE(s, prev + 1e-12);
        prev = s;
    }
    EXPECT_EQ(indices.size(), 4u);
}

TEST_F(HttpContractTest, MultiAlphaCombinedDeterministicAcrossRepeatedThreadedCalls)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3|4|5\"/><rank/>";
    req["isStart"] = "2021-01-01";
    req["osStart"] = "2100-01-01";
    req["threads"] = 4;

    std::string baseline_combined;
    std::string baseline_top_level;
    for (int attempt = 0; attempt < 5; ++attempt) {
        req["threads"] = 1 + (attempt % 4);
        const HttpResult res = simulate(req);
        ASSERT_EQ(res.status, 200) << res.body;

        Json::Value body;
        ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
        ASSERT_TRUE(body.isMember("combined"));
        ASSERT_TRUE(body["combined"].isObject());
        EXPECT_FALSE(body.isMember("performance"));
        ASSERT_TRUE(body["combined"]["performance"].isObject());

        const std::string combined_compact = compact_json(body["combined"]);
        const std::string top_level_compact = compact_json(body["combined"]["performance"]);
        if (attempt == 0) {
            baseline_combined = combined_compact;
            baseline_top_level = top_level_compact;
            continue;
        }
        EXPECT_EQ(combined_compact, baseline_combined) << "attempt=" << attempt;
        EXPECT_EQ(top_level_compact, baseline_top_level) << "attempt=" << attempt;
    }
}

TEST_F(HttpContractTest, SingleAlphaIncludesSignalPlotWithValues)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"returns\"/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body["alphas"][0]["signals"].isMember("plot"));
    ASSERT_TRUE(body["alphas"][0]["signals"]["plot"].isObject());

    const Json::Value& signal_plot = body["alphas"][0]["signals"]["plot"];
    ASSERT_TRUE(signal_plot.isMember("values"));
    ASSERT_TRUE(signal_plot.isMember("timestamps"));
    ASSERT_TRUE(signal_plot.isMember("symbols"));
    ASSERT_GT(signal_plot["values"].size(), 0u);
    ASSERT_GT(signal_plot["timestamps"].size(), 0u);
    ASSERT_GT(signal_plot["symbols"].size(), 0u);

    size_t non_null = 0;
    for (Json::ArrayIndex i = 0; i < signal_plot["values"].size(); ++i) {
        const Json::Value& row = signal_plot["values"][i];
        for (Json::ArrayIndex j = 0; j < row.size(); ++j) {
            if (!row[j].isNull())
                ++non_null;
        }
    }
    EXPECT_GT(non_null, 0u);
}

TEST_F(HttpContractTest, EmptyXmlReturns400)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 400) << res.body;
}

TEST_F(HttpContractTest, InvalidExprReturns400)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"INVALID\"/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 400) << res.body;
}


TEST_F(HttpContractTest, MalformedRangeReturns400)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"5.5...20\"/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 400) << res.body;
    EXPECT_NE(res.body.find("Invalid range '5.5...20' (expected integers start...end or start...step...end)"),
              std::string::npos) << res.body;
}

TEST_F(HttpContractTest, SimStartBeginsWithAnUnfundedBookAndFirstEntryFee) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    auto baseline = simulate(request);
    ASSERT_EQ(baseline.status, 200) << baseline.body;
    Json::Value original;
    ASSERT_TRUE(parse_json(baseline.body, &original));
    ASSERT_GT(original["alphas"][0]["performance"]["timestamps"].size(), 3u);
    request["simStart"] = date_string(original["alphas"][0]["performance"]["timestamps"][3].asInt64());
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    EXPECT_EQ(body["alphas"][0]["performance"]["timestamps"][0], config_date_ms(request["simStart"].asString()));
    EXPECT_EQ(body["alphas"][0]["signals"]["plot"]["timestamps"][0], config_date_ms(request["simStart"].asString()));
    const auto config_response = http_get(server_->port(), "/api/config");
    Json::Value config;
    ASSERT_EQ(config_response.status, 200);
    ASSERT_TRUE(parse_json(config_response.body, &config));
    const double fee = config["feeRate"].asDouble();
    EXPECT_NEAR(body["alphas"][0]["performance"]["cumulativeNet"][0].asDouble(), -fee, 1e-12);
    EXPECT_NEAR(body["alphas"][0]["performance"]["cumulativeGross"][0].asDouble(), 0.0, 1e-12);
    // Gross minus net over the whole span is exactly the turnover fee.
    const Json::Value& timeseries = body["alphas"][0]["performance"];
    double fee_sum = 0.0;
    for (const auto& turnover : timeseries["turnover"]) fee_sum += turnover.asDouble() * fee;
    const Json::ArrayIndex last = timeseries["cumulativeNet"].size() - 1;
    EXPECT_NEAR(timeseries["cumulativeGross"][last].asDouble() - timeseries["cumulativeNet"][last].asDouble(),
                fee_sum, 1e-9);
    // Performance metrics are gross: IS spans the whole trading period here.
    ASSERT_GT(fee_sum, 1e-9);
    EXPECT_NEAR(body["alphas"][0]["summary"]["is"]["pnl"].asDouble(),
                timeseries["cumulativeGross"][last].asDouble(), 1e-7);
    EXPECT_GT(std::fabs(body["alphas"][0]["summary"]["is"]["pnl"].asDouble() -
                        timeseries["cumulativeNet"][last].asDouble()), 1e-7);
    double peak = 0.0, gross_mdd = 0.0;
    for (Json::ArrayIndex i = 0; i <= last; ++i) {
        const double cum = timeseries["cumulativeGross"][i].asDouble();
        peak = std::max(peak, cum);
        gross_mdd = std::min(gross_mdd, cum - peak);
    }
    EXPECT_NEAR(body["alphas"][0]["summary"]["is"]["maxDrawdown"].asDouble(), gross_mdd, 1e-7);
}

TEST_F(HttpContractTest, SingleSignalPlotHonorsCellBudgetAndRejectsInvalidStart) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    request["signalPlotMaxCells"] = 8;
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    const auto& plot = body["alphas"][0]["signals"]["plot"];
    EXPECT_LE(plot["values"].size() * plot["symbols"].size(), 8u);
    EXPECT_EQ(plot["timestamps"].size(), plot["values"].size());
    EXPECT_TRUE(plot.isMember("rowStride"));
    EXPECT_TRUE(plot.isMember("originalRows"));
    ASSERT_TRUE(body["alphas"][0]["signals"]["logDistribution"].isObject());
    const auto& log_histogram = body["alphas"][0]["signals"]["logDistribution"];
    ASSERT_TRUE(log_histogram["counts"].isArray());
    ASSERT_GT(log_histogram["counts"].size(), 0u);
    EXPECT_EQ(log_histogram["counts"].size(), log_histogram["bins"].size());
    ASSERT_EQ(log_histogram["edges"].size(), log_histogram["counts"].size() + 1);
    EXPECT_EQ(log_histogram["scale"].asString(), "log");
    Json::UInt64 linear_count = 0, log_count = 0;
    for (const auto& count : body["alphas"][0]["signals"]["distribution"]["counts"]) linear_count += count.asUInt64();
    for (const auto& count : log_histogram["counts"]) log_count += count.asUInt64();
    EXPECT_EQ(log_count, linear_count);
    EXPECT_EQ(log_histogram["totalCount"].asUInt64(), log_count);
    // The histogram pools full signals, not the eight-cell display sample.
    EXPECT_GT(log_count, 8u);
    EXPECT_EQ(log_histogram["min"], body["alphas"][0]["signals"]["distribution"]["min"]);
    EXPECT_EQ(log_histogram["max"], body["alphas"][0]["signals"]["distribution"]["max"]);
    EXPECT_EQ(log_histogram["edges"][0], log_histogram["min"]);
    EXPECT_EQ(log_histogram["edges"][log_histogram["edges"].size() - 1], log_histogram["max"]);
    request["simStart"] = "not-a-date";
    EXPECT_EQ(simulate(request).status, 400);
    request["simStart"] = "2200-01-01";
    EXPECT_EQ(simulate(request).status, 400);
}

TEST_F(HttpContractTest, ResultPayloadsHaveOneCanonicalOwner) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3|4\"/>";
    request["isStart"]="2021-01-01";request["osStart"]="2100-01-01";
    request["signalPlotMaxCells"]=12;
    const auto response=simulate(request);ASSERT_EQ(response.status,200)<<response.body;
    Json::Value body;ASSERT_TRUE(parse_json(response.body,&body));
    ASSERT_EQ(body["alphas"].size(),3u);
    ASSERT_TRUE(body["combined"]["signals"]["plot"].isObject());
    for(const char* key:{"summary","yearly","performance","signals","signalPlot","timeseries"}) EXPECT_FALSE(body.isMember(key))<<key;
    for(const auto& alpha:body["alphas"]) { EXPECT_TRUE(alpha["performance"].isObject()); EXPECT_FALSE(alpha.isMember("signals")); }
    const auto& plot=body["combined"]["signals"]["plot"];
    EXPECT_LE(plot["values"].size()*plot["symbols"].size(),12u);
}




TEST_F(HttpContractTest, XmlDelayOwnsTimingWithoutAnotherShift) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2100-01-01";
    auto call = [&](const Json::Value& payload) {
        const auto response = simulate(payload);
        EXPECT_EQ(response.status, 200) << response.body;
        Json::Value body;
        EXPECT_TRUE(parse_json(response.body, &body));
        return body;
    };
    const auto initial = call(request);
    ASSERT_GT(initial["alphas"][0]["performance"]["timestamps"].size(), 3u);
    request["simStart"] = date_string(initial["alphas"][0]["performance"]["timestamps"][3].asInt64());
    const auto delayed = call(request);
    double gross = 0.0, maximum = 0.0;
    for (const auto& value : delayed["alphas"][0]["signals"]["plot"]["values"][0]) {
        if (value.isNull()) continue;
        const double absolute = std::fabs(value.asDouble());
        gross += absolute;
        maximum = std::max(maximum, absolute);
    }
    EXPECT_NEAR(delayed["alphas"][0]["performance"]["maxWeight"][0].asDouble(), maximum / gross, 1e-8);
}


TEST_F(HttpContractTest, ConfigPublishesMissingClosePolicy) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(response.body, &config)) << response.body;
    ASSERT_TRUE(config.isMember("missingClosePolicy"));
    const std::string policy = config["missingClosePolicy"].asString();
    EXPECT_TRUE(policy == "carry" || policy == "error") << policy;
}

TEST_F(HttpContractTest, SimulateReportsMissingQuoteTotalsAndFrozenSeries) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body)) << response.body;
    EXPECT_FALSE(body.isMember("missingData"));
    ASSERT_TRUE(body["alphas"][0].isMember("missingData"));
    const Json::Value& totals = body["alphas"][0]["missingData"];
    for (const char* key : {"missingHeldBars", "blockedOrders", "blockedTradeWeight",
                            "missingEntries", "resumedHoldings", "terminalFrozenGross", "frozenGrossSum"}) {
        ASSERT_TRUE(totals.isMember(key)) << key;
        EXPECT_TRUE(totals[key].isNumeric()) << key;
    }
    EXPECT_LE(totals["missingEntries"].asUInt64(), totals["blockedOrders"].asUInt64());
    const Json::Value& timeseries = body["alphas"][0]["performance"];
    ASSERT_TRUE(timeseries.isMember("frozenGross"));
    ASSERT_EQ(timeseries["frozenGross"].size(), timeseries["timestamps"].size());
    double frozen_sum = 0.0;
    for (const auto& value : timeseries["frozenGross"]) {
        EXPECT_GE(value.asDouble(), 0.0);
        frozen_sum += value.asDouble();
    }
    EXPECT_NEAR(frozen_sum, totals["frozenGrossSum"].asDouble(), 1e-9);
    ASSERT_FALSE(timeseries["frozenGross"].empty());
    EXPECT_NEAR(timeseries["frozenGross"][timeseries["frozenGross"].size() - 1].asDouble(),
                totals["terminalFrozenGross"].asDouble(), 1e-12);
}

TEST_F(HttpContractTest, CombinedAndGaShareMissingClosePolicy) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    request["missingClose"] = "carry";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body)) << response.body;
    ASSERT_TRUE(body["combined"].isMember("missingData"));
    for (Json::ArrayIndex i = 0; i < body["alphas"].size(); ++i)
        EXPECT_TRUE(body["alphas"][i].isMember("missingData"));

    const Json::Value& totals = body["combined"]["missingData"];
    const bool quiet = totals["missingHeldBars"].asUInt64() == 0 &&
                       totals["blockedOrders"].asUInt64() == 0 &&
                       totals["missingEntries"].asUInt64() == 0;
    request["missingClose"] = "error";
    const auto strict = simulate(request);
    if (quiet) {
        ASSERT_EQ(strict.status, 200) << strict.body;
        Json::Value strict_body;
        ASSERT_TRUE(parse_json(strict.body, &strict_body));
        EXPECT_EQ(compact_json(strict_body["combined"]["performance"]),
                  compact_json(body["combined"]["performance"]));
    } else {
        EXPECT_TRUE(strict.status == 200 || strict.status == 400) << strict.body;
    }

    Json::Value ga = request;
    ga["missingClose"] = "carry"; ga["firstSearch"] = 2; ga["stopRound"] = 1;
    const auto response_ga = http_post_json(server_->port(), "/api/ga/stream", ga);
    ASSERT_EQ(response_ga.status, 200) << response_ga.body;
    std::istringstream lines(response_ga.body); std::string line; unsigned candidates=0; bool complete=false;
    while(std::getline(lines,line)) {
        if(line.empty()) continue;
        Json::Value event; ASSERT_TRUE(parse_json(line,&event));
        ASSERT_NE(event["type"].asString(),"error") << line;
        if(event["type"].asString()=="candidate") { ++candidates; EXPECT_TRUE(event["candidate"]["missingData"].isObject()); }
        if(event["type"].asString()=="search_complete") complete=true;
    }
    EXPECT_EQ(candidates,2u); EXPECT_TRUE(complete);
}

TEST_F(HttpContractTest, InvalidMissingClosePolicyIsRejected) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    Json::Value bad = request;
    bad["missingClose"] = "forward-fill";
    EXPECT_EQ(simulate(bad).status, 400);
    for (const char* key : {"threads", "signalPlotMaxCells"}) {
        bad = request;
        bad[key] = static_cast<Json::Int64>(5000000000LL);
        const auto response = simulate(bad);
        EXPECT_EQ(response.status, 400) << key;
        EXPECT_NE(response.body.find(key), std::string::npos) << response.body;
        EXPECT_EQ(response.body.find("LargestInt"), std::string::npos) << response.body;
    }
}



// `base:` publishes the two group panels as ordinary panel columns, which is what
// <neutralize group="..."/> compiles against (docs/data_contract.md 4.4 f).
TEST_F(HttpContractTest, ConfigPublishesTheGroupColumnsFromTheBaseBlock) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(response.body, &config)) << response.body;
    ASSERT_TRUE(config["columns"].isArray()) << response.body;
    std::set<std::string> columns;
    for (const Json::Value& column : config["columns"])
        columns.insert(column.asString());
    for (const char* field : {"open", "high", "low", "close", "volume", "vwap", "cap",
                              "sector", "industry"})
        EXPECT_EQ(columns.count(field), 1u) << field << " missing from " << response.body;
}

TEST_F(HttpContractTest, NeutralizeRunsPerGroupAndExpandsThePipe) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/><neutralize group=\"sector\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    const auto single = simulate(request);
    ASSERT_EQ(single.status, 200) << single.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(single.body, &body)) << single.body;
    ASSERT_EQ(body["alphas"].size(), 1u);
    EXPECT_NE(body["alphas"][0]["xml"].asString().find("group=\"sector\""), std::string::npos);

    request["xml"] = "<data expr=\"close\"/><rank/><neutralize group=\"sector|industry\"/>";
    const auto piped = simulate(request);
    ASSERT_EQ(piped.status, 200) << piped.body;
    Json::Value expanded;
    ASSERT_TRUE(parse_json(piped.body, &expanded)) << piped.body;
    ASSERT_EQ(expanded["alphas"].size(), 2u);
    std::set<std::string> groups;
    for (const Json::Value& batch : expanded["alphas"]) {
        const std::string xml = batch["xml"].asString();
        EXPECT_EQ(xml.find('|'), std::string::npos) << xml;
        if (xml.find("group=\"sector\"") != std::string::npos) groups.insert("sector");
        if (xml.find("group=\"industry\"") != std::string::npos) groups.insert("industry");
    }
    EXPECT_EQ(groups, (std::set<std::string>{"sector", "industry"}));
}

TEST_F(HttpContractTest, UnsupportedNeutralizeGroupReturns400WithTheSupportedValues) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/><neutralize group=\"country\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 400) << response.body;
    EXPECT_NE(response.body.find("country"), std::string::npos) << response.body;
    EXPECT_NE(response.body.find("sector"), std::string::npos) << response.body;
    EXPECT_NE(response.body.find("industry"), std::string::npos) << response.body;
}

TEST_F(HttpContractTest, GaStreamsEveryUniqueCandidateBetweenRoundBoundaries) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"1...48\"/>";
    request["isStart"] = "2021-01-01"; request["osStart"] = "2100-01-01";
    request["firstSearch"] = 200; request["stopRound"] = 40; request["stopAlphas"] = 400;
    request["threads"] = 20;
    for (int scenario = 0; scenario < 2; ++scenario) {
        if (scenario) { request["firstSearch"] = 4; request["roundSize"] = 3; request["stopAlphas"] = 10; }
        const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
        ASSERT_EQ(response.status,200) << response.body;
        std::istringstream stream(response.body); std::string line;
        std::set<unsigned> started, ended;
        std::set<std::string> candidates;
        Json::Value complete;
        while (std::getline(stream,line)) {
            if (line.empty()) continue;
            Json::Value event; ASSERT_TRUE(parse_json(line,&event)) << line;
            const auto type = event["type"].asString();
            ASSERT_NE(type,"error") << line;
            if (type == "round_start") started.insert(event["round"].asUInt());
            if (type == "candidate") {
                const auto& candidate = event["candidate"];
                const unsigned round = candidate["round"].asUInt();
                EXPECT_EQ(started.count(round),1u); EXPECT_EQ(ended.count(round),0u);
                EXPECT_TRUE(candidates.insert(candidate["xml"].asString()).second);
                EXPECT_EQ(candidate["status"].asString(),"completed") << line;
                EXPECT_TRUE(candidate["summary"].isObject());
                EXPECT_FALSE(candidate.isMember("performance")); EXPECT_FALSE(candidate["signals"].isMember("plot"));
            }
            if (type == "round_end") ended.insert(event["round"].asUInt());
            if (type == "search_complete") complete = event;
        }
        EXPECT_EQ(candidates.size(),scenario ? 10u : 48u);
        EXPECT_EQ(complete["evaluated"].asUInt(),scenario ? 10u : 48u);
        EXPECT_EQ(complete["stopReason"].asString(),scenario ? "stop_alphas" : "exhausted");
        EXPECT_EQ(started.size(),scenario ? 3u : 1u); EXPECT_EQ(started,ended);
    }
}

TEST_F(HttpContractTest, OutputScopesOmitUnrelatedAccountingAndKeepSelectedRawSignals) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"]="<data expr=\"close\"/><rank/>";
    request["isStart"]="2000-01-01";request["osStart"]="2100-01-01";
    request["output"]="full";
    const auto full=simulate(request);ASSERT_EQ(full.status,200)<<full.body;
    Json::Value baseline;ASSERT_TRUE(parse_json(full.body,&baseline));
    const auto& alpha=baseline["alphas"][0];
    ASSERT_TRUE(alpha["signals"]["plot"].isObject());
    request["output"]="performance";
    const auto lean=simulate(request);ASSERT_EQ(lean.status,200)<<lean.body;
    Json::Value details;ASSERT_TRUE(parse_json(lean.body,&details));
    EXPECT_EQ(details["alphas"][0]["performance"],alpha["performance"]);
    EXPECT_EQ(details["alphas"][0]["summary"],alpha["summary"]);
    EXPECT_FALSE(details["alphas"][0].isMember("signals"));
    request["output"]="signals";
    const auto signals_response=simulate(request);ASSERT_EQ(signals_response.status,200)<<signals_response.body;
    Json::Value signals;ASSERT_TRUE(parse_json(signals_response.body,&signals));
    EXPECT_EQ(signals["alphas"][0]["signals"],alpha["signals"]);
    for(const char* key:{"performance","summary","yearly","missingData","poolCorrelation"}) EXPECT_FALSE(signals["alphas"][0].isMember(key))<<key;
    request["output"]="signalSeries";
    EXPECT_EQ(simulate(request).status,400);
    request["signalPlotSymbols"].append(alpha["signals"]["plot"]["symbols"][0]);
    const auto series_response=simulate(request);ASSERT_EQ(series_response.status,200)<<series_response.body;
    Json::Value series;ASSERT_TRUE(parse_json(series_response.body,&series));
    const auto& series_alpha=series["alphas"][0];
    const auto& plot=series_alpha["signals"]["plot"];
    ASSERT_EQ(plot["symbols"].size(),1u);
    ASSERT_EQ(plot["values"].size(),alpha["signals"]["plot"]["values"].size());
    for(Json::ArrayIndex row=0;row<plot["values"].size();++row) EXPECT_EQ(plot["values"][row][0],alpha["signals"]["plot"]["values"][row][0]);
    for(const char* key:{"performance","summary","yearly","missingData","poolCorrelation"}) EXPECT_FALSE(series_alpha.isMember(key))<<key;
    EXPECT_FALSE(series_alpha["signals"].isMember("distribution"));
    EXPECT_FALSE(series_alpha["signals"].isMember("logDistribution"));
    request["output"]="unknown"; EXPECT_EQ(simulate(request).status,400);
}

TEST_F(HttpContractTest, ConcreteCandidateAggregateMatchesOrdinaryCombinedPortfolio) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"0\"/><tsmean days=\"2|3|4\"/><rank/>";
    request["simStart"] = "2023-02-01"; request["simEnd"] = "2023-09-29";
    request["isStart"] = "2023-03-01"; request["osStart"] = "2023-06-01";
    request["output"] = "performance";
    request["threads"] = 1;
    const auto baseline = simulate(request); ASSERT_EQ(baseline.status, 200) << baseline.body;
    Json::Value normal; ASSERT_TRUE(parse_json(baseline.body, &normal));
    // Sort by original expansion index, independent of performance order.
    std::vector<std::pair<int, std::string>> entries;
    for (const auto& entry : normal["alphas"]) entries.emplace_back(entry["id"].asInt(), entry["xml"].asString());
    std::sort(entries.begin(), entries.end());
    request["xmls"] = Json::Value(Json::arrayValue);
    for (const auto& entry : entries) request["xmls"].append(entry.second);
    request["combinedOnly"] = true; request["threads"] = 20;
    const auto combined_response = simulate(request); ASSERT_EQ(combined_response.status, 200) << combined_response.body;
    Json::Value combined; ASSERT_TRUE(parse_json(combined_response.body, &combined));
    EXPECT_TRUE(combined["alphas"].empty());
    EXPECT_EQ(combined["combined"], normal["combined"]);
    EXPECT_EQ(combined["combined"]["performance"], normal["combined"]["performance"]);
    EXPECT_EQ(combined["combined"]["yearly"], normal["combined"]["yearly"]);
    request["xmls"] = Json::Value(Json::arrayValue);
    EXPECT_EQ(simulate(request).status, 400);
    request["xmls"].append(request["xml"]); // An expanded template is not one concrete candidate.
    EXPECT_EQ(simulate(request).status, 400);
    request["xmls"][0] = 42;
    EXPECT_EQ(simulate(request).status, 400);
}

namespace {
std::string read_text(const fs::path& file)
{
    std::ifstream in(file, std::ios::binary);
    std::ostringstream buffer;
    buffer << in.rdbuf();
    return buffer.str();
}

std::string unique_alpha_name(const std::string& stem)
{
    return "wpe_" + stem + "_" + std::to_string(::getpid());
}

Json::Value alpha_metadata(const std::string& execution = "close")
{
    Json::Value metadata;
    metadata["simStart"] = "2024-01-01";
    metadata["simEnd"] = "2024-12-31";
    metadata["isStart"] = "2024-01-01";
    metadata["osStart"] = "2024-07-01";
    metadata["universe"] = "";
    metadata["executionPrice"] = execution;
    metadata["threads"] = 7;
    return metadata;
}

Json::Value with_metadata(Json::Value request, const Json::Value& metadata)
{
    for (const auto& key : metadata.getMemberNames()) request[key] = metadata[key];
    return request;
}

std::vector<fs::path> hidden_entries(const fs::path& dir)
{
    std::vector<fs::path> hidden;
    for (const auto& entry : fs::directory_iterator(dir))
        if (entry.path().filename().string().front() == '.') hidden.push_back(entry.path());
    return hidden;
}

// Same series as tests/make_http_test_fixture.py (float32 storage, day index from 2020-01-01).
double fixture_close(int symbol, long day)
{
    const double t = static_cast<double>(day);
    return static_cast<float>(100 + 15 * symbol + 0.01 * t + 2 * std::sin(0.09 * t + 0.4 * symbol));
}
} // namespace

class AlphaStorageHttpTest : public HttpContractTest {
protected:
    fs::path alphas_dir()
    {
        const auto response = http_get(server_->port(), "/api/config");
        Json::Value config;
        if (response.status != 200 || !parse_json(response.body, &config)) return {};
        return config["alphasDir"].asString();
    }
    HttpResult save(const Json::Value& request)
    {
        return http_post_json(server_->port(), "/api/alphas", request);
    }
    void remove(const std::string& name)
    {
        http_delete(server_->port(), "/api/alphas/" + name);
    }
};

TEST_F(AlphaStorageHttpTest, ConfigPublishesAnExistingAbsoluteAlphasDir) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const fs::path dir = alphas_dir();
    ASSERT_FALSE(dir.empty());
    EXPECT_TRUE(dir.is_absolute());
    EXPECT_TRUE(fs::is_directory(dir));
    EXPECT_EQ(fs::weakly_canonical(dir), fs::weakly_canonical(run_dir_ / "alphas"));
}

TEST_F(AlphaStorageHttpTest, XmlLevelStoresFormattedXmlAndToolbarMetadataOnly) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("xml");
    remove(name);
    const std::string xml = "<!-- keep me -->\r\n<data expr=\"close\" delay=\"1\"/>\n\t<tsmean days=\"5|10\"/>  \n"
                            "<choice><rank/><zscore/></choice>\n\xec\x95\x8c\xed\x8c\x8c\n";
    const std::string formatted = "<!-- keep me -->\n<data expr=\"close\" delay=\"1\" />\n"
                                  "<tsmean days=\"5|10\" />\n"
                                  "<choice>\n  <rank />\n  <zscore />\n</choice>\n"
                                  "\xec\x95\x8c\xed\x8c\x8c\n";
    Json::Value request;
    request["name"] = name;
    request["xml"] = xml;
    request["level"] = "xml";
    request["metadata"] = alpha_metadata("vwap");
    request["metadata"]["universe"] = "";
    request["fee"] = 99;  // unknown keys are not stored
    const auto saved = save(request);
    ASSERT_EQ(saved.status, 200) << saved.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(saved.body, &body));
    EXPECT_EQ(body["name"].asString(), name);
    EXPECT_TRUE(body["saved"].asBool());
    EXPECT_EQ(body["level"].asString(), "xml");
    const fs::path dir = alphas_dir() / name;
    EXPECT_EQ(fs::weakly_canonical(body["path"].asString()), fs::weakly_canonical(dir));
    EXPECT_EQ(read_text(dir / "alpha.xml"), formatted);
    EXPECT_EQ(read_text(dir / "metadata.yml"),
              "level: xml\nsimStart: 2024-01-01\nsimEnd: 2024-12-31\nisStart: 2024-01-01\n"
              "osStart: 2024-07-01\nuniverse: \"\"\nexecutionPrice: vwap\nthreads: 7\n");
    std::set<std::string> files;
    for (const auto& entry : fs::directory_iterator(dir)) files.insert(entry.path().filename().string());
    EXPECT_EQ(files, (std::set<std::string>{"alpha.xml", "metadata.yml"}));

    const auto listed = http_get(server_->port(), "/api/alphas");
    ASSERT_EQ(listed.status, 200);
    Json::Value list;
    ASSERT_TRUE(parse_json(listed.body, &list));
    bool found = false;
    for (const auto& item : list) {
        EXPECT_NE(item["name"].asString().front(), '.');
        if (item["name"].asString() != name) continue;
        found = true;
        EXPECT_EQ(item["xml"].asString(), formatted);
        EXPECT_EQ(item["level"].asString(), "xml");
        EXPECT_EQ(item["metadata"], alpha_metadata("vwap"));
    }
    EXPECT_TRUE(found);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results").status, 404);

    request["xml"] = "<data expr=\"close\"/>";
    const auto duplicate = save(request);
    EXPECT_EQ(duplicate.status, 409) << duplicate.body;
    EXPECT_EQ(read_text(dir / "alpha.xml"), formatted);

    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
    EXPECT_FALSE(fs::exists(dir));
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 404);
    EXPECT_TRUE(hidden_entries(alphas_dir()).empty());
}

TEST_F(AlphaStorageHttpTest, InvalidNamesMetadataAndRowsAreRejectedWithoutFiles) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("invalid");
    remove(name);
    Json::Value valid;
    valid["name"] = name;
    valid["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    valid["level"] = "results";
    valid["metadata"] = alpha_metadata();
    valid["rows"] = Json::Value(Json::arrayValue);
    Json::Value row;
    row["id"] = 1; row["round"] = 1; row["xml"] = "<data expr=\"close\"/><tsmean days=\"2\"/>";
    valid["rows"].append(row);

    std::vector<std::pair<std::string, Json::Value>> cases;
    auto variant = [&](const std::string& label, auto edit) {
        Json::Value request = valid;
        edit(request);
        cases.emplace_back(label, request);
    };
    for (const char* bad : {"", ".hidden", "..", "a/b", "../escape", "a b"})
        variant(std::string("name ") + bad, [&](Json::Value& r) { r["name"] = bad; });
    variant("no xml", [](Json::Value& r) { r.removeMember("xml"); });
    variant("malformed xml", [](Json::Value& r) { r["level"] = "xml"; r["xml"] = "<choice><rank/></wrong>"; });
    variant("bad level", [](Json::Value& r) { r["level"] = "signals"; });
    variant("no metadata", [](Json::Value& r) { r.removeMember("metadata"); });
    variant("missing threads", [](Json::Value& r) { r["metadata"].removeMember("threads"); });
    variant("bad threads", [](Json::Value& r) { r["metadata"]["threads"] = 0; });
    variant("missing osStart", [](Json::Value& r) { r["metadata"].removeMember("osStart"); });
    variant("empty simEnd", [](Json::Value& r) { r["metadata"]["simEnd"] = ""; });
    variant("bad date", [](Json::Value& r) { r["metadata"]["simStart"] = "2024-02-30"; });
    variant("is after os", [](Json::Value& r) { r["metadata"]["isStart"] = "2025-01-01"; });
    variant("bad execution", [](Json::Value& r) { r["metadata"]["executionPrice"] = "open"; });
    variant("unknown universe", [](Json::Value& r) { r["metadata"]["universe"] = "NOPE"; });
    variant("no rows", [](Json::Value& r) { r.removeMember("rows"); });
    variant("empty rows", [](Json::Value& r) { r["rows"] = Json::Value(Json::arrayValue); });
    variant("negative id", [](Json::Value& r) { r["rows"][0]["id"] = -1; });
    variant("fractional id", [](Json::Value& r) { r["rows"][0]["id"] = 1.5; });
    variant("duplicate id", [](Json::Value& r) { r["rows"].append(r["rows"][0]); });
    variant("template row", [](Json::Value& r) { r["rows"][0]["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>"; });
    variant("broken row", [](Json::Value& r) { r["rows"][0]["xml"] = "<data expr=\"INVALID\"/>"; });
    variant("xml level needs metadata", [](Json::Value& r) { r["level"] = "xml"; r.removeMember("metadata"); });
    for (const auto& [label, request] : cases) {
        const auto response = save(request);
        EXPECT_EQ(response.status, 400) << label << ": " << response.body;
    }
    EXPECT_FALSE(fs::exists(alphas_dir() / name));
    EXPECT_TRUE(hidden_entries(alphas_dir()).empty());
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/..%2F..%2Fetc/results").status / 100, 4);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results/rows/abc").status, 400);
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/.hidden").status, 400);
}

TEST_F(AlphaStorageHttpTest, ResultFilesEqualLiveSimulateBodies) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("results");
    remove(name);
    Json::Value metadata = alpha_metadata("ohlc4");
    metadata["isStart"] = "2024-04-01";  // the members need warmup
    Json::Value request;
    request["name"] = name;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3|4\"/><rank/>";
    request["level"] = "results";
    request["metadata"] = metadata;
    request["rows"] = Json::Value(Json::arrayValue);
    const std::vector<std::pair<int, std::string>> rows = {
        {17, "<data expr=\"close\" delay=\"1\"/><tsmean days=\"4\"/><rank/>"},
        {3, "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2\"/><rank/>"},
        {9, "<data expr=\"close\" delay=\"1\"/><tsmean days=\"3\"/><rank/>"}};
    for (const auto& [id, xml] : rows) {
        Json::Value row;
        row["id"] = id; row["round"] = id == 17 ? 1 : 2; row["xml"] = xml;
        request["rows"].append(row);
    }
    const auto saved = save(request);
    ASSERT_EQ(saved.status, 200) << saved.body;
    const fs::path dir = alphas_dir() / name;
    EXPECT_FALSE(fs::exists(dir / "positions"));

    const auto index_response = http_get(server_->port(), "/api/alphas/" + name + "/results");
    ASSERT_EQ(index_response.status, 200) << index_response.body;
    EXPECT_EQ(index_response.body, read_text(dir / "results" / "index.json"));
    Json::Value index;
    ASSERT_TRUE(parse_json(index_response.body, &index));
    EXPECT_TRUE(index["hasBatch"].asBool());
    EXPECT_EQ(index["context"]["threads"].asUInt(), 7u);
    ASSERT_EQ(index["rows"].size(), rows.size());
    Json::Value batch_request;
    batch_request["xmls"] = Json::Value(Json::arrayValue);
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto& [id, xml] = rows[i];
        Json::Value live_request;
        live_request["xml"] = xml;
        live_request["output"] = "performance";
        const auto live = simulate(with_metadata(live_request, metadata));
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value expected, stored;
        ASSERT_TRUE(parse_json(live.body, &expected));
        const auto file = http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(id));
        ASSERT_EQ(file.status, 200) << file.body;
        EXPECT_EQ(file.body, read_text(dir / "results" / "rows" / (std::to_string(id) + ".json")));
        ASSERT_TRUE(parse_json(file.body, &stored));
        EXPECT_EQ(stored["context"]["threads"].asUInt(), 7u);
        expected["alphas"][0]["id"] = id;
        expected["alphas"][0]["round"] = id == 17 ? 1 : 2;
        expected["alphas"][0]["origin"] = "saved";
        EXPECT_EQ(stored, expected) << "row " << id;
        const auto& entry = index["rows"][static_cast<Json::ArrayIndex>(i)];
        EXPECT_EQ(entry["id"].asInt(), id);
        EXPECT_EQ(entry["round"].asInt(), id == 17 ? 1 : 2);
        EXPECT_EQ(entry["xml"].asString(), xml);
        EXPECT_EQ(entry["origin"].asString(), "saved");
        EXPECT_EQ(entry["status"].asString(), "completed");
        EXPECT_EQ(entry["summary"], expected["alphas"][0]["summary"]);
        EXPECT_EQ(entry["yearly"], expected["alphas"][0]["yearly"]);
        batch_request["xmls"].append(xml);
    }
    batch_request["combinedOnly"] = true;
    batch_request["output"] = "performance";
    const auto live_batch = simulate(with_metadata(batch_request, metadata));
    ASSERT_EQ(live_batch.status, 200) << live_batch.body;
    const auto stored_batch = http_get(server_->port(), "/api/alphas/" + name + "/results/batch");
    ASSERT_EQ(stored_batch.status, 200) << stored_batch.body;
    Json::Value expected_batch, batch;
    ASSERT_TRUE(parse_json(live_batch.body, &expected_batch));
    ASSERT_TRUE(parse_json(stored_batch.body, &batch));
    EXPECT_EQ(batch["context"]["threads"].asUInt(), 7u);
    EXPECT_EQ(batch, expected_batch);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results/rows/4").status, 404);

    // One row: no batch file.
    const std::string single = unique_alpha_name("single");
    remove(single);
    request["name"] = single;
    request["rows"].resize(1);
    ASSERT_EQ(save(request).status, 200);
    Json::Value single_index;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/alphas/" + single + "/results").body, &single_index));
    EXPECT_FALSE(single_index["hasBatch"].asBool());
    EXPECT_FALSE(fs::exists(alphas_dir() / single / "results" / "batch.json"));
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + single + "/results/batch").status, 404);

    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + single).status, 200);
    EXPECT_FALSE(fs::exists(dir));
    EXPECT_TRUE(hidden_entries(alphas_dir()).empty());
}

TEST_F(AlphaStorageHttpTest, PositionsAreExecutedTargetsThatReproduceGrossPnl) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("positions");
    remove(name);
    Json::Value metadata = alpha_metadata("close");
    metadata["isStart"] = "2024-01-04";  // the members need warmup
    const std::string xml = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"3\"/><rank/><neutralize group=\"sector\"/>";
    Json::Value request;
    request["name"] = name;
    request["xml"] = xml;
    request["level"] = "positions";
    request["metadata"] = metadata;
    Json::Value row;
    row["id"] = 5; row["round"] = 1; row["xml"] = xml;
    request["rows"].append(row);
    const auto saved = save(request);
    ASSERT_EQ(saved.status, 200) << saved.body;
    const fs::path dir = alphas_dir() / name;

    auto input = arrow::io::ReadableFile::Open((dir / "positions" / "5.parquet").string()).ValueOrDie();
    std::unique_ptr<parquet::arrow::FileReader> reader;
    ASSERT_TRUE(parquet::arrow::OpenFile(input, arrow::default_memory_pool(), &reader).ok());
    std::shared_ptr<arrow::Table> table;
    ASSERT_TRUE(reader->ReadTable(&table).ok());
    table = table->CombineChunks().ValueOrDie();
    ASSERT_EQ(table->num_columns(), 3);
    ASSERT_GT(table->num_rows(), 0);
    EXPECT_TRUE(table->schema()->field(0)->type()->Equals(arrow::date32()));
    EXPECT_TRUE(table->schema()->field(1)->type()->Equals(arrow::utf8()));
    EXPECT_TRUE(table->schema()->field(2)->type()->Equals(arrow::float64()));
    auto dates = std::static_pointer_cast<arrow::Date32Array>(table->column(0)->chunk(0));
    auto ids = std::static_pointer_cast<arrow::StringArray>(table->column(1)->chunk(0));
    auto weights = std::static_pointer_cast<arrow::DoubleArray>(table->column(2)->chunk(0));

    const int32_t epoch_2020 = 18262;  // 2020-01-01 as days since 1970-01-01
    const int32_t first_day = 19723, last_day = 20088;  // 2024-01-01, 2024-12-31
    std::map<int32_t, std::map<int, double>> book;
    for (int64_t i = 0; i < table->num_rows(); ++i) {
        const double w = weights->Value(i);
        EXPECT_TRUE(std::isfinite(w) && w != 0.0) << i;
        EXPECT_GE(dates->Value(i), first_day);
        EXPECT_LE(dates->Value(i), last_day);
        if (i > 0) {
            const bool ordered = dates->Value(i - 1) < dates->Value(i) ||
                (dates->Value(i - 1) == dates->Value(i) && ids->GetString(i - 1) < ids->GetString(i));
            EXPECT_TRUE(ordered) << i;
        }
        const std::string id = ids->GetString(i);
        ASSERT_EQ(id.size(), 1u);
        book[dates->Value(i)][std::stoi(id) - 1] = w;
    }
    // Computation starts at simStart - 1: tsmean 3 emits from simStart + 1, its oldest row NaN from delay 1.
    EXPECT_EQ(book.size(), static_cast<std::size_t>(last_day - first_day));
    EXPECT_EQ(book.begin()->first, first_day + 1);
    for (const auto& [day, positions] : book) {
        double gross = 0.0;
        for (const auto& [symbol, w] : positions) gross += std::fabs(w);
        EXPECT_NEAR(gross, 1.0, 1e-12) << day;
    }

    // Close execution with cached total returns and no missing quotes:
    // gross_ret[t+1] = sum_i w[t,i] * (close[t+1,i] / close[t,i] - 1).
    Json::Value row_file;
    ASSERT_TRUE(parse_json(read_text(dir / "results" / "rows" / "5.json"), &row_file));
    const Json::Value& cum = row_file["alphas"][0]["performance"]["cumulativeGross"];
    const int32_t is_day = first_day + 3;  // the performance series starts at isStart (2024-01-04)
    ASSERT_EQ(cum.size(), static_cast<Json::ArrayIndex>(last_day - is_day + 1));
    double previous = 0.0;
    std::size_t checked = 0;
    for (int32_t day = is_day; day <= last_day; ++day) {
        const double gross = cum[static_cast<Json::ArrayIndex>(day - is_day)].asDouble() - previous;
        previous += gross;
        double expected = 0.0;
        for (const auto& [symbol, w] : book[day - 1]) {
            const long t = day - epoch_2020;
            expected += w * (fixture_close(symbol, t) / fixture_close(symbol, t - 1) - 1.0);
        }
        EXPECT_NEAR(gross, expected, 1e-12) << day;
        ++checked;
    }
    EXPECT_EQ(checked, 363u);
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}

TEST(AlphaStorageStartup, UnwritableAlphasDirFailsStartupWithAClearMessage) {
    const fs::path bin = find_server_binary();
    const fs::path cfg = find_config_path();
    if (bin.empty() || cfg.empty()) GTEST_SKIP() << "server binary or LONGSHORT_TEST_CONFIG not found";
    const fs::path temp = fs::temp_directory_path() / ("longshort_alphas_startup_" + std::to_string(::getpid()));
    fs::create_directories(temp);
    std::ofstream(temp / "blocker") << "file";
    // Beside the original so relative data paths resolve; JSON fixtures are YAML too.
    YAML::Node config = YAML::LoadFile(cfg.string());
    config["alphas_dir"] = (temp / "blocker" / "alphas").string();
    YAML::Emitter emitter;
    emitter << config;
    const fs::path startup_cfg = cfg.parent_path() / (".longshort_http_test_" + std::to_string(::getpid()) + ".yaml");
    std::ofstream(startup_cfg) << emitter.c_str() << "\n";
    const int port = test_port() + 7;
    int rc = 0;
    const std::string output = run_command_capture(
        "LONGSHORT_PORT=" + std::to_string(port) + " timeout 120 " + shell_quote(bin.string()) + " " +
        shell_quote(startup_cfg.string()) + " 2>&1", &rc);
    fs::remove(startup_cfg);
    EXPECT_TRUE(WIFEXITED(rc) && WEXITSTATUS(rc) == 1) << output;
    EXPECT_NE(output.find("Cannot start: Cannot create alphas_dir"), std::string::npos) << output;
    std::error_code error;
    fs::remove_all(temp, error);
}

TEST_F(AlphaStorageHttpTest, CorsPreflightAllowsDelete) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    int rc = 0;
    const std::string headers = run_command_capture(
        "curl -sS -o /dev/null -D - -X OPTIONS -H 'Origin: http://localhost:3000' "
        "-H 'Access-Control-Request-Method: DELETE' " +
        shell_quote("http://127.0.0.1:" + std::to_string(server_->port()) + "/api/alphas/foo") + " 2>/dev/null", &rc);
    EXPECT_NE(headers.find(" 200"), std::string::npos) << headers;
    std::string lower = headers;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return std::tolower(c); });
    EXPECT_NE(lower.find("access-control-allow-methods: get, post, delete, options"), std::string::npos) << headers;
}

TEST_F(AlphaStorageHttpTest, DeleteOnlyRemovesSavedAlphaDirectories) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const fs::path dir = alphas_dir();
    const std::string stem = unique_alpha_name("del");
    std::ofstream(dir / (stem + "_legacy.xml")) << "<legacy/>";
    fs::create_directories(dir / (stem + "_other") / "nested");
    fs::create_directory_symlink(dir / (stem + "_other"), dir / (stem + "_link"));
    for (const std::string name : {stem + "_legacy.xml", stem + "_legacy", stem + "_other", stem + "_link"})
        EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 404) << name;
    EXPECT_TRUE(fs::exists(dir / (stem + "_legacy.xml")));
    EXPECT_TRUE(fs::exists(dir / (stem + "_other") / "nested"));
    EXPECT_TRUE(fs::is_symlink(dir / (stem + "_link")));
    fs::remove(dir / (stem + "_link"));
    fs::remove_all(dir / (stem + "_other"));
    fs::remove(dir / (stem + "_legacy.xml"));
}

TEST_F(AlphaStorageHttpTest, RowRoundDefaultsToZeroAndIdsAreJavaScriptSafe) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("ids");
    remove(name);
    const Json::UInt64 max_id = (Json::UInt64(1) << 53) - 1;
    Json::Value request;
    request["name"] = name;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/><rank/>";
    request["level"] = "results";
    request["metadata"] = alpha_metadata();
    Json::Value row;
    row["id"] = max_id + 1; row["xml"] = "<data expr=\"close\" delay=\"1\"/><rank/>";
    request["rows"].append(row);
    EXPECT_EQ(save(request).status, 400);
    request["rows"][0]["id"] = max_id;
    const auto saved = save(request);
    ASSERT_EQ(saved.status, 200) << saved.body;
    Json::Value index;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/alphas/" + name + "/results").body, &index));
    ASSERT_EQ(index["rows"].size(), 1u);
    EXPECT_TRUE(index["rows"][0]["round"].isIntegral());
    EXPECT_EQ(index["rows"][0]["round"].asInt(), 0);
    EXPECT_EQ(index["rows"][0]["id"].asUInt64(), max_id);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(max_id)).status, 200);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(max_id + 1)).status, 400);
    EXPECT_EQ(save(Json::Value(request)).status, 409);
    request["name"] = std::string(201, 'n');
    EXPECT_EQ(save(request).status, 400);
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}

TEST_F(HttpContractTest, SimulateRejectsThreadsOutside1To32) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"5\"/>";
    request["isStart"] = "2021-01-01"; request["osStart"] = "2100-01-01";
    request["output"] = "performance";
    const auto omitted = simulate(request);
    ASSERT_EQ(omitted.status, 200) << omitted.body;
    for (const int threads : {0, 33}) {
        request["threads"] = threads;
        const auto rejected = simulate(request);
        EXPECT_EQ(rejected.status, 400) << threads << ": " << rejected.body.substr(0, 200);
        EXPECT_NE(rejected.body.find("threads must be between 1 and 32"), std::string::npos)
            << rejected.body.substr(0, 200);
    }
    request["threads"] = 32;
    const auto accepted = simulate(request);
    ASSERT_EQ(accepted.status, 200) << accepted.body;
    Json::Value body, omitted_body;
    ASSERT_TRUE(parse_json(accepted.body, &body)) << accepted.body;
    ASSERT_TRUE(parse_json(omitted.body, &omitted_body));
    EXPECT_EQ(body["alphas"].size(), 1u);
    EXPECT_EQ(compact_json(body["alphas"]), compact_json(omitted_body["alphas"]));

    Json::Value ga;
    ga["xml"] = "<data expr=\"close\"/><tsmean days=\"1...4\"/>";
    ga["isStart"] = "2021-01-01"; ga["osStart"] = "2100-01-01";
    ga["threads"] = 33;
    const auto ga_rejected = http_post_json(server_->port(), "/api/ga/stream", ga);
    EXPECT_EQ(ga_rejected.status, 400) << ga_rejected.body;
}

namespace {
// utime + stime of the server process, in seconds.
double process_cpu_seconds(pid_t pid)
{
    std::ifstream in("/proc/" + std::to_string(pid) + "/stat");
    std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    const auto close = text.rfind(')');
    if (close == std::string::npos) return -1.0;
    std::istringstream fields(text.substr(close + 2));
    std::string field;
    unsigned long long utime = 0, stime = 0;
    for (int index = 3; fields >> field; ++index) {
        if (index == 14) utime = std::stoull(field);
        if (index == 15) { stime = std::stoull(field); break; }
    }
    return static_cast<double>(utime + stime) / static_cast<double>(::sysconf(_SC_CLK_TCK));
}
}  // namespace

TEST_F(HttpContractTest, SimulateStopsWhenClientDisconnects) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    std::string xml = "<data expr=\"close\"/>";
    // min_count 1 keeps every rank finite so each module does O(days) work per cell (~3 s total).
    for (int i = 0; i < 300; ++i) xml += "<data expr=\"close\"/><tsrank days=\"1800\" min_count=\"1\"/>";
    Json::Value request;
    request["xml"] = xml;
    request["isStart"] = "2025-01-01"; request["osStart"] = "2100-01-01";
    const pid_t pid = server_->pid();

    const double cpu_before = process_cpu_seconds(pid);
    const auto wall_start = std::chrono::steady_clock::now();
    const auto full = simulate(request);
    const double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - wall_start).count();
    const double cpu_full = process_cpu_seconds(pid) - cpu_before;
    ASSERT_EQ(full.status, 200) << full.body.substr(0, 300);
    // Gate on CPU time: a loaded machine inflates wall time while CPU stays small, and a fast
    // machine must not skip silently. The server must have had real work to abandon.
    if (cpu_full < 1.5) GTEST_SKIP() << "single run used only " << cpu_full << " s CPU on this fixture";

    const double abort_before = process_cpu_seconds(pid);
    const auto aborted = http_post_json(server_->port(), "/api/simulate", request, 0.3);
    EXPECT_EQ(aborted.status, 0);
    std::this_thread::sleep_for(std::chrono::duration<double>(wall + 0.5));
    const double cpu_abort = process_cpu_seconds(pid) - abort_before;
    std::cout << "[disconnect] wall=" << wall << " cpu_full=" << cpu_full << " cpu_abort=" << cpu_abort << "\n";
    EXPECT_LT(cpu_abort, 0.6 * cpu_full) << "wall=" << wall << " cpu_full=" << cpu_full;
}

// <meta expr>: fixture sets under <fixture>/meta (sample, broken, empty, nested).
namespace {
Json::Value meta_request(const std::string& xml)
{
    Json::Value request;
    request["xml"] = xml;
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2100-01-01";
    request["signalPlotMaxCells"] = 1000;
    return request;
}

Json::Value sample_meta_list()
{
    Json::Value list(Json::arrayValue);
    Json::Value entry;
    entry["name"] = "sample";
    entry["members"] = 3;
    entry["combine"] = "ew";
    list.append(entry);
    return list;
}

const std::vector<std::string> kSampleMembers = {
    "<data expr=\"close\" delay=\"1\"/><rank/>\n",
    "<data expr=\"close\" delay=\"1\"/><tsmean days=\"5\"/><zscore/>\n",
    "<data expr=\"close\" delay=\"1\"/><tsrank days=\"10\"/>\n"};
}  // namespace

TEST_F(HttpContractTest, MetaEqualsCombinedBatch) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto started = std::chrono::steady_clock::now();
    const auto meta = simulate(meta_request("<meta expr=\"sample\"/>"));
    const auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
    std::cout << "[ meta run ] <meta expr=\"sample\"/> full body: " << elapsed << " s\n";
    ASSERT_EQ(meta.status, 200) << meta.body;
    Json::Value combined_request = meta_request("");
    combined_request.removeMember("xml");
    combined_request["xmls"] = Json::Value(Json::arrayValue);
    for (const auto& xml : kSampleMembers) combined_request["xmls"].append(xml);
    combined_request["combinedOnly"] = true;
    combined_request["output"] = "performance";
    const auto combined = simulate(combined_request);
    ASSERT_EQ(combined.status, 200) << combined.body;
    Json::Value meta_body, combined_body;
    ASSERT_TRUE(parse_json(meta.body, &meta_body));
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    const auto& batch = meta_body["alphas"][0];
    const auto& expected = combined_body["combined"];
    EXPECT_EQ(batch["meta"], sample_meta_list());
    for (const char* key : {"summary", "yearly", "performance", "missingData"})
        EXPECT_EQ(batch[key], expected[key]) << key;
    EXPECT_EQ(meta_body["alphas"][0]["performance"], combined_body["combined"]["performance"]);
    EXPECT_TRUE(meta_body["alphas"][0]["signals"].isMember("plot"));
}

TEST_F(HttpContractTest, MetaWithPostModule) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto plain = simulate(meta_request("<meta expr=\"sample\"/>"));
    const auto neutral = simulate(meta_request("<meta expr=\"sample\"/><neutralize group=\"sector\"/>"));
    ASSERT_EQ(plain.status, 200) << plain.body;
    ASSERT_EQ(neutral.status, 200) << neutral.body;
    Json::Value a, b;
    ASSERT_TRUE(parse_json(plain.body, &a));
    ASSERT_TRUE(parse_json(neutral.body, &b));
    EXPECT_EQ(b["alphas"][0]["meta"], sample_meta_list());
    EXPECT_NE(a["alphas"][0]["summary"], b["alphas"][0]["summary"]);
    // An ordinary alpha carries no meta key.
    const auto ordinary = simulate(meta_request("<data expr=\"close\" delay=\"1\"/><rank/>"));
    Json::Value c;
    ASSERT_TRUE(parse_json(ordinary.body, &c));
    EXPECT_FALSE(c["alphas"][0].isMember("meta"));
}

TEST_F(HttpContractTest, MetaExplicitEwEqualsImplicit) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto implicit = simulate(meta_request("<meta expr=\"sample\"/>"));
    const auto explicit_ew = simulate(meta_request("<meta expr=\"sample\"/><ew/>"));
    ASSERT_EQ(implicit.status, 200) << implicit.body;
    ASSERT_EQ(explicit_ew.status, 200) << explicit_ew.body;
    Json::Value a, b;
    ASSERT_TRUE(parse_json(implicit.body, &a));
    ASSERT_TRUE(parse_json(explicit_ew.body, &b));
    a["alphas"][0].removeMember("xml");
    b["alphas"][0].removeMember("xml");
    EXPECT_EQ(a, b);
}

TEST_F(HttpContractTest, MetaExprPipeGivesTwoBatches) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto broken = simulate(meta_request("<meta expr=\"sample|broken\"/>"));
    EXPECT_EQ(broken.status, 400) << broken.body;
    EXPECT_NE(broken.body.find("bad.xml"), std::string::npos) << broken.body;
    // simulate does not dedupe expanded alphas: two identical batches.
    const auto twice = simulate(meta_request("<meta expr=\"sample|sample\"/>"));
    ASSERT_EQ(twice.status, 200) << twice.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(twice.body, &body));
    ASSERT_EQ(body["alphas"].size(), 2u);
    EXPECT_EQ(body["alphas"][0]["summary"], body["alphas"][1]["summary"]);
    EXPECT_EQ(body["alphas"][0]["meta"], sample_meta_list());
    EXPECT_EQ(body["alphas"][1]["meta"], sample_meta_list());
    const auto single = simulate(meta_request("<meta expr=\"sample\"/>"));
    Json::Value single_body;
    ASSERT_TRUE(parse_json(single.body, &single_body));
    EXPECT_EQ(body["alphas"][0]["summary"], single_body["alphas"][0]["summary"]);
}

TEST_F(HttpContractTest, MetaRequestErrorsReturn400) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::vector<std::pair<std::string, std::vector<std::string>>> cases = {
        {"<meta expr=\"nope\"/>", {"Unknown meta 'nope'"}},
        {"<meta expr=\"empty\"/>", {"meta 'empty' has no .xml files"}},
        {"<meta expr=\"nested\"/>", {"nested <meta> is not allowed", "x.xml"}},
        {"<meta expr=\"broken\"/>", {"meta 'broken' member bad.xml", "dayz"}},
    };
    for (const auto& [xml, needles] : cases) {
        const auto response = simulate(meta_request(xml));
        EXPECT_EQ(response.status, 400) << xml << ": " << response.body;
        for (const auto& needle : needles)
            EXPECT_NE(response.body.find(needle), std::string::npos) << xml << ": " << response.body;
    }
}

TEST_F(HttpContractTest, MetaInGAStream) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"sample\"/><neutralize group=\"sector|industry\"/>");
    request["firstSearch"] = 2; request["stopRound"] = 1;
    request["threads"] = 4;
    const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
    ASSERT_EQ(response.status, 200) << response.body;
    std::istringstream stream(response.body);
    std::string line;
    std::vector<Json::Value> candidates;
    while (std::getline(stream, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event)) << line;
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "candidate") candidates.push_back(event["candidate"]);
    }
    ASSERT_EQ(candidates.size(), 2u);
    for (const auto& candidate : candidates) {
        EXPECT_EQ(candidate["status"].asString(), "completed") << compact_json(candidate);
        EXPECT_EQ(candidate["meta"], sample_meta_list());
        Json::Value live_request = meta_request(candidate["xml"].asString());
        live_request["output"] = "performance";
        const auto live = simulate(live_request);
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value body;
        ASSERT_TRUE(parse_json(live.body, &body));
        EXPECT_EQ(candidate["summary"], body["alphas"][0]["summary"]) << candidate["xml"].asString();
    }
}

TEST_F(AlphaStorageHttpTest, SaveRejectsInsufficientHistoryBeforeWriting) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("short");
    remove(name);
    const std::string xml = "<data expr=\"close\"/><tsmean days=\"200\"/>";
    Json::Value request;
    request["name"] = name;
    request["xml"] = xml;
    request["level"] = "results";
    request["metadata"] = alpha_metadata();
    Json::Value row;
    row["id"] = 0; row["xml"] = xml;
    request["rows"].append(row);
    const auto saved = save(request);
    EXPECT_EQ(saved.status, 400);
    Json::Value body;
    ASSERT_TRUE(parse_json(saved.body, &body));
    EXPECT_EQ(body["error"].asString(), "Insufficient history");
    EXPECT_FALSE(fs::exists(alphas_dir() / name));
}

TEST_F(AlphaStorageHttpTest, MetaSaveResultsLevel) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("meta");
    remove(name);
    Json::Value metadata = alpha_metadata();
    metadata["isStart"] = "2024-04-01";  // the members need warmup
    const std::vector<std::string> xmls = {
        "<meta expr=\"sample\"/>\n<neutralize group=\"sector\"/>",
        "<meta expr=\"sample\"/>\n<neutralize group=\"industry\"/>"};
    Json::Value request;
    request["name"] = name;
    request["xml"] = "<meta expr=\"sample\"/><neutralize group=\"sector|industry\"/>";
    request["level"] = "results";
    request["metadata"] = metadata;
    request["rows"] = Json::Value(Json::arrayValue);
    for (std::size_t i = 0; i < xmls.size(); ++i) {
        Json::Value row;
        row["id"] = static_cast<Json::UInt64>(i);
        row["xml"] = xmls[i];
        request["rows"].append(row);
    }
    const auto saved = save(request);
    ASSERT_EQ(saved.status, 200) << saved.body;
    EXPECT_EQ(read_text(alphas_dir() / name / "alpha.xml"),
              "<meta expr=\"sample\" />\n<neutralize group=\"sector|industry\" />\n");
    const auto index_response = http_get(server_->port(), "/api/alphas/" + name + "/results");
    ASSERT_EQ(index_response.status, 200) << index_response.body;
    Json::Value index;
    ASSERT_TRUE(parse_json(index_response.body, &index));
    ASSERT_EQ(index["rows"].size(), 2u);
    for (std::size_t i = 0; i < xmls.size(); ++i) {
        Json::Value live_request;
        live_request["xml"] = xmls[i];
        live_request["output"] = "performance";
        const auto live = simulate(with_metadata(live_request, metadata));
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value expected, stored;
        ASSERT_TRUE(parse_json(live.body, &expected));
        EXPECT_EQ(expected["alphas"][0]["meta"], sample_meta_list());
        EXPECT_EQ(index["rows"][static_cast<Json::ArrayIndex>(i)]["summary"], expected["alphas"][0]["summary"]);
        const auto file = http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(i));
        ASSERT_TRUE(parse_json(file.body, &stored));
        expected["alphas"][0]["id"] = static_cast<int>(i);
        expected["alphas"][0]["round"] = 0;
        expected["alphas"][0]["origin"] = "saved";
        EXPECT_EQ(stored["context"]["threads"].asUInt(), 7u);
        EXPECT_EQ(stored, expected);
    }
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}



TEST_F(HttpContractTest, MetaDisconnectStopsMembers) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"heavy\"/>");
    request["isStart"] = "2025-01-01";
    request["output"] = "performance";
    // One member worker: the CPU spent before the disconnect is then ~0.3 s regardless of CPU speed,
    // which keeps the 0.6 x cpu_full margin wide.
    request["threads"] = 1;
    const pid_t pid = server_->pid();
    const double cpu_before = process_cpu_seconds(pid);
    const auto wall_start = std::chrono::steady_clock::now();
    const auto full = simulate(request);
    const double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - wall_start).count();
    const double cpu_full = process_cpu_seconds(pid) - cpu_before;
    ASSERT_EQ(full.status, 200) << full.body.substr(0, 300);
    std::cout << "[meta disconnect] wall=" << wall << " cpu_full=" << cpu_full << "\n";
    if (cpu_full < 1.5) GTEST_SKIP() << "meta run used only " << cpu_full << " s CPU on this fixture";

    const double abort_before = process_cpu_seconds(pid);
    const auto aborted = http_post_json(server_->port(), "/api/simulate", request, 0.3);
    EXPECT_EQ(aborted.status, 0);
    std::this_thread::sleep_for(std::chrono::duration<double>(wall + 0.5));
    const double cpu_abort = process_cpu_seconds(pid) - abort_before;
    std::cout << "[meta disconnect] cpu_abort=" << cpu_abort << "\n";
    EXPECT_LT(cpu_abort, 0.6 * cpu_full) << "wall=" << wall << " cpu_full=" << cpu_full;
}

// Non-EW combinations (<mvo>, <minvar>, ...) of the fixture sets.
namespace {
std::string mvo_key(int days, const std::string& delta)
{
    return "mvo days=" + std::to_string(days) + " shrink=diag delta=" + delta + " rebalance=1";
}
}  // namespace

TEST_F(HttpContractTest, MetaMvoRunsAndReports) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto mvo = simulate(meta_request("<meta expr=\"sample\"/><mvo days=\"60\"/>"));
    const auto ew = simulate(meta_request("<meta expr=\"sample\"/>"));
    ASSERT_EQ(mvo.status, 200) << mvo.body.substr(0, 300);
    ASSERT_EQ(ew.status, 200) << ew.body.substr(0, 300);
    Json::Value a, b;
    ASSERT_TRUE(parse_json(mvo.body, &a));
    ASSERT_TRUE(parse_json(ew.body, &b));
    const auto& meta = a["alphas"][0]["meta"];
    ASSERT_EQ(meta.size(), 1u);
    EXPECT_EQ(meta[0]["name"].asString(), "sample");
    EXPECT_EQ(meta[0]["members"].asUInt(), 3u);
    EXPECT_EQ(meta[0]["combine"].asString(), mvo_key(60, "0.5"));
    ASSERT_TRUE(meta[0].isMember("fallbackBars"));
    ASSERT_TRUE(meta[0].isMember("firstEstimatedBar"));
    EXPECT_EQ(meta[0]["firstEstimatedBar"].asUInt(), 60u) << compact_json(meta);
    std::cout << "[ meta mvo ] " << compact_json(meta) << "\n";
    EXPECT_NE(a["alphas"][0]["summary"], b["alphas"][0]["summary"]);
}

TEST_F(HttpContractTest, MetaMethodsAllRun) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::vector<std::tuple<std::string, std::string, std::string>> cases = {
        {"sample", "<minvar days=\"60\"/>", "minvar days=60 shrink=diag delta=0.5 rebalance=1"},
        {"sample", "<minvar days=\"60\" shrink=\"lw\" rebalance=\"5\"/>", "minvar days=60 shrink=lw rebalance=5"},
        {"sample", "<erc days=\"60\"/>", "erc days=60 rebalance=1"},
        {"sample", "<ivol days=\"60\"/>", "ivol days=60 rebalance=1"},
        {"sample", "<spca days=\"60\" k=\"2\"/>", "spca days=60 k=2 rebalance=1"},
        {"sample", "<glasso days=\"60\" alpha=\"0.1\"/>", "glasso days=60 alpha=0.1 rebalance=1"},
        {"sample", "<ew sign=\"60\"/>", "ewsign days=60 rebalance=1"},
        {"ten", "<spca days=\"60\" k=\"4\"/>", "spca days=60 k=4 rebalance=1"},
    };
    for (const auto& [name, tag, key] : cases) {
        const auto response = simulate(meta_request("<meta expr=\"" + name + "\"/>" + tag));
        ASSERT_EQ(response.status, 200) << tag << ": " << response.body.substr(0, 300);
        Json::Value body;
        ASSERT_TRUE(parse_json(response.body, &body));
        const auto& meta = body["alphas"][0]["meta"][0];
        EXPECT_EQ(meta["combine"].asString(), key) << tag;
        EXPECT_EQ(meta["members"].asUInt(), name == "ten" ? 10u : 3u);
        EXPECT_TRUE(meta.isMember("fallbackBars")) << tag;
        EXPECT_TRUE(std::isfinite(body["alphas"][0]["summary"]["is"]["sharpe"].asDouble())) << tag;
    }
}

TEST_F(HttpContractTest, MetaSpecErrors400) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::vector<std::pair<std::string, std::string>> cases = {
        {"<meta expr=\"sample\"/><spca days=\"60\" k=\"4\"/>", "meta 'sample' <spca>: k=4 exceeds the member count 3"},
        {"<meta expr=\"sample\"/><mvo days=\"5000\"/>", "Insufficient history"},
    };
    for (const auto& [xml, needle] : cases) {
        const auto response = simulate(meta_request(xml));
        EXPECT_EQ(response.status, 400) << xml << ": " << response.body;
        EXPECT_NE(response.body.find(needle), std::string::npos) << xml << ": " << response.body;
    }
    // In an expanded batch the failing spec fails the request too.
    const auto batch = simulate(meta_request("<meta expr=\"sample\"/><spca days=\"60\" k=\"2|4\"/>"));
    EXPECT_EQ(batch.status, 400) << batch.body.substr(0, 300);
    EXPECT_NE(batch.body.find("k=4 exceeds the member count 3"), std::string::npos) << batch.body;
}

// Fixture bars are consecutive days from 2020-01-01; simStart 2020-01-11 is bar 10, computation starts at 9.
TEST_F(HttpContractTest, InsufficientHistoryBoundary) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::vector<std::tuple<std::string, std::string, std::string>> cases = {
        // delay 1 then tsmean 5: valid at 9 + 1 + 4 = 14, so the first IS bar may be 15.
        {"<data expr=\"close\" delay=\"1\"/><tsmean days=\"5\"/>", "2020-01-16", "2020-01-15"},
        // <data> restart: the longer first chain decides (9 + 4 = 13).
        {"<data expr=\"close\"/><tsmean days=\"5\"/><data expr=\"close\"/><rank/>", "2020-01-15", "2020-01-14"},
        // pcaresid fits at multiples of refit: 9 + 20 = 29 rounds up to 35.
        {"<data expr=\"close\"/><pcaresid train=\"20\" refit=\"7\"/>", "2020-02-06", "2020-02-05"},
    };
    for (const auto& [xml, enough, short_by_one] : cases) {
        Json::Value request;
        request["xml"] = xml;
        request["simStart"] = "2020-01-11";
        request["osStart"] = "2100-01-01";
        request["output"] = "performance";
        request["isStart"] = enough;
        const auto ok = simulate(request);
        EXPECT_EQ(ok.status, 200) << xml << ": " << ok.body.substr(0, 300);
        request["isStart"] = short_by_one;
        const auto rejected = simulate(request);
        EXPECT_EQ(rejected.status, 400) << xml;
        Json::Value body;
        ASSERT_TRUE(parse_json(rejected.body, &body));
        EXPECT_EQ(body["error"].asString(), "Insufficient history") << xml;
    }
}

TEST_F(HttpContractTest, InsufficientHistoryAroundSimStart) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    auto run = [&](const std::string& xml, const char* sim_start, const char* is_start, const char* sim_end = "") {
        Json::Value request;
        request["xml"] = xml;
        if (*sim_start) request["simStart"] = sim_start;
        if (*sim_end) request["simEnd"] = sim_end;
        request["isStart"] = is_start;
        request["osStart"] = "2100-01-01";
        request["output"] = "performance";
        return simulate(request);
    };
    const std::string delayed = "<data expr=\"close\" delay=\"1\"/>";
    // Valid exactly at simStart: the book is unfunded there, so isStart == simStart runs.
    EXPECT_EQ(run(delayed, "2020-01-11", "2020-01-11").status, 200);
    // isStart < simStart: simStart is the bound. Valid at 9 + 4 = 13 > 10.
    const std::string window = "<data expr=\"close\"/><tsmean days=\"5\"/>";
    const auto rejected = run(window, "2020-01-11", "2020-01-02");
    EXPECT_EQ(rejected.status, 400);
    EXPECT_NE(rejected.body.find("Insufficient history"), std::string::npos);
    EXPECT_EQ(run("<data expr=\"close\"/><tsmean days=\"2\"/>", "2020-01-11", "2020-01-02").status, 200);
    // isStart before the panel: delay 0 runs, delay 1 cannot be valid on bar 0.
    EXPECT_EQ(run("<data expr=\"close\" delay=\"0\"/><rank/>", "", "2000-01-01").status, 200);
    EXPECT_EQ(run(delayed, "", "2000-01-01").status, 400);
    // Server panels carry a returns field, so no expr operand has lookback here (see test_lookback for
    // that case); the binary expr is timed with its delay: valid at bar 2 from simStart 0.
    EXPECT_EQ(run("<data expr=\"close - returns\" delay=\"2\"/>", "", "2020-01-04").status, 200);
    EXPECT_EQ(run("<data expr=\"close - returns\" delay=\"2\"/>", "", "2020-01-03").status, 400);
    // Never valid inside the run, however late IS is: valid at 9 + 99 = 108 > 19.
    const auto never = run("<data expr=\"close\"/><tsmean days=\"100\"/>", "2020-01-11", "2021-01-01", "2020-01-20");
    EXPECT_EQ(never.status, 400);
    EXPECT_NE(never.body.find("Insufficient history"), std::string::npos);
}

TEST_F(HttpContractTest, InsufficientHistoryRejectsWholeBatch) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xmls"] = Json::Value(Json::arrayValue);
    request["xmls"].append("<data expr=\"close\"/><tsmean days=\"2\"/>");
    request["xmls"].append("<data expr=\"close\"/><tsmean days=\"200\"/>");
    request["isStart"] = "2020-04-10";  // bar 100
    request["osStart"] = "2100-01-01";
    request["output"] = "performance";
    for (const bool combined_only : {false, true}) {
        request["combinedOnly"] = combined_only;
        const auto response = simulate(request);
        EXPECT_EQ(response.status, 400);
        Json::Value body;
        ASSERT_TRUE(parse_json(response.body, &body));
        EXPECT_EQ(body["error"].asString(), "Insufficient history");
    }
}

TEST_F(HttpContractTest, GaReportsInsufficientHistoryPerCandidate) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    auto search = [&](const std::string& xml) {
        Json::Value request;
        request["xml"] = xml;
        request["isStart"] = "2020-04-10";  // bar 100
        request["osStart"] = "2100-01-01";
        request["threads"] = 2;
        const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
        EXPECT_EQ(response.status, 200) << response.body;
        std::vector<Json::Value> events;
        std::istringstream stream(response.body);
        std::string line;
        while (std::getline(stream, line)) {
            if (line.empty()) continue;
            Json::Value event;
            EXPECT_TRUE(parse_json(line, &event)) << line;
            events.push_back(event);
        }
        return events;
    };
    auto failed_count = [](const std::vector<Json::Value>& events) {
        unsigned failed = 0;
        for (const auto& event : events) {
            if (event["type"].asString() != "candidate" || event["candidate"]["status"].asString() != "failed") continue;
            EXPECT_EQ(event["candidate"]["error"].asString(), "Insufficient history");
            ++failed;
        }
        return failed;
    };
    const auto mixed = search("<data expr=\"close\"/><tsmean days=\"2|3|200\"/>");
    ASSERT_FALSE(mixed.empty());
    EXPECT_EQ(mixed.back()["type"].asString(), "search_complete") << compact_json(mixed.back());
    EXPECT_EQ(failed_count(mixed), 1u);
    EXPECT_NE(mixed.back()["bestXml"].asString().find("tsmean"), std::string::npos);

    const auto all_failed = search("<data expr=\"close\"/><tsmean days=\"200|300\"/>");
    ASSERT_FALSE(all_failed.empty());
    EXPECT_EQ(all_failed.back()["type"].asString(), "error") << compact_json(all_failed.back());
    EXPECT_EQ(all_failed.back()["error"].asString(), "Insufficient history");
    EXPECT_EQ(failed_count(all_failed), 2u);

    const auto one_candidate = search("<data expr=\"close\"/><tsmean days=\"200\"/>");
    ASSERT_FALSE(one_candidate.empty());
    EXPECT_EQ(one_candidate.back()["type"].asString(), "error");
    EXPECT_EQ(one_candidate.back()["error"].asString(), "Insufficient history");
}

TEST_F(HttpContractTest, MetaRebalanceGridBoundsInsufficientHistory) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    // Sample lookback 10 from simStart - 1: the window must start at simStart + 10, so the grid
    // simStart + 40 + 60k gives bar 100 (2022-05-12), which positions the bar after it.
    Json::Value request = meta_request("<meta expr=\"sample\"/><mvo days=\"40\" rebalance=\"60\"/>");
    request["simStart"] = "2022-02-01";
    request["output"] = "performance";
    request["isStart"] = "2022-05-13";
    EXPECT_EQ(simulate(request).status, 200);
    request["isStart"] = "2022-05-12";
    const auto rejected = simulate(request);
    EXPECT_EQ(rejected.status, 400);
    EXPECT_NE(rejected.body.find("Insufficient history"), std::string::npos);
}

TEST_F(HttpContractTest, EveryPathRunsWithSimStartMidPanel) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    auto with_dates = [](Json::Value request) {
        // Dates used by no other test: the member-return cache is process-wide.
        request["simStart"] = "2021-08-17";
        request["isStart"] = "2021-11-01";
        request["osStart"] = "2022-06-01";
        request["simEnd"] = "2022-12-31";
        return request;
    };
    const std::string rolling = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"5\"/><rank/>";
    Json::Value single;
    single["xml"] = rolling;
    single["signalPlotMaxCells"] = 1000;
    for (const char* output : {"full", "performance", "signals"}) {
        single["output"] = output;
        const auto response = simulate(with_dates(single));
        ASSERT_EQ(response.status, 200) << output << ": " << response.body.substr(0, 300);
        Json::Value body;
        ASSERT_TRUE(parse_json(response.body, &body));
        EXPECT_EQ(body["context"]["simStart"].asString(), "2021-08-17");
        if (std::string(output) != "performance") {
            const auto& plot = body["alphas"][0]["signals"]["plot"];
            ASSERT_GT(plot["values"].size(), 0u);
            // Row 0 is simStart; tsmean emits only once 5 rows were pushed (simStart + 3). Rows are sampled.
            const auto finite_in = [&](Json::ArrayIndex row) {
                Json::UInt finite = 0;
                for (const auto& value : plot["values"][row]) finite += !value.isNull();
                return finite;
            };
            EXPECT_EQ(finite_in(0), 0u);
            EXPECT_GT(finite_in(plot["values"].size() - 1), 0u);
            single["signalPlotSymbols"] = Json::Value(Json::arrayValue);
            single["signalPlotSymbols"].append(plot["symbols"][0]);
        }
    }
    single["output"] = "signalSeries";
    const auto series = simulate(with_dates(single));
    ASSERT_EQ(series.status, 200) << series.body.substr(0, 300);
    single.removeMember("signalPlotSymbols");

    single["output"] = "performance";
    const auto performance = simulate(with_dates(single));
    Json::Value performance_body;
    ASSERT_TRUE(parse_json(performance.body, &performance_body));
    EXPECT_GT(performance_body["alphas"][0]["performance"]["timestamps"].size(), 0u);

    Json::Value batch;
    batch["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"3|5\"/><rank/>";
    batch["output"] = "full";
    batch["signalPlotMaxCells"] = 1000;
    const auto combined = simulate(with_dates(batch));
    ASSERT_EQ(combined.status, 200) << combined.body.substr(0, 300);
    Json::Value combined_body;
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    EXPECT_EQ(combined_body["alphas"].size(), 2u);
    EXPECT_TRUE(combined_body["combined"]["summary"].isObject());
    EXPECT_GT(combined_body["combined"]["summary"]["is"]["activeBarCount"].asInt(), 0);

    for (const char* xml : {"<meta expr=\"sample\"/>", "<meta expr=\"sample\"/><rank/>",
                            "<meta expr=\"sample\"/><mvo days=\"40\"/>"}) {
        const auto meta = simulate(with_dates(meta_request(xml)));
        ASSERT_EQ(meta.status, 200) << xml << ": " << meta.body.substr(0, 300);
        Json::Value body;
        ASSERT_TRUE(parse_json(meta.body, &body));
        EXPECT_GT(body["alphas"][0]["summary"]["is"]["activeBarCount"].asInt(), 0) << xml;
    }

    Json::Value ga = with_dates(meta_request(""));
    ga["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3|4\"/><rank/>";
    ga["firstSearch"] = 3; ga["stopRound"] = 1; ga["threads"] = 4;
    const auto search = http_post_json(server_->port(), "/api/ga/stream", ga);
    ASSERT_EQ(search.status, 200) << search.body.substr(0, 300);
    std::istringstream stream(search.body);
    std::string line;
    unsigned candidates = 0;
    bool complete = false;
    while (std::getline(stream, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event));
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "candidate") ++candidates;
        if (event["type"].asString() == "search_complete") complete = true;
    }
    EXPECT_EQ(candidates, 3u);
    EXPECT_TRUE(complete);
}

TEST_F(HttpContractTest, MetaInGAStreamWithMvoGrid) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"sample\"/><mvo days=\"40|60\" shrink=\"0.3|0.7\"/>");
    // Dates used by no other test: the member-return cache is process-wide.
    request["simStart"] = "2021-03-01";
    request["isStart"] = "2021-06-01";
    request["simEnd"] = "2022-11-30";
    request["firstSearch"] = 4; request["stopRound"] = 1;
    request["threads"] = 4;
    request["metaStats"] = true;
    const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
    ASSERT_EQ(response.status, 200) << response.body;
    std::istringstream stream(response.body);
    std::string line;
    std::vector<Json::Value> candidates;
    Json::Value complete;
    while (std::getline(stream, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event)) << line;
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "candidate") candidates.push_back(event["candidate"]);
        if (event["type"].asString() == "search_complete") complete = event;
    }
    ASSERT_EQ(candidates.size(), 4u);
    std::set<std::string> keys;
    for (const auto& candidate : candidates) {
        EXPECT_EQ(candidate["status"].asString(), "completed") << compact_json(candidate);
        keys.insert(candidate["meta"][0]["combine"].asString());
        // Bars after simEnd are not counted: no fallback, first estimate at simStart + days.
        const int sim_start_index = 18687 - 18262;  // 2021-03-01 in the daily fixture from 2020-01-01
        const std::string key = candidate["meta"][0]["combine"].asString();
        const int days = std::stoi(key.substr(key.find("days=") + 5));
        EXPECT_EQ(candidate["meta"][0]["fallbackBars"].asUInt(), 0u) << compact_json(candidate["meta"]);
        EXPECT_EQ(candidate["meta"][0]["firstEstimatedBar"].asInt(), sim_start_index + days) << key;
    }
    EXPECT_EQ(keys, (std::set<std::string>{mvo_key(40, "0.3"), mvo_key(40, "0.7"), mvo_key(60, "0.3"),
                                           mvo_key(60, "0.7")}));
    // One member-return pass and one weighted pass for the round: each member ran twice.
    std::cout << "[ meta GA ] metaStats " << compact_json(complete["metaStats"]) << "\n";
    EXPECT_EQ(complete["metaStats"]["materializations"].asUInt(), 1u) << compact_json(complete);
    EXPECT_EQ(complete["metaStats"]["memberRuns"].asUInt(), 6u) << compact_json(complete);

    // The candidate summary equals a live single run of its XML.
    Json::Value live_request = request;
    for (const char* key : {"firstSearch", "stopRound", "metaStats"}) live_request.removeMember(key);
    live_request["xml"] = candidates[0]["xml"];
    live_request["output"] = "performance";
    const auto live = simulate(live_request);
    ASSERT_EQ(live.status, 200) << live.body.substr(0, 300);
    Json::Value body;
    ASSERT_TRUE(parse_json(live.body, &body));
    EXPECT_EQ(candidates[0]["summary"], body["alphas"][0]["summary"]);
    EXPECT_EQ(candidates[0]["meta"], body["alphas"][0]["meta"]);
}

namespace {
// date -> symbol index -> weight, from a saved positions parquet file.
std::map<int32_t, std::map<int, double>> read_positions(const fs::path& file)
{
    std::map<int32_t, std::map<int, double>> book;
    auto input = arrow::io::ReadableFile::Open(file.string()).ValueOrDie();
    std::unique_ptr<parquet::arrow::FileReader> reader;
    if (!parquet::arrow::OpenFile(input, arrow::default_memory_pool(), &reader).ok()) return book;
    std::shared_ptr<arrow::Table> table;
    if (!reader->ReadTable(&table).ok()) return book;
    table = table->CombineChunks().ValueOrDie();
    auto dates = std::static_pointer_cast<arrow::Date32Array>(table->column(0)->chunk(0));
    auto ids = std::static_pointer_cast<arrow::StringArray>(table->column(1)->chunk(0));
    auto weights = std::static_pointer_cast<arrow::DoubleArray>(table->column(2)->chunk(0));
    for (int64_t i = 0; i < table->num_rows(); ++i) book[dates->Value(i)][std::stoi(ids->GetString(i)) - 1] = weights->Value(i);
    return book;
}
}  // namespace

TEST_F(AlphaStorageHttpTest, MetaMvoMatchesOracleOnFixture) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value metadata = alpha_metadata("close");
    metadata["isStart"] = "2024-04-01";  // the members need warmup
    const int32_t first_day = 19723, last_day = 20088;  // 2024-01-01, 2024-12-31
    const unsigned T = static_cast<unsigned>(last_day - first_day + 1);
    std::vector<std::string> xmls(kSampleMembers.begin(), kSampleMembers.end());
    xmls.push_back("<meta expr=\"sample\"/><mvo days=\"60\"/>");
    const unsigned first_book[4] = {0, 3, 9, 0};
    std::vector<std::map<int32_t, std::map<int, double>>> books;
    MemberReturns returns;
    returns.T = T;
    returns.K = 3;
    returns.sim_start = 0;
    returns.rows.assign(T, std::vector<double>(3, 0.0));
    std::vector<std::string> names;
    for (std::size_t k = 0; k < xmls.size(); ++k) {
        const std::string name = unique_alpha_name("metamvo" + std::to_string(k));
        names.push_back(name);
        remove(name);
        Json::Value request;
        request["name"] = name;
        request["xml"] = xmls[k];
        request["level"] = "positions";
        request["metadata"] = metadata;
        Json::Value row;
        row["id"] = 0; row["xml"] = xmls[k];
        request["rows"].append(row);
        const auto saved = save(request);
        ASSERT_EQ(saved.status, 200) << saved.body;
        books.push_back(read_positions(alphas_dir() / name / "positions" / "0.parquet"));
        // From simStart - 1: rank has a signal on simStart, tsmean 5 emits at +3, tsrank 10 needs 10 finite rows
        // (+9). The combined book holds rank from the first bar.
        const unsigned offset = first_book[k];
        ASSERT_EQ(books.back().size(), T - offset) << xmls[k];
        ASSERT_EQ(books.back().begin()->first, first_day + static_cast<int32_t>(offset)) << xmls[k];
        if (k == 3) continue;
        // Close execution: gross_ret[t] = sum_i w[t-1,i] * (close[t,i] / close[t-1,i] - 1); the
        // performance series starts at isStart, so the returns come from the saved book.
        const long first_t = first_day - 18262;  // 2020-01-01 as days since 1970-01-01
        for (unsigned t = offset + 1; t < T; ++t)
            for (const auto& [symbol, w] : books.back().at(first_day + static_cast<int32_t>(t) - 1))
                returns.rows[t][k] += w * (fixture_close(symbol, first_t + t) / fixture_close(symbol, first_t + t - 1) - 1.0);
    }
    CombineSpec spec;
    spec.method = CombineMethod::Mvo;
    spec.days = 60;
    const CombineWeights weights = compute_combine_weights(returns, spec);
    EXPECT_EQ(weights.first_estimated_bar, 60u);
    std::size_t checked = 0;
    double max_diff = 0.0;
    for (unsigned t = 0; t < T; ++t) {
        const int32_t day = first_day + static_cast<int32_t>(t);
        double combined[4] = {0, 0, 0, 0}, gross = 0.0;
        for (int i = 0; i < 4; ++i) {
            for (int k = 0; k < 3; ++k) {
                if (t < first_book[k]) continue;
                const auto& book = books[k].at(day);
                const auto found = book.find(i);
                if (found != book.end()) combined[i] += weights.rows[t][k] * found->second;
            }
            gross += std::fabs(combined[i]);
        }
        ASSERT_GT(gross, 0.0) << day;
        for (int i = 0; i < 4; ++i) {
            const auto& book = books[3].at(day);
            const auto found = book.find(i);
            const double engine = found == book.end() ? 0.0 : found->second;
            const double expected = combined[i] / gross;
            const double diff = std::fabs(engine - expected);
            max_diff = std::max(max_diff, diff);
            EXPECT_NEAR(engine, expected, 1e-6) << "day " << day << " symbol " << i;
            ++checked;
        }
    }
    std::cout << "[ meta mvo oracle ] cells=" << checked << " max|diff|=" << max_diff << "\n";
    EXPECT_EQ(checked, 4u * T);
    for (const auto& name : names) EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}

TEST_F(HttpContractTest, MetaCrossRequestDisconnectDoesNotFailOthers) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"heavy\"/><mvo days=\"40\"/>");
    request["simStart"] = "2020-04-07";  // used by no other test: the member-return key is new
    request["isStart"] = "2025-06-01";
    request["output"] = "performance";
    request["threads"] = 1;
    // A computes the shared member returns and disconnects; B waits on the same key and must not
    // inherit A's cancellation.
    std::thread first([&] { (void)http_post_json(server_->port(), "/api/simulate", request, 0.8); });
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    const auto second = simulate(request);
    first.join();
    EXPECT_EQ(second.status, 200) << second.body.substr(0, 300);
    std::cout << "[ meta cross-request ] B status " << second.status << "\n";
}

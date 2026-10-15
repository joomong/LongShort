#include <gtest/gtest.h>

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include "api/meta_weights.h"

#include <algorithm>
#include <cctype>
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
    const std::vector<fs::path> candidates = {
        fs::current_path() / "../../config.yaml",
        fs::current_path() / "../config.yaml",
        fs::current_path() / "config.yaml",
    };
    for (const auto& p : candidates) {
        if (fs::exists(p))
            return fs::weakly_canonical(p);
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
            skip_reason_ = "config.yaml not found";
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
        EXPECT_EQ(body["executionPrice"].asString(), price);
        return body;
    };
    const auto close = call("close");
    const auto ohlc4 = call("ohlc4");
    const auto vwap = call("vwap");
    const auto close_again = call("close");
    EXPECT_EQ(close["timeseries"], close_again["timeseries"]);
    EXPECT_EQ(close["timeseries"]["turnover"], ohlc4["timeseries"]["turnover"]);
    EXPECT_NE(close["timeseries"]["cumPnl"], ohlc4["timeseries"]["cumPnl"]);
    EXPECT_EQ(close["timeseries"]["turnover"], vwap["timeseries"]["turnover"]);
    EXPECT_NE(close["timeseries"]["cumPnl"], vwap["timeseries"]["cumPnl"]);
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
    EXPECT_EQ(capped["simEnd"].asString(), "2023-06-30");
    const auto& times = capped["timeseries"]["timestamps"];
    ASSERT_GT(times.size(), 0u);
    EXPECT_EQ(times[times.size()-1].asString(), "2023-06-30");
    for (const char* key : {"timestamps", "cumPnl", "cumPnlGross", "turnover"})
        for (Json::ArrayIndex i=0; i<times.size(); ++i)
            EXPECT_EQ(capped["timeseries"][key][i], full["timeseries"][key][i]);
    ASSERT_EQ(capped["yearly"].size(), 1u);
    EXPECT_EQ(capped["yearly"][0]["year"].asInt(), 2023);
    for (const auto& date : capped["signalPlot"]["timestamps"])
        EXPECT_LE(date.asString(), "2023-06-30");
    EXPECT_DOUBLE_EQ(capped["batches"][0]["summary"]["os"]["pnl"].asDouble(), 0.0);
    req.removeMember("simEnd");
    EXPECT_EQ(call()["timeseries"], full["timeseries"]);
    for (const auto& end : {Json::Value("bad"), Json::Value("2023-02-30"), Json::Value("2023-01-01"), Json::Value(1), Json::Value()}) {
        req["simEnd"] = end;
        EXPECT_EQ(simulate(req).status, 400);
    }
    Json::Value config;
    ASSERT_TRUE(parse_json(http_get(server_->port(), "/api/config").body, &config));
    EXPECT_TRUE(config["supportsSimEnd"].asBool());
    EXPECT_GT(config["simEnd"].asString(), "2023-06-30");
}

TEST_F(HttpContractTest, SimEndAlsoCapsCombinedSignalsAndGa) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value req;
    req["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    req["simStart"] = "2023-01-02"; req["simEnd"] = "2023-06-30";
    req["isStart"] = "2023-01-02"; req["osStart"] = "2023-05-01";
    req["collectSignals"] = true; req["signalPlotMaxCells"] = 10000;
    auto response = simulate(req); ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body; ASSERT_TRUE(parse_json(response.body, &body));
    for (const auto& batch : body["batches"])
        for (const auto& date : batch["timeseries"]["timestamps"])
            EXPECT_LE(date.asString(), "2023-06-30");
    for (const auto& date : body["combined"]["signalPlot"]["timestamps"])
        EXPECT_LE(date.asString(), "2023-06-30");
    req["popSize"] = 2; req["generations"] = 1;
    const auto ga = http_post_json(server_->port(), "/api/ga/stream", req);
    ASSERT_EQ(ga.status, 200) << ga.body;
    std::istringstream stream(ga.body); std::string line; bool completed=false;
    while (std::getline(stream,line)) {
        if (line.rfind("data:",0) == 0) line = line.substr(5);
        Json::Value event;
        if (!parse_json(line, &event) || event["type"].asString() != "complete") continue;
        completed=true;
        EXPECT_EQ(event["simEnd"].asString(), "2023-06-30");
        for (const auto& date : event["timeseries"]["timestamps"])
            EXPECT_LE(date.asString(), "2023-06-30");
        for (const auto& date : event["signalPlot"]["timestamps"])
            EXPECT_LE(date.asString(), "2023-06-30");
    }
    EXPECT_TRUE(completed) << ga.body.substr(0, 1000);
}

TEST_F(HttpContractTest, MultiAlphaBatchesContainPerAlphaXml)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3\"/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("batches"));
    ASSERT_TRUE(body["batches"].isArray());
    ASSERT_EQ(body["batches"].size(), 2u);

    std::set<std::string> xmls;
    bool has_days_2 = false;
    bool has_days_3 = false;
    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i) {
        const Json::Value& b = body["batches"][i];
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
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("batches"));
    ASSERT_TRUE(body["batches"].isArray());
    ASSERT_EQ(body["batches"].size(), 2u);

    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i) {
        const Json::Value& b = body["batches"][i];
        EXPECT_FALSE(b.isMember("signalPlot"));
        EXPECT_FALSE(b.isMember("signalRange"));
        EXPECT_FALSE(b.isMember("distribution"));
        EXPECT_FALSE(b.isMember("signalDistributionLog"));
    }
}

TEST_F(HttpContractTest, MultiAlphaCollectSignalsTopKControlsPayloadCount)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1\"/><tsmean days=\"2|3|4\"/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";
    req["collectSignals"] = true;
    req["collectSignalsTopK"] = 1;

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("batches"));
    ASSERT_TRUE(body["batches"].isArray());
    ASSERT_EQ(body["batches"].size(), 3u);

    int batches_with_signal = 0;
    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i) {
        const Json::Value& b = body["batches"][i];
        if (b.isMember("signalPlot")) {
            ++batches_with_signal;
            ASSERT_TRUE(b["signalPlot"].isObject());
            ASSERT_TRUE(b.isMember("signalRange"));
            ASSERT_TRUE(b.isMember("distribution"));
            ASSERT_TRUE(b["signalDistributionLog"].isObject());
        }
    }
    EXPECT_EQ(batches_with_signal, 1);
}

TEST_F(HttpContractTest, MultiAlphaBatchesSortedByIsSharpeDesc)
{
    if (!available_)
        GTEST_SKIP() << skip_reason_;

    Json::Value req;
    req["xml"] = "<data expr=\"close\" delay=\"1|2|3|4\"/><rank/>";
    req["isStart"] = "2000-01-01";
    req["osStart"] = "2100-01-01";

    const HttpResult res = simulate(req);
    ASSERT_EQ(res.status, 200) << res.body;

    Json::Value body;
    ASSERT_TRUE(parse_json(res.body, &body)) << res.body;
    ASSERT_TRUE(body.isMember("batches"));
    ASSERT_TRUE(body["batches"].isArray());
    ASSERT_EQ(body["batches"].size(), 4u);

    std::set<int> indices;
    double prev = std::numeric_limits<double>::infinity();
    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i) {
        const Json::Value& b = body["batches"][i];
        ASSERT_TRUE(b.isMember("index"));
        ASSERT_TRUE(b.isMember("summary"));
        ASSERT_TRUE(b["summary"].isMember("is"));
        ASSERT_TRUE(b["summary"]["is"].isMember("sharpe"));

        const int idx = b["index"].asInt();
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
    req["isStart"] = "2000-01-01";
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
        ASSERT_TRUE(body.isMember("timeseries"));
        ASSERT_TRUE(body["timeseries"].isObject());

        const std::string combined_compact = compact_json(body["combined"]);
        const std::string top_level_compact = compact_json(body["timeseries"]);
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
    ASSERT_TRUE(body.isMember("signalPlot"));
    ASSERT_TRUE(body["signalPlot"].isObject());

    const Json::Value& signal_plot = body["signalPlot"];
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
    ASSERT_GT(original["timeseries"]["timestamps"].size(), 3u);
    request["simStart"] = original["timeseries"]["timestamps"][3];
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    EXPECT_EQ(body["timeseries"]["timestamps"][0], request["simStart"]);
    EXPECT_EQ(body["signalPlot"]["timestamps"][0], request["simStart"]);
    EXPECT_EQ(body["isStartIndex"].asUInt(), 0u);
    const auto config_response = http_get(server_->port(), "/api/config");
    Json::Value config;
    ASSERT_EQ(config_response.status, 200);
    ASSERT_TRUE(parse_json(config_response.body, &config));
    const double fee = config["feeRate"].asDouble();
    EXPECT_NEAR(body["timeseries"]["cumPnl"][0].asDouble(), -fee, 1e-12);
    EXPECT_NEAR(body["timeseries"]["cumPnlGross"][0].asDouble(), 0.0, 1e-12);
    // Gross minus net over the whole span is exactly the turnover fee.
    const Json::Value& timeseries = body["timeseries"];
    double fee_sum = 0.0;
    for (const auto& turnover : timeseries["turnover"]) fee_sum += turnover.asDouble() / 100.0 * fee;
    const Json::ArrayIndex last = timeseries["cumPnl"].size() - 1;
    EXPECT_NEAR(timeseries["cumPnlGross"][last].asDouble() - timeseries["cumPnl"][last].asDouble(),
                fee_sum, 1e-9);
    // Performance metrics are gross: IS spans the whole trading period here.
    ASSERT_GT(fee_sum, 1e-9);
    EXPECT_NEAR(body["batches"][0]["summary"]["is"]["pnl"].asDouble(),
                timeseries["cumPnlGross"][last].asDouble() * 100.0, 1e-7);
    EXPECT_GT(std::fabs(body["batches"][0]["summary"]["is"]["pnl"].asDouble() -
                        timeseries["cumPnl"][last].asDouble() * 100.0), 1e-7);
    double peak = 0.0, gross_mdd = 0.0;
    for (Json::ArrayIndex i = 0; i <= last; ++i) {
        const double cum = timeseries["cumPnlGross"][i].asDouble();
        peak = std::max(peak, cum);
        gross_mdd = std::min(gross_mdd, cum - peak);
    }
    EXPECT_NEAR(body["batches"][0]["summary"]["is"]["mdd"].asDouble(), gross_mdd * 100.0, 1e-7);
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
    const auto& plot = body["signalPlot"];
    EXPECT_LE(plot["values"].size() * plot["symbols"].size(), 8u);
    EXPECT_EQ(plot["timestamps"].size(), plot["values"].size());
    EXPECT_TRUE(plot.isMember("rowStride"));
    EXPECT_TRUE(plot.isMember("originalRows"));
    ASSERT_TRUE(body["signalDistributionLog"].isObject());
    const auto& log_histogram = body["signalDistributionLog"];
    ASSERT_TRUE(log_histogram["counts"].isArray());
    ASSERT_GT(log_histogram["counts"].size(), 0u);
    EXPECT_EQ(log_histogram["counts"].size(), log_histogram["bins"].size());
    ASSERT_EQ(log_histogram["edges"].size(), log_histogram["counts"].size() + 1);
    EXPECT_EQ(log_histogram["scale"].asString(), "log");
    Json::UInt64 linear_count = 0, log_count = 0;
    for (const auto& count : body["distribution"]["counts"]) linear_count += count.asUInt64();
    for (const auto& count : log_histogram["counts"]) log_count += count.asUInt64();
    EXPECT_EQ(log_count, linear_count);
    EXPECT_EQ(log_histogram["totalCount"].asUInt64(), log_count);
    // The histogram pools full signals, not the eight-cell display sample.
    EXPECT_GT(log_count, 8u);
    EXPECT_EQ(log_histogram["min"], body["distribution"]["min"]);
    EXPECT_EQ(log_histogram["max"], body["distribution"]["max"]);
    EXPECT_EQ(log_histogram["edges"][0], log_histogram["min"]);
    EXPECT_EQ(log_histogram["edges"][log_histogram["edges"].size() - 1], log_histogram["max"]);
    request["simStart"] = "not-a-date";
    EXPECT_EQ(simulate(request).status, 400);
    request["simStart"] = "2200-01-01";
    EXPECT_EQ(simulate(request).status, 400);
}

TEST_F(HttpContractTest, CombinedAndTopSignalPayloadsAreExplicit) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3|4\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    request["collectSignals"] = true;
    request["collectSignalsTopK"] = 1;
    request["signalPlotMaxCells"] = 12;
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    EXPECT_FALSE(body.isMember("combinedMode"));
    EXPECT_TRUE(body["combined"].isMember("signalPlot"));
    EXPECT_EQ(body["signalPlot"], body["combined"]["signalPlot"]);
    ASSERT_TRUE(body["signalDistributionLog"].isObject());
    EXPECT_EQ(body["signalDistributionLog"], body["combined"]["signalDistributionLog"]);
    ASSERT_EQ(body["batches"].size(), 3u);
    EXPECT_TRUE(body["batches"][0].isMember("signalPlot"));
    EXPECT_FALSE(body["batches"][1].isMember("signalPlot"));
    EXPECT_TRUE(body["batches"][0]["signalDistributionLog"].isObject());
    EXPECT_FALSE(body["batches"][1].isMember("signalDistributionLog"));
    EXPECT_LE(body["batches"][0]["signalPlot"]["values"].size() *
              body["batches"][0]["signalPlot"]["symbols"].size(), 12u);
}

TEST_F(HttpContractTest, GaCompleteContainsBoundedBestAlphaSignals) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    request["popSize"] = 2;
    request["generations"] = 1;
    request["signalPlotMaxCells"] = 12;
    const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
    ASSERT_EQ(response.status, 200) << response.body;
    std::istringstream lines(response.body);
    std::string line;
    Json::Value complete;
    while (std::getline(lines, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event)) << line;
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "complete") complete = event;
    }
    ASSERT_EQ(complete["type"].asString(), "complete") << response.body;
    EXPECT_TRUE(complete.isMember("signalRange"));
    EXPECT_TRUE(complete.isMember("distribution"));
    EXPECT_TRUE(complete["signalDistributionLog"].isObject());
    EXPECT_EQ(complete["signalDistributionLog"], complete["batches"][0]["signalDistributionLog"]);
    EXPECT_EQ(complete["signalPlot"], complete["batches"][0]["signalPlot"]);
    EXPECT_LE(complete["signalPlot"]["values"].size() * complete["signalPlot"]["symbols"].size(), 12u);
}


TEST_F(HttpContractTest, XmlDelayOwnsTimingWithoutAnotherShift) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"1\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    auto call = [&](const Json::Value& payload) {
        const auto response = simulate(payload);
        EXPECT_EQ(response.status, 200) << response.body;
        Json::Value body;
        EXPECT_TRUE(parse_json(response.body, &body));
        return body;
    };
    const auto initial = call(request);
    ASSERT_GT(initial["timeseries"]["timestamps"].size(), 3u);
    request["simStart"] = initial["timeseries"]["timestamps"][3];
    const auto delayed = call(request);
    double gross = 0.0, maximum = 0.0;
    for (const auto& value : delayed["signalPlot"]["values"][0]) {
        if (value.isNull()) continue;
        const double absolute = std::fabs(value.asDouble());
        gross += absolute;
        maximum = std::max(maximum, absolute);
    }
    EXPECT_NEAR(delayed["timeseries"]["maxWeight"][0].asDouble(), 100.0 * maximum / gross, 1e-8);
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
    ASSERT_TRUE(body.isMember("missingData"));
    ASSERT_TRUE(body["batches"][0].isMember("missingData"));
    EXPECT_EQ(compact_json(body["missingData"]), compact_json(body["batches"][0]["missingData"]));
    const Json::Value& totals = body["missingData"];
    for (const char* key : {"missingHeldBars", "blockedOrders", "blockedTradeWeight",
                            "missingEntries", "resumedHoldings", "terminalFrozenGross", "frozenGrossSum"}) {
        ASSERT_TRUE(totals.isMember(key)) << key;
        EXPECT_TRUE(totals[key].isNumeric()) << key;
    }
    EXPECT_LE(totals["missingEntries"].asUInt64(), totals["blockedOrders"].asUInt64());
    const Json::Value& timeseries = body["timeseries"];
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
    EXPECT_EQ(compact_json(body["missingData"]), compact_json(body["combined"]["missingData"]));
    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i)
        EXPECT_TRUE(body["batches"][i].isMember("missingData"));

    const Json::Value& totals = body["missingData"];
    const bool quiet = totals["missingHeldBars"].asUInt64() == 0 &&
                       totals["blockedOrders"].asUInt64() == 0 &&
                       totals["missingEntries"].asUInt64() == 0;
    request["missingClose"] = "error";
    const auto strict = simulate(request);
    if (quiet) {
        ASSERT_EQ(strict.status, 200) << strict.body;
        Json::Value strict_body;
        ASSERT_TRUE(parse_json(strict.body, &strict_body));
        EXPECT_EQ(compact_json(strict_body["combined"]["timeseries"]),
                  compact_json(body["combined"]["timeseries"]));
    } else {
        EXPECT_TRUE(strict.status == 200 || strict.status == 400) << strict.body;
    }

    Json::Value ga = request;
    ga["missingClose"] = "carry";
    ga["popSize"] = 2;
    ga["generations"] = 1;
    const auto ga_response = http_post_json(server_->port(), "/api/ga/stream", ga);
    ASSERT_EQ(ga_response.status, 200) << ga_response.body;
    std::istringstream lines(ga_response.body);
    std::string line;
    Json::Value complete;
    while (std::getline(lines, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event)) << line;
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "complete") complete = event;
    }
    ASSERT_EQ(complete["type"].asString(), "complete") << ga_response.body;
    ASSERT_TRUE(complete.isMember("missingData"));
    EXPECT_TRUE(complete["batches"][0].isMember("missingData"));
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
    for (const char* key : {"threads", "signalPlotMaxCells", "collectSignalsTopK"}) {
        bad = request;
        bad[key] = static_cast<Json::Int64>(5000000000LL);
        const auto response = simulate(bad);
        EXPECT_EQ(response.status, 400) << key;
        EXPECT_NE(response.body.find(key), std::string::npos) << response.body;
        EXPECT_EQ(response.body.find("LargestInt"), std::string::npos) << response.body;
    }
}

TEST_F(HttpContractTest, RemovedSettlementRequestOptionsAreRejected) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    for (const char* key : {"defaultDelistingReturn", "staleQuoteSettleBars",
                            "settlementLog", "distributionLog"}) {
        Json::Value bad = request;
        bad[key] = (std::string(key).find("Log") != std::string::npos)
            ? Json::Value(true) : Json::Value(0);
        const auto response = simulate(bad);
        EXPECT_EQ(response.status, 400) << key << ": " << response.body;
        EXPECT_NE(response.body.find(key), std::string::npos) << response.body;
    }
    {
        Json::Value bad = request;
        bad["combinedMode"] = "raw";
        const auto response = simulate(bad);
        EXPECT_EQ(response.status, 400) << response.body;
        EXPECT_NE(response.body.find("combinedMode was removed"), std::string::npos) << response.body;
    }
    const auto normal = simulate(request);
    ASSERT_EQ(normal.status, 200) << normal.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(normal.body, &body));
    for (const char* key : {"settlementLog", "distributionLog",
                            "corporateActions", "pnlUnconfirmed"}) {
        EXPECT_FALSE(body.isMember(key)) << key;
        if (body["batches"].isArray() && !body["batches"].empty())
            EXPECT_FALSE(body["batches"][0].isMember(key)) << key;
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
    ASSERT_EQ(body["batches"].size(), 1u);
    EXPECT_NE(body["batches"][0]["xml"].asString().find("group=\"sector\""), std::string::npos);

    request["xml"] = "<data expr=\"close\"/><rank/><neutralize group=\"sector|industry\"/>";
    const auto piped = simulate(request);
    ASSERT_EQ(piped.status, 200) << piped.body;
    Json::Value expanded;
    ASSERT_TRUE(parse_json(piped.body, &expanded)) << piped.body;
    ASSERT_EQ(expanded["batches"].size(), 2u);
    std::set<std::string> groups;
    for (const Json::Value& batch : expanded["batches"]) {
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
    request["isStart"] = "2000-01-01"; request["osStart"] = "2100-01-01";
    request["firstSearch"] = 200; request["stopRound"] = 40; request["stopAlphas"] = 400;
    request["threads"] = 20; request["streamCandidates"] = true;
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
                EXPECT_FALSE(candidate.isMember("timeseries")); EXPECT_FALSE(candidate.isMember("signalPlot"));
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

TEST_F(HttpContractTest, LightweightDetailsAndSymbolSeriesKeepTheSameAccounting) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2000-01-01"; request["osStart"] = "2100-01-01";
    request["collectSignals"] = true;
    const auto full = simulate(request); ASSERT_EQ(full.status,200) << full.body;
    Json::Value baseline; ASSERT_TRUE(parse_json(full.body,&baseline));
    request["metricsOnly"] = true;
    const auto lean = simulate(request); ASSERT_EQ(lean.status,200) << lean.body;
    Json::Value details; ASSERT_TRUE(parse_json(lean.body,&details));
    EXPECT_EQ(details["timeseries"],baseline["timeseries"]); EXPECT_EQ(details["yearly"],baseline["yearly"]);
    EXPECT_FALSE(details.isMember("signalPlot")); EXPECT_FALSE(details.isMember("distribution"));
    request.removeMember("metricsOnly"); request["signalSeriesOnly"] = true;
    EXPECT_EQ(simulate(request).status,400);
    request["signalPlotSymbols"] = Json::Value(Json::arrayValue);
    request["signalPlotSymbols"].append(baseline["signalPlot"]["symbols"][0]);
    const auto series_response = simulate(request); ASSERT_EQ(series_response.status,200) << series_response.body;
    Json::Value series; ASSERT_TRUE(parse_json(series_response.body,&series));
    EXPECT_EQ(series["timeseries"],baseline["timeseries"]);
    EXPECT_EQ(series["signalPlot"]["symbols"].size(),1u);
    ASSERT_EQ(series["signalPlot"]["values"].size(),baseline["signalPlot"]["values"].size());
    for (Json::ArrayIndex row=0;row<series["signalPlot"]["values"].size();++row)
        EXPECT_EQ(series["signalPlot"]["values"][row][0],baseline["signalPlot"]["values"][row][0]);
    EXPECT_FALSE(series.isMember("distribution")); EXPECT_FALSE(series.isMember("signalDistributionLog"));
}

TEST_F(HttpContractTest, ConcreteCandidateAggregateMatchesOrdinaryCombinedPortfolio) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\" delay=\"0\"/><tsmean days=\"2|3|4\"/><rank/>";
    request["simStart"] = "2023-03-01"; request["simEnd"] = "2023-09-29";
    request["isStart"] = "2023-03-01"; request["osStart"] = "2023-06-01";
    request["metricsOnly"] = true;
    request["threads"] = 1;
    const auto baseline = simulate(request); ASSERT_EQ(baseline.status, 200) << baseline.body;
    Json::Value normal; ASSERT_TRUE(parse_json(baseline.body, &normal));
    // Sort by original expansion index, independent of performance order.
    std::vector<std::pair<int, std::string>> entries;
    for (const auto& entry : normal["batches"]) entries.emplace_back(entry["index"].asInt(), entry["xml"].asString());
    std::sort(entries.begin(), entries.end());
    request["xmls"] = Json::Value(Json::arrayValue);
    for (const auto& entry : entries) request["xmls"].append(entry.second);
    request["combinedOnly"] = true; request["threads"] = 20;
    const auto combined_response = simulate(request); ASSERT_EQ(combined_response.status, 200) << combined_response.body;
    Json::Value combined; ASSERT_TRUE(parse_json(combined_response.body, &combined));
    EXPECT_TRUE(combined["batches"].empty());
    EXPECT_EQ(combined["combined"], normal["combined"]);
    EXPECT_EQ(combined["timeseries"], normal["timeseries"]);
    EXPECT_EQ(combined["yearly"], normal["yearly"]);
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

// Same series as tools/make_http_test_fixture.py (float32 storage, day index from 2020-01-01).
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

TEST_F(AlphaStorageHttpTest, XmlLevelStoresEditorBytesAndToolbarMetadataOnly) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("xml");
    remove(name);
    const std::string xml = "<!-- keep me -->\r\n<data expr=\"close\" delay=\"1\"/>\n\t<tsmean days=\"5|10\"/>  \n"
                            "<choice><rank/><zscore/></choice>\n\xec\x95\x8c\xed\x8c\x8c\n";
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
    EXPECT_EQ(read_text(dir / "alpha.xml"), xml);
    EXPECT_EQ(read_text(dir / "metadata.yml"),
              "level: xml\nsimStart: 2024-01-01\nsimEnd: 2024-12-31\nisStart: 2024-01-01\n"
              "osStart: 2024-07-01\nuniverse: \"\"\nexecutionPrice: vwap\n");
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
        EXPECT_EQ(item["xml"].asString(), xml);
        EXPECT_EQ(item["level"].asString(), "xml");
        EXPECT_EQ(item["metadata"], alpha_metadata("vwap"));
    }
    EXPECT_TRUE(found);
    EXPECT_EQ(http_get(server_->port(), "/api/alphas/" + name + "/results").status, 404);

    request["xml"] = "<data expr=\"close\"/>";
    const auto duplicate = save(request);
    EXPECT_EQ(duplicate.status, 409) << duplicate.body;
    EXPECT_EQ(read_text(dir / "alpha.xml"), xml);

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
    variant("bad level", [](Json::Value& r) { r["level"] = "signals"; });
    variant("no metadata", [](Json::Value& r) { r.removeMember("metadata"); });
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
    const Json::Value metadata = alpha_metadata("ohlc4");
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
    ASSERT_EQ(index["rows"].size(), rows.size());
    Json::Value batch_request;
    batch_request["xmls"] = Json::Value(Json::arrayValue);
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto& [id, xml] = rows[i];
        Json::Value live_request;
        live_request["xml"] = xml;
        live_request["metricsOnly"] = true;
        const auto live = simulate(with_metadata(live_request, metadata));
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value expected, stored;
        ASSERT_TRUE(parse_json(live.body, &expected));
        const auto file = http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(id));
        ASSERT_EQ(file.status, 200) << file.body;
        EXPECT_EQ(file.body, read_text(dir / "results" / "rows" / (std::to_string(id) + ".json")));
        ASSERT_TRUE(parse_json(file.body, &stored));
        EXPECT_EQ(stored, expected) << "row " << id;
        const auto& entry = index["rows"][static_cast<Json::ArrayIndex>(i)];
        EXPECT_EQ(entry["index"].asInt(), id);
        EXPECT_EQ(entry["round"].asInt(), id == 17 ? 1 : 2);
        EXPECT_EQ(entry["xml"].asString(), xml);
        EXPECT_EQ(entry["origin"].asString(), "saved");
        EXPECT_EQ(entry["status"].asString(), "completed");
        EXPECT_EQ(entry["summary"], expected["batches"][0]["summary"]);
        EXPECT_EQ(entry["yearly"], expected["yearly"]);
        batch_request["xmls"].append(xml);
    }
    batch_request["combinedOnly"] = true;
    batch_request["metricsOnly"] = true;
    const auto live_batch = simulate(with_metadata(batch_request, metadata));
    ASSERT_EQ(live_batch.status, 200) << live_batch.body;
    const auto stored_batch = http_get(server_->port(), "/api/alphas/" + name + "/results/batch");
    ASSERT_EQ(stored_batch.status, 200) << stored_batch.body;
    Json::Value expected_batch, batch;
    ASSERT_TRUE(parse_json(live_batch.body, &expected_batch));
    ASSERT_TRUE(parse_json(stored_batch.body, &batch));
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
    const Json::Value metadata = alpha_metadata("close");
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
        book[dates->Value(i)][id[0] - 'A'] = w;
    }
    EXPECT_EQ(book.size(), static_cast<std::size_t>(last_day - first_day + 1));
    for (const auto& [day, positions] : book) {
        double gross = 0.0;
        for (const auto& [symbol, w] : positions) gross += std::fabs(w);
        EXPECT_NEAR(gross, 1.0, 1e-12) << day;
    }

    // Close execution, no missing quotes, ordinary panel (simulator value_holdings + execute):
    // gross_ret[t+1] = sum_i w[t,i] * (close[t+1,i] / close[t,i] - 1).
    Json::Value row_file;
    ASSERT_TRUE(parse_json(read_text(dir / "results" / "rows" / "5.json"), &row_file));
    const Json::Value& cum = row_file["timeseries"]["cumPnlGross"];
    ASSERT_EQ(cum.size(), static_cast<Json::ArrayIndex>(last_day - first_day + 1));
    double previous = cum[0].asDouble();
    EXPECT_EQ(previous, 0.0);  // nothing is held into the first trading bar
    std::size_t checked = 0;
    for (int32_t day = first_day + 1; day <= last_day; ++day) {
        const double gross = cum[static_cast<Json::ArrayIndex>(day - first_day)].asDouble() - previous;
        previous += gross;
        double expected = 0.0;
        for (const auto& [symbol, w] : book[day - 1]) {
            const long t = day - epoch_2020;
            expected += w * (fixture_close(symbol, t) / fixture_close(symbol, t - 1) - 1.0);
        }
        EXPECT_NEAR(gross, expected, 1e-12) << day;
        ++checked;
    }
    EXPECT_EQ(checked, 365u);
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}

TEST(AlphaStorageStartup, UnwritableAlphasDirFailsStartupWithAClearMessage) {
    const fs::path bin = find_server_binary();
    const fs::path cfg = find_config_path();
    if (bin.empty() || cfg.empty()) GTEST_SKIP() << "server binary or config not found";
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
    EXPECT_EQ(index["rows"][0]["index"].asUInt64(), max_id);
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
    request["isStart"] = "2000-01-01"; request["osStart"] = "2100-01-01";
    request["metricsOnly"] = true;
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
    EXPECT_EQ(body["batches"].size(), 1u);
    EXPECT_EQ(compact_json(body["batches"]), compact_json(omitted_body["batches"]));

    Json::Value ga;
    ga["xml"] = "<data expr=\"close\"/><tsmean days=\"1...4\"/>";
    ga["isStart"] = "2000-01-01"; ga["osStart"] = "2100-01-01";
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
    for (int i = 0; i < 300; ++i) xml += "<tsrank days=\"2000\" min_count=\"1\"/>";
    Json::Value request;
    request["xml"] = xml;
    request["isStart"] = "2000-01-01"; request["osStart"] = "2100-01-01";
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
    request["isStart"] = "2000-01-01";
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
    combined_request["metricsOnly"] = true;
    const auto combined = simulate(combined_request);
    ASSERT_EQ(combined.status, 200) << combined.body;
    Json::Value meta_body, combined_body;
    ASSERT_TRUE(parse_json(meta.body, &meta_body));
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    const auto& batch = meta_body["batches"][0];
    const auto& expected = combined_body["combined"];
    EXPECT_EQ(batch["meta"], sample_meta_list());
    for (const char* key : {"summary", "yearly", "timeseries", "missingData", "isStartIndex", "osStartIndex"})
        EXPECT_EQ(batch[key], expected[key]) << key;
    EXPECT_EQ(meta_body["timeseries"], combined_body["timeseries"]);
    EXPECT_TRUE(meta_body.isMember("signalPlot"));
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
    EXPECT_EQ(b["batches"][0]["meta"], sample_meta_list());
    EXPECT_NE(a["batches"][0]["summary"], b["batches"][0]["summary"]);
    // An ordinary alpha carries no meta key.
    const auto ordinary = simulate(meta_request("<data expr=\"close\" delay=\"1\"/><rank/>"));
    Json::Value c;
    ASSERT_TRUE(parse_json(ordinary.body, &c));
    EXPECT_FALSE(c["batches"][0].isMember("meta"));
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
    a["batches"][0].removeMember("xml");
    b["batches"][0].removeMember("xml");
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
    ASSERT_EQ(body["batches"].size(), 2u);
    EXPECT_EQ(body["batches"][0]["summary"], body["batches"][1]["summary"]);
    EXPECT_EQ(body["batches"][0]["meta"], sample_meta_list());
    EXPECT_EQ(body["batches"][1]["meta"], sample_meta_list());
    const auto single = simulate(meta_request("<meta expr=\"sample\"/>"));
    Json::Value single_body;
    ASSERT_TRUE(parse_json(single.body, &single_body));
    EXPECT_EQ(body["batches"][0]["summary"], single_body["batches"][0]["summary"]);
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
    request["firstSearch"] = 2; request["stopRound"] = 1; request["streamCandidates"] = true;
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
        live_request["metricsOnly"] = true;
        const auto live = simulate(live_request);
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value body;
        ASSERT_TRUE(parse_json(live.body, &body));
        EXPECT_EQ(candidate["summary"], body["batches"][0]["summary"]) << candidate["xml"].asString();
    }
}

TEST_F(AlphaStorageHttpTest, MetaSaveResultsLevel) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::string name = unique_alpha_name("meta");
    remove(name);
    const Json::Value metadata = alpha_metadata();
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
    const auto index_response = http_get(server_->port(), "/api/alphas/" + name + "/results");
    ASSERT_EQ(index_response.status, 200) << index_response.body;
    Json::Value index;
    ASSERT_TRUE(parse_json(index_response.body, &index));
    ASSERT_EQ(index["rows"].size(), 2u);
    for (std::size_t i = 0; i < xmls.size(); ++i) {
        Json::Value live_request;
        live_request["xml"] = xmls[i];
        live_request["metricsOnly"] = true;
        const auto live = simulate(with_metadata(live_request, metadata));
        ASSERT_EQ(live.status, 200) << live.body;
        Json::Value expected, stored;
        ASSERT_TRUE(parse_json(live.body, &expected));
        EXPECT_EQ(expected["batches"][0]["meta"], sample_meta_list());
        EXPECT_EQ(index["rows"][static_cast<Json::ArrayIndex>(i)]["summary"], expected["batches"][0]["summary"]);
        const auto file = http_get(server_->port(), "/api/alphas/" + name + "/results/rows/" + std::to_string(i));
        ASSERT_TRUE(parse_json(file.body, &stored));
        EXPECT_EQ(stored, expected);
    }
    EXPECT_EQ(http_delete(server_->port(), "/api/alphas/" + name).status, 200);
}

TEST_F(HttpContractTest, MetaInGAStreamNonStreamingBest) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"sample\"/><neutralize group=\"sector|industry\"/>");
    request["firstSearch"] = 2; request["stopRound"] = 1; request["threads"] = 4;
    const auto response = http_post_json(server_->port(), "/api/ga/stream", request);
    ASSERT_EQ(response.status, 200) << response.body;
    std::istringstream stream(response.body);
    std::string line;
    Json::Value complete;
    while (std::getline(stream, line)) {
        if (line.empty()) continue;
        Json::Value event;
        ASSERT_TRUE(parse_json(line, &event)) << line;
        ASSERT_NE(event["type"].asString(), "error") << line;
        if (event["type"].asString() == "complete") complete = event;
    }
    ASSERT_EQ(complete["batches"].size(), 1u) << response.body.substr(0, 300);
    const auto& batch = complete["batches"][0];
    EXPECT_EQ(batch["meta"], sample_meta_list());
    Json::Value live_request = meta_request(complete["bestXml"].asString());
    live_request["metricsOnly"] = true;
    const auto live = simulate(live_request);
    ASSERT_EQ(live.status, 200) << live.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(live.body, &body));
    EXPECT_EQ(batch["summary"], body["batches"][0]["summary"]);
}

TEST_F(HttpContractTest, MetaDisconnectStopsMembers) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"heavy\"/>");
    request["metricsOnly"] = true;
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
    const auto& meta = a["batches"][0]["meta"];
    ASSERT_EQ(meta.size(), 1u);
    EXPECT_EQ(meta[0]["name"].asString(), "sample");
    EXPECT_EQ(meta[0]["members"].asUInt(), 3u);
    EXPECT_EQ(meta[0]["combine"].asString(), mvo_key(60, "0.5"));
    ASSERT_TRUE(meta[0].isMember("fallbackBars"));
    ASSERT_TRUE(meta[0].isMember("firstEstimatedBar"));
    EXPECT_EQ(meta[0]["firstEstimatedBar"].asUInt(), a["isStartIndex"].asUInt() + 60) << compact_json(meta);
    std::cout << "[ meta mvo ] " << compact_json(meta) << "\n";
    EXPECT_NE(a["batches"][0]["summary"], b["batches"][0]["summary"]);
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
        const auto& meta = body["batches"][0]["meta"][0];
        EXPECT_EQ(meta["combine"].asString(), key) << tag;
        EXPECT_EQ(meta["members"].asUInt(), name == "ten" ? 10u : 3u);
        EXPECT_TRUE(meta.isMember("fallbackBars")) << tag;
        EXPECT_TRUE(std::isfinite(body["batches"][0]["summary"]["is"]["sharpe"].asDouble())) << tag;
    }
}

TEST_F(HttpContractTest, MetaSpecErrors400) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const std::vector<std::pair<std::string, std::string>> cases = {
        {"<meta expr=\"sample\"/><spca days=\"60\" k=\"4\"/>", "meta 'sample' <spca>: k=4 exceeds the member count 3"},
        {"<meta expr=\"sample\"/><mvo days=\"5000\"/>", "meta 'sample' <mvo>: days=5000 exceeds the trading bars"},
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

TEST_F(HttpContractTest, MetaInGAStreamWithMvoGrid) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request = meta_request("<meta expr=\"sample\"/><mvo days=\"40|60\" shrink=\"0.3|0.7\"/>");
    // Dates used by no other test: the member-return cache is process-wide.
    request["simStart"] = "2021-03-01";
    request["simEnd"] = "2022-11-30";
    request["firstSearch"] = 4; request["stopRound"] = 1; request["streamCandidates"] = true;
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
    for (const char* key : {"firstSearch", "stopRound", "streamCandidates", "metaStats"}) live_request.removeMember(key);
    live_request["xml"] = candidates[0]["xml"];
    live_request["metricsOnly"] = true;
    const auto live = simulate(live_request);
    ASSERT_EQ(live.status, 200) << live.body.substr(0, 300);
    Json::Value body;
    ASSERT_TRUE(parse_json(live.body, &body));
    EXPECT_EQ(candidates[0]["summary"], body["batches"][0]["summary"]);
    EXPECT_EQ(candidates[0]["meta"], body["batches"][0]["meta"]);
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
    for (int64_t i = 0; i < table->num_rows(); ++i) book[dates->Value(i)][ids->GetString(i)[0] - 'A'] = weights->Value(i);
    return book;
}
}  // namespace

TEST_F(AlphaStorageHttpTest, MetaMvoMatchesOracleOnFixture) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const Json::Value metadata = alpha_metadata("close");
    const int32_t first_day = 19723, last_day = 20088;  // 2024-01-01, 2024-12-31
    const unsigned T = static_cast<unsigned>(last_day - first_day + 1);
    std::vector<std::string> xmls(kSampleMembers.begin(), kSampleMembers.end());
    xmls.push_back("<meta expr=\"sample\"/><mvo days=\"60\"/>");
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
        ASSERT_EQ(books.back().size(), T) << xmls[k];
        if (k == 3) continue;
        Json::Value row_file;
        ASSERT_TRUE(parse_json(read_text(alphas_dir() / name / "results" / "rows" / "0.json"), &row_file));
        const Json::Value& cum = row_file["timeseries"]["cumPnlGross"];
        ASSERT_EQ(cum.size(), T);
        for (unsigned t = 0; t < T; ++t)
            returns.rows[t][k] = cum[t].asDouble() - (t ? cum[t - 1].asDouble() : 0.0);
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
            const double diff = std::fabs(engine - combined[i] / gross);
            max_diff = std::max(max_diff, diff);
            EXPECT_NEAR(engine, combined[i] / gross, 1e-6) << "day " << day << " symbol " << i;
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
    request["metricsOnly"] = true;
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

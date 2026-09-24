#include <gtest/gtest.h>

#include <algorithm>
#include <csignal>
#include <cstdlib>
#include <cstdio>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <json/json.h>
#include <limits>
#include <set>
#include <sstream>
#include <string>
#include <stdexcept>
#include <chrono>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
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

HttpResult http_post_json(int port, const std::string& path, const Json::Value& payload)
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
        << " --data-binary @" << shell_quote(payload_file.string())
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
    fs::remove(payload_file, ec);
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

        server_ = std::make_unique<ServerProcess>(bin, cfg, test_port());
        if (!server_->start()) {
            available_ = false;
            skip_reason_ = "failed to start server (likely sandbox/network restriction)";
            return;
        }
        available_ = true;
    }

    static void TearDownTestSuite()
    {
        if (server_ != nullptr)
            server_->stop();
        server_.reset();
    }

    static HttpResult simulate(const Json::Value& payload)
    {
        return http_post_json(server_->port(), "/api/simulate", payload);
    }

    static std::unique_ptr<ServerProcess> server_;
    static bool available_;
    static std::string skip_reason_;
};

std::unique_ptr<ServerProcess> HttpContractTest::server_;
bool HttpContractTest::available_ = false;
std::string HttpContractTest::skip_reason_;

}  // namespace

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

TEST_F(HttpContractTest, CombinedModesAndTopSignalPayloadsAreExplicit) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3|4\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    request["collectSignals"] = true;
    request["collectSignalsTopK"] = 1;
    request["signalPlotMaxCells"] = 12;
    for (const char* mode : {"normalized", "raw"}) {
        request["combinedMode"] = mode;
        const auto response = simulate(request);
        ASSERT_EQ(response.status, 200) << response.body;
        Json::Value body;
        ASSERT_TRUE(parse_json(response.body, &body));
        EXPECT_EQ(body["combinedMode"].asString(), mode);
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
    request["combinedMode"] = "misspelled";
    EXPECT_EQ(simulate(request).status, 400);
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


TEST_F(HttpContractTest, D0AndD1UseCurrentAndPreviousSignalTargets) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2000-01-01";
    request["osStart"] = "2100-01-01";
    request["mode"] = 0;
    auto call = [&](const Json::Value& payload) {
        const auto response = simulate(payload);
        EXPECT_EQ(response.status, 200) << response.body;
        Json::Value body;
        EXPECT_TRUE(parse_json(response.body, &body));
        return body;
    };
    const auto initial = call(request);
    EXPECT_EQ(initial["timeseries"]["timestamps"][0], initial["signalPlot"]["timestamps"][0]);
    ASSERT_GT(initial["timeseries"]["timestamps"].size(), 3u);
    request["simStart"] = initial["timeseries"]["timestamps"][3];
    const auto d0 = call(request);
    request["mode"] = 1;
    const auto d1 = call(request);
    request["xml"] = "<data expr=\"close\" delay=\"1\"/>";
    const auto previous_signal = call(request);
    auto normalized_max = [](const Json::Value& row) {
        double gross = 0.0, maximum = 0.0;
        for (const auto& value : row) {
            if (value.isNull()) continue;
            const double absolute = std::fabs(value.asDouble());
            gross += absolute;
            maximum = std::max(maximum, absolute);
        }
        return 100.0 * maximum / gross;
    };
    EXPECT_EQ(d0["mode"].asInt(), 0);
    EXPECT_EQ(d1["mode"].asInt(), 1);
    EXPECT_NEAR(d0["timeseries"]["maxWeight"][0].asDouble(), normalized_max(d0["signalPlot"]["values"][0]), 1e-8);
    EXPECT_NEAR(d1["timeseries"]["maxWeight"][0].asDouble(), normalized_max(previous_signal["signalPlot"]["values"][0]), 1e-8);
    for (const Json::Value& mode : {Json::Value(-1), Json::Value(2), Json::Value(1.5)}) {
        request["mode"] = mode;
        EXPECT_EQ(simulate(request).status, 400);
    }
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
// <neutralize group="..."/> compiles against (docs/neutralize_plan_2026-09-16.md 4.4 f).
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


// Borrow cost reporting.
TEST_F(HttpContractTest, ConfigAndSimulateReportTheBorrowCostModule) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto config_response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(config_response.status, 200) << config_response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(config_response.body, &config)) << config_response.body;
    ASSERT_TRUE(config.isMember("costs")) << config_response.body;
    const Json::Value& borrow = config["costs"]["borrow"];
    ASSERT_TRUE(config["costs"]["reportUnapplied"].isBool());
    ASSERT_TRUE(borrow["enabled"].isBool());
    ASSERT_TRUE(borrow["applied"].isBool());
    ASSERT_TRUE(borrow["defaultRate"].isNull() || borrow["defaultRate"].isNumeric());
    EXPECT_GT(borrow["dayCount"].asUInt64(), 0u);
    ASSERT_TRUE(borrow["path"].isNull() || borrow["path"].isString());
    for (const char* key : {"tableRows", "tableRowsMatched", "symbolsRated"})
        ASSERT_TRUE(borrow[key].isNumeric()) << key;
    EXPECT_LE(borrow["tableRowsMatched"].asUInt64(), borrow["tableRows"].asUInt64());
    EXPECT_LE(borrow["symbolsRated"].asUInt64(), borrow["tableRowsMatched"].asUInt64());
    if (!borrow["enabled"].asBool()) EXPECT_FALSE(borrow["applied"].asBool());
    if (borrow["applied"].asBool())
        EXPECT_TRUE(borrow["enabled"].asBool() &&
                    (borrow["symbolsRated"].asUInt64() > 0 || borrow["defaultRate"].isNumeric()));

    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body)) << response.body;
    ASSERT_TRUE(body.isMember("costs"));
    ASSERT_TRUE(body["batches"][0].isMember("costs"));
    EXPECT_EQ(compact_json(body["costs"]), compact_json(body["batches"][0]["costs"]));
    const Json::Value& costs = body["costs"];
    ASSERT_TRUE(costs["borrowEnabled"].isBool());
    ASSERT_TRUE(costs["borrowApplied"].isBool());
    ASSERT_TRUE(costs["borrowCost"].isNumeric());
    EXPECT_EQ(costs["borrowEnabled"].asBool(), borrow["enabled"].asBool());
    EXPECT_EQ(costs["borrowApplied"].asBool(), borrow["applied"].asBool());
    if (config["costs"]["reportUnapplied"].asBool()) {
        ASSERT_TRUE(costs["borrowUnratedShortBars"].isNumeric());
        EXPECT_EQ(costs.isMember("borrowUnapplied"), !costs["borrowApplied"].asBool());
    } else {
        EXPECT_FALSE(costs.isMember("borrowUnratedShortBars"));
        EXPECT_FALSE(costs.isMember("borrowUnapplied"));
    }
    // The per-bar series covers the trading span and sums to the total; a long-only
    // book (rank of close) borrows nothing, so the net series equals the fixed book.
    const Json::Value& timeseries = body["timeseries"];
    ASSERT_TRUE(timeseries.isMember("borrowCost"));
    ASSERT_EQ(timeseries["borrowCost"].size(), timeseries["timestamps"].size());
    double total = 0.0;
    for (Json::ArrayIndex i = 0; i < timeseries["borrowCost"].size(); ++i) {
        EXPECT_GE(timeseries["borrowCost"][i].asDouble(), 0.0);
        total += timeseries["borrowCost"][i].asDouble();
    }
    EXPECT_NEAR(total, costs["borrowCost"].asDouble(), 1e-12);
    EXPECT_EQ(total, 0.0);
    EXPECT_NEAR(timeseries["cumPnlGross"][timeseries["cumPnlGross"].size() - 1].asDouble() -
                timeseries["cumPnl"][timeseries["cumPnl"].size() - 1].asDouble(),
                0.0 + [&] { double fee = 0.0; for (const auto& t : timeseries["turnover"]) fee += t.asDouble() / 100.0 * config["feeRate"].asDouble(); return fee; }(),
                1e-9);
    // A fully short book pays: the charge is positive and gross - net = fee + borrow.
    if (costs["borrowApplied"].asBool()) {
        request["xml"] = "<data expr=\"-close\"/>";
        const auto shorts = simulate(request);
        ASSERT_EQ(shorts.status, 200) << shorts.body;
        Json::Value short_body;
        ASSERT_TRUE(parse_json(shorts.body, &short_body));
        const Json::Value& series = short_body["timeseries"];
        double borrow_sum = 0.0, fee_sum = 0.0;
        for (Json::ArrayIndex i = 0; i < series["borrowCost"].size(); ++i) {
            borrow_sum += series["borrowCost"][i].asDouble();
            fee_sum += series["turnover"][i].asDouble() / 100.0 * config["feeRate"].asDouble();
        }
        EXPECT_GT(short_body["costs"]["borrowCost"].asDouble(), 0.0);
        EXPECT_NEAR(borrow_sum, short_body["costs"]["borrowCost"].asDouble(), 1e-12);
        EXPECT_NEAR(series["cumPnlGross"][series["cumPnlGross"].size() - 1].asDouble() -
                    series["cumPnl"][series["cumPnl"].size() - 1].asDouble(),
                    fee_sum + borrow_sum, 1e-9);
    }
    // Multi-alpha and combined results carry the same summary.
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    const auto combined = simulate(request);
    ASSERT_EQ(combined.status, 200) << combined.body;
    Json::Value combined_body;
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    ASSERT_TRUE(combined_body["combined"].isMember("costs"));
    EXPECT_EQ(compact_json(combined_body["costs"]), compact_json(combined_body["combined"]["costs"]));
}

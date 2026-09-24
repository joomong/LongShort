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


TEST_F(HttpContractTest, ConfigPublishesMissingCloseAccountingSettings) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(response.body, &config)) << response.body;
    ASSERT_TRUE(config.isMember("missingClosePolicy"));
    const std::string policy = config["missingClosePolicy"].asString();
    EXPECT_TRUE(policy == "carry" || policy == "error") << policy;
    ASSERT_TRUE(config.isMember("defaultDelistingReturn"));
    EXPECT_TRUE(std::isfinite(config["defaultDelistingReturn"].asDouble()));
    ASSERT_TRUE(config.isMember("staleQuoteSettleBars"));
    EXPECT_GE(config["staleQuoteSettleBars"].asInt64(), 0);
    ASSERT_TRUE(config.isMember("eventsLoaded"));
    ASSERT_TRUE(config.isMember("settlementSymbols"));
    ASSERT_TRUE(config.isMember("successorEvents"));
    EXPECT_LE(config["successorEvents"].asUInt64(), config["eventsLoaded"].asUInt64());
    // The three guard counts partition the event rows this panel matched.
    ASSERT_TRUE(config.isMember("eventsPriceChecks"));
    ASSERT_TRUE(config.isMember("eventsPriceChecksOutOfRange"));
    ASSERT_TRUE(config.isMember("eventsPriceChecksNotEvaluable"));
    const auto guard_rows = config["eventsPriceChecks"].asUInt64() +
                            config["eventsPriceChecksOutOfRange"].asUInt64() +
                            config["eventsPriceChecksNotEvaluable"].asUInt64();
    // With anchors configured the three counts partition every matched event row,
    // one bucket per row, whether or not this panel reaches the settlement. A
    // config that declares no anchors leaves all three at zero.
    EXPECT_GE(config["eventsLoaded"].asUInt64(), config["settlementSymbols"].asUInt64());
    if (guard_rows > 0) {
        EXPECT_EQ(guard_rows, config["eventsLoaded"].asUInt64());
    } else {
        EXPECT_EQ(config["eventsPriceChecks"].asUInt64(), 0u);
        EXPECT_EQ(config["eventsPriceChecksOutOfRange"].asUInt64(), 0u);
    }
    EXPECT_LE(config["settlementSymbols"].asUInt64(), config["eventsLoaded"].asUInt64());
    EXPECT_LE(config["eventsLoaded"].asUInt64(), config["N"].asUInt64());
}

TEST_F(HttpContractTest, SimulateReportsMissingDataTotalsAndFrozenSeries) {
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
                            "missingEntries", "resumedHoldings", "delistings", "delistingPnl",
                            "settledGross", "defaultDelistings", "defaultDelistingGross",
                            "staleSettlements", "terminalFrozenGross", "frozenGrossSum",
                            "retirements", "cancelledDelistingEntries",
                            "postEndDateQuoteBars", "pendingSettlements",
                            "successorConversions", "convertedGross",
                            "computedSettlementReturns"}) {
        ASSERT_TRUE(totals.isMember(key)) << key;
        EXPECT_TRUE(totals[key].isNumeric()) << key;
    }
    // The key set is part of the contract: a silently dropped key must fail here.
    EXPECT_EQ(totals.getMemberNames().size(), 20u) << compact_json(totals);
    EXPECT_LE(totals["defaultDelistings"].asUInt64(), totals["delistings"].asUInt64());
    EXPECT_LE(totals["staleSettlements"].asUInt64(), totals["delistings"].asUInt64());
    EXPECT_LE(totals["missingEntries"].asUInt64(), totals["blockedOrders"].asUInt64());
    // A conversion is a settled holding that moved instead of being cashed out.
    EXPECT_LE(totals["successorConversions"].asUInt64(), totals["delistings"].asUInt64());
    EXPECT_LE(totals["computedSettlementReturns"].asUInt64(), totals["delistings"].asUInt64());
    // Every settled holding is a retirement; unheld names retire without settling.
    EXPECT_LE(totals["delistings"].asUInt64(), totals["retirements"].asUInt64());
    // Every scheduled event either retired inside the panel or is still pending;
    // the remaining retirements are the stale-quote ones, which carry no end date.
    const auto config_response = http_get(server_->port(), "/api/config");
    Json::Value config;
    ASSERT_EQ(config_response.status, 200);
    ASSERT_TRUE(parse_json(config_response.body, &config));
    const auto event_retirements = static_cast<Json::Int64>(totals["retirements"].asUInt64()) -
                                   static_cast<Json::Int64>(totals["staleSettlements"].asUInt64());
    EXPECT_GE(event_retirements, 0);
    EXPECT_EQ(event_retirements + static_cast<Json::Int64>(totals["pendingSettlements"].asUInt64()),
              static_cast<Json::Int64>(config["settlementSymbols"].asUInt64()));

    const Json::Value& timeseries = body["timeseries"];
    ASSERT_TRUE(timeseries.isMember("frozenGross"));
    ASSERT_TRUE(timeseries.isMember("settledGross"));
    ASSERT_EQ(timeseries["frozenGross"].size(), timeseries["timestamps"].size());
    ASSERT_EQ(timeseries["settledGross"].size(), timeseries["timestamps"].size());
    // The per-bar series and the reported totals describe the same trading span.
    double settled_sum = 0.0;
    for (Json::ArrayIndex i = 0; i < timeseries["settledGross"].size(); ++i) {
        EXPECT_GE(timeseries["frozenGross"][i].asDouble(), 0.0);
        EXPECT_GE(timeseries["settledGross"][i].asDouble(), 0.0);
        settled_sum += timeseries["settledGross"][i].asDouble();
    }
    double frozen_sum = 0.0;
    for (Json::ArrayIndex i = 0; i < timeseries["frozenGross"].size(); ++i)
        frozen_sum += timeseries["frozenGross"][i].asDouble();
    EXPECT_NEAR(settled_sum, totals["settledGross"].asDouble(), 1e-9);
    EXPECT_NEAR(frozen_sum, totals["frozenGrossSum"].asDouble(), 1e-9);
    ASSERT_FALSE(timeseries["frozenGross"].empty());
    EXPECT_NEAR(timeseries["frozenGross"][timeseries["frozenGross"].size() - 1].asDouble(),
                totals["terminalFrozenGross"].asDouble(), 1e-12);
}

TEST_F(HttpContractTest, CombinedAndGaShareTheMissingCloseOptions) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    request["defaultDelistingReturn"] = -1.0;
    request["staleQuoteSettleBars"] = 60;
    request["missingClose"] = "carry";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body)) << response.body;
    ASSERT_TRUE(body["combined"].isMember("missingData"));
    EXPECT_TRUE(body["combined"]["missingData"]["delistings"].isNumeric());
    EXPECT_EQ(compact_json(body["missingData"]), compact_json(body["combined"]["missingData"]));
    for (Json::ArrayIndex i = 0; i < body["batches"].size(); ++i)
        EXPECT_TRUE(body["batches"][i].isMember("missingData"));

    // With nothing missing anywhere the two policies must agree exactly. Once a
    // close is absent they legitimately diverge: carry keeps the quoteless name in
    // the per-alpha normalization, and it may also refuse the run outright.
    const Json::Value& observed = body["missingData"];
    const bool quiet = observed["missingHeldBars"].asUInt64() == 0 &&
                       observed["blockedOrders"].asUInt64() == 0 &&
                       observed["missingEntries"].asUInt64() == 0 &&
                       observed["retirements"].asUInt64() == 0 &&
                       observed["postEndDateQuoteBars"].asUInt64() == 0;
    request["missingClose"] = "error";
    const auto strict = simulate(request);
    if (quiet) {
        ASSERT_EQ(strict.status, 200) << strict.body;
        Json::Value strict_body;
        ASSERT_TRUE(parse_json(strict.body, &strict_body));
        EXPECT_EQ(compact_json(strict_body["combined"]["timeseries"]),
                  compact_json(body["combined"]["timeseries"]));
    } else {
        ASSERT_TRUE(strict.status == 200 || strict.status == 400) << strict.body;
        if (strict.status == 200) {
            Json::Value strict_body;
            ASSERT_TRUE(parse_json(strict.body, &strict_body));
            // The settlement schedule itself does not depend on the policy.
            const Json::Value& strict_totals = strict_body["missingData"];
            EXPECT_EQ(strict_totals["pendingSettlements"].asUInt64(),
                      observed["pendingSettlements"].asUInt64());
            EXPECT_EQ(strict_totals["postEndDateQuoteBars"].asUInt64(),
                      observed["postEndDateQuoteBars"].asUInt64());
        }
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

TEST_F(HttpContractTest, InvalidMissingCloseOptionsAreRejected) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    Json::Value bad = request;
    bad["missingClose"] = "forward-fill";
    EXPECT_EQ(simulate(bad).status, 400);
    bad = request;
    bad["defaultDelistingReturn"] = "minus one";
    EXPECT_EQ(simulate(bad).status, 400);
    bad = request;
    bad["staleQuoteSettleBars"] = -3;
    EXPECT_EQ(simulate(bad).status, 400);
    bad = request;
    bad["staleQuoteSettleBars"] = 1.5;
    EXPECT_EQ(simulate(bad).status, 400);
    // A total return outside [-1, 1] cannot be multiplied into a book weight.
    for (double outside : {-5.0, -1.0000001, 3.0}) {
        bad = request;
        bad["defaultDelistingReturn"] = outside;
        EXPECT_EQ(simulate(bad).status, 400) << outside;
    }
    for (double inside : {-1.0, 0.0, 1.0}) {
        Json::Value good = request;
        good["defaultDelistingReturn"] = inside;
        EXPECT_EQ(simulate(good).status, 200) << inside;
    }
    // Out-of-range unsigned options answer with our own message, not jsoncpp's.
    for (const char* key : {"staleQuoteSettleBars", "threads", "signalPlotMaxCells",
                            "collectSignalsTopK"}) {
        bad = request;
        bad[key] = static_cast<Json::Int64>(5000000000LL);
        const auto response = simulate(bad);
        EXPECT_EQ(response.status, 400) << key;
        EXPECT_NE(response.body.find(key), std::string::npos) << response.body;
        EXPECT_EQ(response.body.find("LargestInt"), std::string::npos) << response.body;
    }
}

TEST_F(HttpContractTest, SettlementLogIsOptInAndAddsUpToTheReportedTotals) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";

    const auto quiet = simulate(request);
    ASSERT_EQ(quiet.status, 200) << quiet.body;
    Json::Value quiet_body;
    ASSERT_TRUE(parse_json(quiet.body, &quiet_body));
    EXPECT_FALSE(quiet_body.isMember("settlementLog"));
    EXPECT_FALSE(quiet_body.isMember("settlementLogUnheld"));
    EXPECT_FALSE(quiet_body["batches"][0].isMember("settlementLog"));

    request["settlementLog"] = true;
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    ASSERT_TRUE(body.isMember("settlementLog"));
    ASSERT_TRUE(body["settlementLog"].isArray());
    ASSERT_TRUE(body.isMember("settlementLogUnheld"));
    EXPECT_EQ(compact_json(body["missingData"]), compact_json(quiet_body["missingData"]))
        << "asking for the log must not change the accounting";

    const Json::Value& totals = body["missingData"];
    const Json::Value& log = body["settlementLog"];
    EXPECT_EQ(log.size(), totals["delistings"].asUInt64());
    EXPECT_EQ(log.size() + body["settlementLogUnheld"].asUInt64(),
              totals["delistings"].asUInt64() + body["settlementLogUnheld"].asUInt64());
    double settled_pnl = 0.0, default_exposure = 0.0;
    for (Json::ArrayIndex i = 0; i < log.size(); ++i) {
        const Json::Value& entry = log[i];
        for (const char* key : {"symbol", "time", "bar", "exposure", "return", "source",
                                "cashShare", "stockShare", "successor", "transferred"})
            ASSERT_TRUE(entry.isMember(key)) << key;
        EXPECT_FALSE(entry["symbol"].asString().empty());
        EXPECT_EQ(entry["time"].asString().size(), 10u);
        EXPECT_NE(entry["exposure"].asDouble(), 0.0);   // unheld retirements stay out
        const std::string source = entry["source"].asString();
        EXPECT_TRUE(source == "computed" || source == "explicit" || source == "default") << source;
        settled_pnl += entry["exposure"].asDouble() * entry["return"].asDouble();
        if (source == "default") default_exposure += std::fabs(entry["exposure"].asDouble());
    }
    EXPECT_NEAR(settled_pnl, totals["delistingPnl"].asDouble(), 1e-9);
    EXPECT_NEAR(default_exposure, totals["defaultDelistingGross"].asDouble(), 1e-9);
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


// ── Corporate-action ledger (docs/corporate_actions_engine_2026-09-17.md) ────────

TEST_F(HttpContractTest, ConfigPublishesTheCorporateActionsLedgerReport) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value config;
    ASSERT_TRUE(parse_json(response.body, &config)) << response.body;
    ASSERT_TRUE(config.isMember("corporateActions")) << response.body;
    const Json::Value& ledger = config["corporateActions"];
    for (const char* key : {"rows", "rowsMatched", "rowsQuantity", "rowsUnresolved", "legs",
                            "legsEstimated", "legsUnbookable", "legsValueOnly", "legsRights", "legsAfterParentEnd", "legsOutOfRange",
                            "unpricedChildren", "priceChecks", "priceChecksNotEvaluable",
                            "rightsWithoutExpiry"})
        ASSERT_TRUE(ledger.isMember(key) && ledger[key].isNumeric()) << key;
    const std::string unresolved_policy = ledger["unresolvedPolicy"].asString();
    EXPECT_TRUE(unresolved_policy == "warn" || unresolved_policy == "strict") << unresolved_policy;
    EXPECT_LE(ledger["legsUnbookable"].asUInt64(), ledger["legs"].asUInt64());
    EXPECT_LE(ledger["legsUnbookable"].asUInt64(), ledger["rowsUnresolved"].asUInt64());
    ASSERT_TRUE(ledger["loaded"].isBool());
    ASSERT_TRUE(ledger["rightsTermsLoaded"].isBool());
    ASSERT_TRUE(ledger["autoExercise"].isBool());
    EXPECT_FALSE(ledger["autoExercise"].asBool());   // true is refused at startup
    EXPECT_LE(ledger["rightsWithoutExpiry"].asUInt64(), ledger["legsRights"].asUInt64());
    ASSERT_TRUE(ledger["applyDividends"].isBool());
    ASSERT_TRUE(ledger["applyDistributions"].isBool());
    ASSERT_TRUE(ledger["staleCheck"].isBool());
    const std::string policy = ledger["unpricedChild"].asString();
    EXPECT_TRUE(policy == "pending" || policy == "drop") << policy;
    // The guard counts partition the scheduled legs; every leg comes from a matched row.
    EXPECT_EQ(ledger["priceChecks"].asUInt64() + ledger["priceChecksNotEvaluable"].asUInt64(),
              ledger["legs"].asUInt64());
    EXPECT_LE(ledger["legs"].asUInt64() + ledger["legsOutOfRange"].asUInt64() + ledger["rowsQuantity"].asUInt64(),
              ledger["rowsMatched"].asUInt64());
    EXPECT_LE(ledger["rowsMatched"].asUInt64(), ledger["rows"].asUInt64());
    EXPECT_LE(ledger["legsEstimated"].asUInt64(), ledger["legs"].asUInt64());
    EXPECT_LE(ledger["legsRights"].asUInt64(), ledger["legs"].asUInt64());
    EXPECT_LE(ledger["unpricedChildren"].asUInt64(), ledger["legs"].asUInt64());
    if (!ledger["loaded"].asBool()) {
        EXPECT_EQ(ledger["rows"].asUInt64(), 0u);
        EXPECT_EQ(ledger["legs"].asUInt64(), 0u);
    }
}

TEST_F(HttpContractTest, SimulateReportsCorporateActionTotalsSeriesAndOptInLog) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    const auto quiet = simulate(request);
    ASSERT_EQ(quiet.status, 200) << quiet.body;
    Json::Value quiet_body;
    ASSERT_TRUE(parse_json(quiet.body, &quiet_body)) << quiet.body;
    ASSERT_TRUE(quiet_body.isMember("corporateActions"));
    ASSERT_TRUE(quiet_body["batches"][0].isMember("corporateActions"));
    EXPECT_EQ(compact_json(quiet_body["corporateActions"]),
              compact_json(quiet_body["batches"][0]["corporateActions"]));
    EXPECT_FALSE(quiet_body.isMember("distributionLog"));
    EXPECT_FALSE(quiet_body["batches"][0].isMember("distributionLog"));
    const Json::Value& totals = quiet_body["corporateActions"];
    for (const char* key : {"dividends", "dividendPnl", "distributions", "distributionPnl",
                            "childPositionsCreated", "distributionsEstimated", "distributionsUnpriced",
                            "legsUnresolved", "distributionPendingBars", "terminalDistributionPending",
                            "rightsExpired", "rightsExpiredHeld", "rightsExpiredGross",
                            "legsOnSettlementBar", "legsDeferredToEvents", "dividendsEstimated"}) {
        ASSERT_TRUE(totals.isMember(key)) << key;
        EXPECT_TRUE(totals[key].isNumeric()) << key;
    }
    ASSERT_TRUE(totals.isMember("unconfirmed") && totals["unconfirmed"].isObject());
    ASSERT_TRUE(totals.isMember("pnlUnconfirmed") && totals["pnlUnconfirmed"].isBool());
    ASSERT_TRUE(totals.isMember("distributionsValueOnly") && totals["distributionsValueOnly"].isNumeric());
    // The key set is part of the contract: a silently dropped key must fail here.
    EXPECT_EQ(totals.getMemberNames().size(), 19u) << compact_json(totals);
    EXPECT_LE(totals["childPositionsCreated"].asUInt64() + totals["distributionsValueOnly"].asUInt64(),
              totals["distributions"].asUInt64());
    EXPECT_LE(totals["distributionsEstimated"].asUInt64(),
              totals["childPositionsCreated"].asUInt64() + totals["distributionsValueOnly"].asUInt64());
    EXPECT_LE(totals["distributionsUnpriced"].asUInt64(), totals["distributions"].asUInt64());
    // The per-bar series cover the trading span and add up to the totals.
    const Json::Value& timeseries = quiet_body["timeseries"];
    ASSERT_TRUE(timeseries.isMember("dividendPnl"));
    ASSERT_TRUE(timeseries.isMember("distributionPnl"));
    ASSERT_EQ(timeseries["dividendPnl"].size(), timeseries["timestamps"].size());
    ASSERT_EQ(timeseries["distributionPnl"].size(), timeseries["timestamps"].size());
    double dividend_sum = 0.0, distribution_sum = 0.0;
    for (Json::ArrayIndex i = 0; i < timeseries["timestamps"].size(); ++i) {
        dividend_sum += timeseries["dividendPnl"][i].asDouble();
        distribution_sum += timeseries["distributionPnl"][i].asDouble();
    }
    EXPECT_NEAR(dividend_sum, totals["dividendPnl"].asDouble(), 1e-12);
    EXPECT_NEAR(distribution_sum, totals["distributionPnl"].asDouble(), 1e-12);
    // The gross P&L includes both legs: what the fixture's ledger schedules shows up here.
    const auto config_response = http_get(server_->port(), "/api/config");
    Json::Value config;
    ASSERT_EQ(config_response.status, 200);
    ASSERT_TRUE(parse_json(config_response.body, &config));
    if (config["corporateActions"]["legs"].asUInt64() > 0)
        EXPECT_GT(totals["dividends"].asUInt64() + totals["distributions"].asUInt64(), 0u);

    // The log is opt-in and must not change the accounting.
    request["distributionLog"] = true;
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body));
    ASSERT_TRUE(body.isMember("distributionLog"));
    ASSERT_TRUE(body["distributionLog"].isArray());
    EXPECT_EQ(compact_json(body["corporateActions"]), compact_json(totals))
        << "asking for the log must not change the accounting";
    EXPECT_EQ(compact_json(body["timeseries"]["cumPnl"]), compact_json(quiet_body["timeseries"]["cumPnl"]));
    const Json::Value& log = body["distributionLog"];
    double created = 0.0;
    unsigned unpriced = 0, estimated = 0, positions = 0, expired = 0, value_only = 0;
    for (Json::ArrayIndex i = 0; i < log.size(); ++i) {
        const Json::Value& entry = log[i];
        for (const char* key : {"parent", "time", "bar", "exDate", "child", "parentWeight", "childWeight",
                                "cashShare", "source", "estimate", "onSettlementBar", "valueSource", "eventType"})
            ASSERT_TRUE(entry.isMember(key)) << key;
        EXPECT_FALSE(entry["parent"].asString().empty());
        EXPECT_EQ(entry["time"].asString().size(), 10u);
        EXPECT_EQ(entry["exDate"].asString().size(), 10u);
        EXPECT_NE(entry["parentWeight"].asDouble(), 0.0);   // unheld parents stay out
        const std::string source = entry["source"].asString();
        EXPECT_TRUE(source == "childClose" || source == "ledgerValue" || source == "firstQuote" ||
                    source == "unpriced" || source == "expired" || source == "ledgerValueCash") << source;
        if (source == "unpriced") {
            ++unpriced;
            EXPECT_EQ(entry["childWeight"].asDouble(), 0.0);
        } else if (source == "ledgerValueCash") {
            // A child the panel does not carry, valued from the ledger: P&L without a position.
            ++value_only;
            EXPECT_TRUE(entry["child"].isNull() || !entry["child"].asString().empty());
            EXPECT_NE(entry["childWeight"].asDouble(), 0.0);
        } else if (source == "expired") {
            ++expired;
            EXPECT_EQ(entry["childWeight"].asDouble(), 0.0);
            EXPECT_LT(entry["exDate"].asString(), entry["time"].asString());   // closed after the ex-date
        } else {
            ++positions;
            EXPECT_FALSE(entry["child"].isNull());
        }
        if (entry["estimate"].asBool()) ++estimated;
        created += entry["childWeight"].asDouble();
    }
    EXPECT_EQ(positions, totals["childPositionsCreated"].asUInt64());
    EXPECT_EQ(unpriced, totals["distributionsUnpriced"].asUInt64());
    EXPECT_EQ(value_only, totals["distributionsValueOnly"].asUInt64());
    if (config["corporateActions"]["legsValueOnly"].asUInt64() > 0)
        EXPECT_GE(value_only, 1u);   // the fixture's B -> F vendor-implied leg while B is held
    EXPECT_EQ(estimated, totals["distributionsEstimated"].asUInt64());
    EXPECT_EQ(expired, totals["rightsExpired"].asUInt64());
    EXPECT_NEAR(created, totals["distributionPnl"].asDouble(), 1e-12);
    // The fixture's warrant into the never-priced W lapses while its parent is held.
    if (config["corporateActions"]["legsRights"].asUInt64() > 0 &&
        config["corporateActions"]["rightsTermsLoaded"].asBool())
        EXPECT_GE(totals["rightsExpired"].asUInt64(), 1u);

    // Multi-alpha and combined results carry the same summary.
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    const auto combined = simulate(request);
    ASSERT_EQ(combined.status, 200) << combined.body;
    Json::Value combined_body;
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    ASSERT_TRUE(combined_body["combined"].isMember("corporateActions"));
    EXPECT_EQ(compact_json(combined_body["corporateActions"]),
              compact_json(combined_body["combined"]["corporateActions"]));
    for (Json::ArrayIndex i = 0; i < combined_body["batches"].size(); ++i) {
        EXPECT_TRUE(combined_body["batches"][i].isMember("corporateActions"));
        EXPECT_TRUE(combined_body["batches"][i].isMember("distributionLog"));
    }
}


// Unconfirmed P&L (docs/corporate_actions_engine_2026-09-17.md section 9): the fixture's
// marker rows (A cash without an amount, C spin-off without a child) and the B->E leg
// whose child is outside the panel are held by the long-only contract alpha, so the
// warn-mode summary lists them; a second server with unresolved_policy strict refuses
// the same request with 400 and names the first events.
TEST_F(HttpContractTest, SimulateReportsUnconfirmedPnlAndTheStrictPolicyReturns400) {
    if (!available_) GTEST_SKIP() << skip_reason_;
    const auto config_response = http_get(server_->port(), "/api/config");
    ASSERT_EQ(config_response.status, 200);
    Json::Value config;
    ASSERT_TRUE(parse_json(config_response.body, &config));
    const Json::Value& ledger = config["corporateActions"];
    EXPECT_EQ(ledger["unresolvedPolicy"].asString(), "warn");   // the fixture config sets no policy
    Json::Value request;
    request["xml"] = "<data expr=\"close\"/><rank/>";
    request["isStart"] = "2021-01-01";
    request["osStart"] = "2024-01-01";
    const auto response = simulate(request);
    ASSERT_EQ(response.status, 200) << response.body;
    Json::Value body;
    ASSERT_TRUE(parse_json(response.body, &body)) << response.body;
    const Json::Value& unconfirmed = body["corporateActions"]["unconfirmed"];
    for (const char* key : {"count", "sumAbsWeight", "byKind", "items", "itemsTruncated", "itemsOmitted"})
        ASSERT_TRUE(unconfirmed.isMember(key)) << key;
    ASSERT_TRUE(unconfirmed["items"].isArray());
    // At most 10,000 items are listed; the count and byKind always cover every record.
    EXPECT_EQ(unconfirmed["items"].size(), std::min<unsigned>(unconfirmed["count"].asUInt(), 10000u));
    EXPECT_EQ(unconfirmed["itemsTruncated"].asBool(), unconfirmed["count"].asUInt() > 10000u);
    EXPECT_EQ(unconfirmed["items"].size() + unconfirmed["itemsOmitted"].asUInt(), unconfirmed["count"].asUInt());
    EXPECT_EQ(body["corporateActions"]["pnlUnconfirmed"].asBool(), unconfirmed["count"].asUInt() > 0);
    for (const char* kind : {"unresolved_leg", "default_settlement", "estimate_value", "pending_dropped"})
        ASSERT_TRUE(unconfirmed["byKind"].isMember(kind) && unconfirmed["byKind"][kind].isNumeric()) << kind;
    double abs_sum = 0.0;
    unsigned by_kind_sum = 0;
    for (const auto& name : unconfirmed["byKind"].getMemberNames()) by_kind_sum += unconfirmed["byKind"][name].asUInt();
    EXPECT_EQ(by_kind_sum, unconfirmed["count"].asUInt());
    std::set<std::string> seen, exposures;   // sumAbsWeight counts each (bar, symbol) once
    for (Json::ArrayIndex i = 0; i < unconfirmed["items"].size(); ++i) {
        const Json::Value& item = unconfirmed["items"][i];
        for (const char* key : {"symbol", "date", "bar", "weightBefore", "kind", "detail"})
            ASSERT_TRUE(item.isMember(key)) << key;
        EXPECT_EQ(item["date"].asString().size(), 10u);
        EXPECT_NE(item["weightBefore"].asDouble(), 0.0);   // held parents only
        if (exposures.insert(item["symbol"].asString() + "@" + std::to_string(item["bar"].asUInt())).second)
            abs_sum += std::fabs(item["weightBefore"].asDouble());
        seen.insert(item["symbol"].asString() + " " + item["date"].asString() + " " + item["kind"].asString() +
                    "(" + item["detail"].asString() + ")");
    }
    if (!unconfirmed["itemsTruncated"].asBool())
        EXPECT_NEAR(abs_sum, unconfirmed["sumAbsWeight"].asDouble(), 1e-12);
    if (ledger["legsUnbookable"].asUInt64() >= 2) {
        EXPECT_TRUE(seen.count("A 2023-09-12 unresolved_leg(no_cash_amount)")) << compact_json(unconfirmed["items"]);
        EXPECT_TRUE(seen.count("C 2025-02-14 unresolved_leg(no_child)")) << compact_json(unconfirmed["items"]);
    }
    if (ledger["unpricedChildren"].asUInt64() >= 1)
        EXPECT_TRUE(seen.count("B 2024-02-20 pending_dropped(child_not_in_panel)")) << compact_json(unconfirmed["items"]);
    if (ledger["legsValueOnly"].asUInt64() >= 1)
        EXPECT_TRUE(seen.count("B 2025-06-12 estimate_value(ledger_value_cash:vendor_implied)")) << compact_json(unconfirmed["items"]);
    // The quote-priced D -> A spin-off is booked from A's quote but its ratio is the ledger's estimate.
    if (ledger["legsEstimated"].asUInt64() >= 2)
        EXPECT_TRUE(seen.count("D 2025-08-15 estimate_value(child_close:ratio)")) << compact_json(unconfirmed["items"]);
    EXPECT_EQ(unconfirmed["byKind"]["unresolved_leg"].asUInt(), body["corporateActions"]["legsUnresolved"].asUInt());
    // The per-bar series adds up to the summary and the accounting is unchanged by the report.
    const Json::Value& timeseries = body["timeseries"];
    ASSERT_TRUE(timeseries.isMember("unconfirmedWeight"));
    ASSERT_EQ(timeseries["unconfirmedWeight"].size(), timeseries["timestamps"].size());
    double series_sum = 0.0;
    for (Json::ArrayIndex i = 0; i < timeseries["unconfirmedWeight"].size(); ++i) {
        EXPECT_GE(timeseries["unconfirmedWeight"][i].asDouble(), 0.0);
        series_sum += timeseries["unconfirmedWeight"][i].asDouble();
    }
    EXPECT_NEAR(series_sum, unconfirmed["sumAbsWeight"].asDouble(), 1e-12);
    // Batch and combined results carry the same block.
    request["xml"] = "<data expr=\"close\"/><tsmean days=\"2|3\"/>";
    const auto combined = simulate(request);
    ASSERT_EQ(combined.status, 200) << combined.body;
    Json::Value combined_body;
    ASSERT_TRUE(parse_json(combined.body, &combined_body));
    ASSERT_TRUE(combined_body["combined"]["corporateActions"].isMember("unconfirmed"));
    for (Json::ArrayIndex i = 0; i < combined_body["batches"].size(); ++i)
        EXPECT_TRUE(combined_body["batches"][i]["corporateActions"].isMember("pnlUnconfirmed"));

    // Strict: the same fixture with unresolved_policy: strict refuses the request.
    if (unconfirmed["count"].asUInt() == 0) GTEST_SKIP() << "fixture has no unconfirmed event to refuse";
    Json::Value yaml;
    {
        std::ifstream in(find_config_path());
        ASSERT_TRUE(parse_json(std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>()), &yaml))
            << "the fixture config is JSON-formatted YAML";
    }
    ASSERT_TRUE(yaml["backtest"].isMember("corporate_actions"));
    yaml["backtest"]["corporate_actions"]["unresolved_policy"] = "strict";
    const fs::path strict_config = fs::temp_directory_path() /
        ("longshort_http_strict_" + std::to_string(::getpid()) + ".yaml");
    {
        std::ofstream out(strict_config);
        out << compact_json(yaml);
    }
    ServerProcess strict(find_server_binary(), strict_config, server_->port() + 1);
    ASSERT_TRUE(strict.start()) << "strict server failed to start";
    const auto strict_config_response = http_get(strict.port(), "/api/config");
    ASSERT_EQ(strict_config_response.status, 200);
    Json::Value strict_report;
    ASSERT_TRUE(parse_json(strict_config_response.body, &strict_report));
    EXPECT_EQ(strict_report["corporateActions"]["unresolvedPolicy"].asString(), "strict");
    request["xml"] = "<data expr=\"close\"/><rank/>";
    const auto refused = http_post_json(strict.port(), "/api/simulate", request);
    EXPECT_EQ(refused.status, 400) << refused.body;
    Json::Value error;
    ASSERT_TRUE(parse_json(refused.body, &error));
    const std::string message = error["error"].asString();
    EXPECT_NE(message.find("unresolved_policy strict"), std::string::npos) << message;
    EXPECT_NE(message.find(unconfirmed["items"][0]["symbol"].asString() + " " +
                           unconfirmed["items"][0]["kind"].asString()), std::string::npos) << message;
    EXPECT_NE(message.find(unconfirmed["items"][0]["date"].asString()), std::string::npos) << message;
    strict.stop();
    std::error_code ec;
    fs::remove(strict_config, ec);
}

// ── Cost modules (docs/corporate_actions_engine_2026-09-17.md "costs") ──────────

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

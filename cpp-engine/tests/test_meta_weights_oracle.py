"""Oracle for the <meta> combination weights: meta_weights_probe vs numpy/sklearn/scipy.

Usage: test_meta_weights_oracle.py /path/to/meta_weights_probe
Exits 77 (CTest skip) when numpy/scipy/sklearn are unavailable.
"""
import subprocess
import sys
import unittest
import warnings

try:
    import numpy as np
    from scipy import optimize
    from sklearn.covariance import graphical_lasso, ledoit_wolf, oas
    from sklearn.exceptions import ConvergenceWarning
except ImportError as error:  # pragma: no cover
    print(f"skip: {error}")
    sys.exit(77)

PROBE = None
T, K, SIM_START, DAYS = 400, 8, 20, 60


def make_returns():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0, 0.01, size=(T, K)) * (1.0 + 0.2 * np.arange(K))
    r += 0.0004 * np.array([1, 2, -1, 0.5, 1.5, -0.5, 1, 0.2])
    factor = rng.normal(0.0, 0.01, size=T)
    r[:, :3] += factor[:, None] * np.array([1.0, 0.8, 0.9])    # correlated block
    r[:SIM_START] = np.nan
    r[200, 3] = np.nan                                         # one bar without a value -> fallbacks
    return r


def normalize(w):
    if w is None or not np.all(np.isfinite(w)):
        return None
    gross = np.abs(w).sum()
    if not np.isfinite(gross) or gross <= 0:
        return None
    return w / gross


def constant_correlation(x):
    """Ledoit & Wolf (2004), 'Honey, I shrunk the sample covariance matrix' (covCor)."""
    t, n = x.shape
    y = x - x.mean(0)
    s = y.T @ y / t
    var = np.diag(s)
    sd = np.sqrt(var)
    rbar = (s / np.outer(sd, sd)).sum() - n
    rbar /= n * (n - 1)
    f = rbar * np.outer(sd, sd)
    np.fill_diagonal(f, var)
    pi_mat = (y ** 2).T @ (y ** 2) / t - s ** 2
    pihat = pi_mat.sum()
    theta = (y ** 3).T @ y / t - var[:, None] * s
    rho = np.trace(pi_mat)
    for i in range(n):
        for j in range(n):
            if i != j:
                rho += rbar / 2 * (sd[j] / sd[i] * theta[i, j] + sd[i] / sd[j] * theta[j, i])
    gamma = ((f - s) ** 2).sum()
    delta = max(0.0, min(1.0, (pihat - rho) / gamma / t))
    return delta * f + (1 - delta) * s


def shrunk(x, shrink, delta):
    if shrink == "diag":
        c = np.cov(x, rowvar=False)
        return (1 - delta) * c + delta * np.diag(np.diag(c))
    if shrink == "lw":
        return ledoit_wolf(x)[0]
    if shrink == "oas":
        return oas(x)[0]
    return constant_correlation(x)


def erc(x):
    c = np.cov(x, rowvar=False)
    k = len(c)
    c = c + 1e-12 * np.trace(c) / k * np.eye(k)
    b = 1.0 / k

    def conditions(y):
        w = np.exp(y)
        return w * (c @ w) - b

    y0 = np.log(1.0 / np.sqrt(np.diag(c)))
    sol = optimize.root(conditions, y0, method="hybr", tol=1e-14)
    # hybr may report "no progress" at this tight tol after it has converged; judge by the residual.
    if np.max(np.abs(conditions(sol.x))) > 1e-10 * b:
        return None
    return normalize(np.exp(sol.x))


def _mvo_weight(cov, mu):  # alpha_zoo_results.py
    a = cov + np.outer(mu, mu)
    try:
        w = np.linalg.solve(a, mu)
    except np.linalg.LinAlgError:
        return None
    return normalize(w)


def _spca_weight(x, mu, k):  # alpha_zoo_results.py, verbatim logic
    cov = np.cov(x, rowvar=False)
    sd = x.std(axis=0, ddof=1)
    score = np.divide(mu, sd, out=np.zeros_like(mu), where=sd > 1e-12)
    scaled_cov = score[:, None] * cov * score[None, :]
    eigenvalues, eigenvectors = np.linalg.eigh(scaled_cov)
    q = eigenvectors[:, np.argsort(eigenvalues)[::-1][:min(k, len(mu))]]
    loading = score[:, None] * q
    pc = x @ loading
    pc_mu = pc.mean(axis=0)
    pc_cov = np.atleast_2d(np.cov(pc, rowvar=False))
    a = _mvo_weight(pc_cov, pc_mu)
    if a is None:
        return None
    return normalize(loading @ a)


def spca(x, k):
    sd = x.std(axis=0, ddof=1)       # the research feeds sd-standardized members
    if np.any(sd <= 1e-12):
        return None
    xs = x / sd
    w = _spca_weight(xs, xs.mean(0), k)
    return None if w is None else normalize(w / sd)


def glasso(x, alpha):  # glasso_mvo.py
    c = np.cov(x, rowvar=False)
    d = np.sqrt(np.diag(c))
    r = c / np.outer(d, d)
    r = 0.99 * r + 0.01 * np.eye(len(r))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            cov_gl, _ = graphical_lasso(r, alpha=alpha, mode="cd", max_iter=100, tol=1e-4)
        except (ConvergenceWarning, FloatingPointError):
            return None
    mu = x.mean(0)
    return _mvo_weight(cov_gl * np.outer(d, d), mu)


def window_weights(x, method, shrink="diag", delta=0.5, k=3, alpha=0.1):
    mu = x.mean(0)
    if method != "ewsign" and np.any(x.std(0, ddof=1) <= 1e-12):
        return None   # flat member: EW fallback for every method except ewsign
    if method == "ewsign":
        return normalize(np.sign(mu))
    if method == "ivol":
        return normalize(1.0 / x.std(0, ddof=1))
    if method == "mvo":
        return normalize(np.linalg.solve(shrunk(x, shrink, delta), mu))
    if method == "minvar":
        return normalize(np.linalg.solve(shrunk(x, shrink, delta), np.ones(x.shape[1])))
    if method == "erc":
        return erc(x)
    if method == "spca":
        return spca(x, k)
    if method == "glasso":
        return glasso(x, alpha)
    raise ValueError(method)


def reference(r, days, rebalance, **kw):
    t_count, k = r.shape
    ew = np.full(k, 1.0 / k)
    rows = np.tile(ew, (t_count, 1))
    t0 = SIM_START + days
    fallback = 0
    current, current_fallback = ew, False
    for t in range(t0, t_count):
        if (t - t0) % rebalance == 0:
            x = r[t - days:t]
            w = window_weights(x, **kw) if np.all(np.isfinite(x)) else None
            current_fallback = w is None
            current = ew if w is None else w
        rows[t] = current
        fallback += current_fallback
    return rows, fallback, min(t0, t_count)


def run_probe(r, days, rebalance, method, shrink="diag", delta=0.5, k=3, alpha=0.1):
    lines = [",".join(f"m{i}" for i in range(r.shape[1]))]
    lines += [",".join("nan" if not np.isfinite(v) else repr(float(v)) for v in row) for row in r]
    args = [PROBE, "--method", method, "--days", str(days), "--shrink", shrink, "--delta", f"{delta:.10f}",
            "--k", str(k), "--alpha", f"{alpha:.10f}", "--rebalance", str(rebalance), "--sim-start", str(SIM_START)]
    out = subprocess.run(args, input="\n".join(lines) + "\n", capture_output=True, text=True, check=True).stdout
    body = out.strip().splitlines()
    tail = dict(item.split("=") for item in body[-1].split())
    rows = np.array([[float(v) for v in line.split(",")] for line in body[:-1]])
    return rows, int(tail["fallback_bars"]), int(tail["first_estimated_bar"])


CASES = [("mvo", dict(shrink="diag", delta=0.5)), ("mvo", dict(shrink="diag", delta=0.0)),
         ("mvo", dict(shrink="lw")), ("mvo", dict(shrink="cc")), ("mvo", dict(shrink="oas")),
         ("minvar", dict(shrink="diag", delta=0.5)), ("erc", {}), ("ivol", {}), ("ewsign", {}),
         ("spca", dict(k=3)), ("glasso", dict(alpha=0.1))]


class MetaWeightsOracle(unittest.TestCase):
    def test_methods_match_reference(self):
        r = make_returns()
        for method, kw in CASES:
            for rebalance in (1, 7):
                with self.subTest(method=method, rebalance=rebalance, **kw):
                    got, got_fallback, got_t0 = run_probe(r, DAYS, rebalance, method, **kw)
                    # The engine reads shrink/alpha as float; give the reference the same float32 value.
                    ref_kw = {key: float(np.float32(v)) if key in ("delta", "alpha") else v for key, v in kw.items()}
                    want, want_fallback, want_t0 = reference(r, DAYS, rebalance, method=method, **ref_kw)
                    self.assertEqual(got.shape, want.shape)
                    self.assertEqual(got_t0, want_t0)
                    self.assertEqual(got_fallback, want_fallback)
                    self.assertGreater(got_fallback, 0)   # the NaN bar falls back in every method
                    tol = 1e-6 if method == "erc" else 1e-8
                    np.testing.assert_allclose(got, want, rtol=0, atol=tol)
                    self.assertFalse(np.allclose(got[want_t0:], 1.0 / K) and method != "ivol")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: test_meta_weights_oracle.py PROBE")
        sys.exit(2)
    PROBE = sys.argv.pop(1)
    unittest.main()

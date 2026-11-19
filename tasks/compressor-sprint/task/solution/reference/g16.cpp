#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

typedef std::vector<uint8_t> Buf;

static Buf load(const char *p) {
  Buf b;
  FILE *f = fopen(p, "rb");
  if (!f) exit(2);
  static uint8_t t[1 << 16];
  size_t r;
  while ((r = fread(t, 1, sizeof t, f)) > 0) b.insert(b.end(), t, t + r);
  fclose(f);
  return b;
}

static void save(const char *p, const Buf &b) {
  FILE *f = fopen(p, "wb");
  if (!f) exit(2);
  if (!b.empty() && fwrite(b.data(), 1, b.size(), f) != b.size()) exit(2);
  if (fclose(f)) exit(2);
}

struct AC {
  uint32_t x1 = 0, x2 = 0xffffffffu, x = 0;
  Buf *out = nullptr;
  const uint8_t *in = nullptr;
  size_t ip = 0, in_n = 0;
  uint32_t next() { return ip < in_n ? in[ip++] : 0; }
  void start(const uint8_t *d, size_t n) {
    in = d;
    in_n = n;
    for (int i = 0; i < 4; i++) x = (x << 8) | next();
  }
  int code(int bit, uint32_t p) {
    uint32_t xm = x1 + (uint32_t)(((uint64_t)(x2 - x1) * p) >> 16);
    if (!out) bit = x <= xm;
    if (bit) x2 = xm; else x1 = xm + 1;
    while (((x1 ^ x2) >> 24) == 0) {
      if (out) out->push_back((uint8_t)(x2 >> 24)); else x = (x << 8) | next();
      x1 <<= 8;
      x2 = (x2 << 8) | 255;
    }
    return bit;
  }
  void flush() {
    for (int i = 0; i < 4; i++) {
      out->push_back((uint8_t)(x1 >> 24));
      x1 <<= 8;
    }
  }
};

static inline double sq(double x) { return 1.0 / (1.0 + std::exp(-x)); }
static inline double cl(double x, double a, double b) { return x < a ? a : x > b ? b : x; }
static inline int cli(int x, int a, int b) { return x < a ? a : x > b ? b : x; }

struct Ctr {
  uint16_t p = 32768;
  uint16_t n = 0;
};

static double STR[4096];

static inline double cst(const Ctr &c) { return STR[c.p >> 4]; }

static inline void cup(Ctr &c, int bit, int lim) {
  int t = bit ? 65535 : 0;
  c.p = (uint16_t)(c.p + (t - (int)c.p) / (c.n + 1.5));
  if (c.n < lim) c.n++;
}

struct OLS {
  int n;
  double lam, nu;
  std::vector<double> A, b, w, L, f, t;
  OLS(int n, double lam, double nu = 1e-4)
      : n(n), lam(lam), nu(nu), A((size_t)n * n, 0.0), b(n, 0.0), w(n, 0.0), L((size_t)n * n, 0.0), f(n, 0.0), t(n, 0.0) {}
  double predict() const {
    double s = 0;
    for (int i = 0; i < n; i++) s += w[i] * f[i];
    return s;
  }
  void update(double y) {
    double tr = 0;
    for (int i = 0; i < n; i++) {
      double fi = f[i];
      double *r = &A[(size_t)i * n];
      for (int j = 0; j <= i; j++) r[j] = lam * r[j] + fi * f[j];
      b[i] = lam * b[i] + fi * y;
      tr += r[i];
    }
    double reg = nu * tr / n + 1e-3;
    for (int i = 0; i < n; i++) {
      const double *ai = &A[(size_t)i * n];
      double *li = &L[(size_t)i * n];
      for (int j = 0; j <= i; j++) {
        const double *lj = &L[(size_t)j * n];
        double s = ai[j];
        for (int k = 0; k < j; k++) s -= li[k] * lj[k];
        if (i == j) {
          s += reg;
          if (s <= 1e-9) return;
          li[i] = std::sqrt(s);
        } else {
          li[j] = s / lj[j];
        }
      }
    }
    for (int i = 0; i < n; i++) {
      const double *li = &L[(size_t)i * n];
      double s = b[i];
      for (int k = 0; k < i; k++) s -= li[k] * t[k];
      t[i] = s / li[i];
    }
    for (int i = n - 1; i >= 0; i--) {
      double s = t[i];
      for (int k = i + 1; k < n; k++) s -= L[(size_t)k * n + i] * w[k];
      w[i] = s / L[(size_t)i * n + i];
    }
  }
};

struct Mix {
  int n;
  double lr;
  std::vector<double> w;
  double *cur = nullptr;
  double dot = 0;
  Mix(int n, int sets, double init, double lr) : n(n), lr(lr), w((size_t)n * sets, init) {}
  double run(const double *x, int s) {
    cur = &w[(size_t)s * n];
    double d = 0;
    for (int i = 0; i < n; i++) d += cur[i] * x[i];
    dot = d;
    return d;
  }
  void upd(const double *x, double err) {
    double e = err * lr;
    for (int i = 0; i < n; i++) cur[i] += e * x[i];
  }
};

struct APM {
  std::vector<float> t;
  int idx = 0;
  double fr = 0;
  double rate;
  APM(int ctxs, double rate) : t((size_t)ctxs * 33), rate(rate) {
    for (size_t c = 0; c < (size_t)ctxs; c++)
      for (int j = 0; j < 33; j++) t[c * 33 + j] = (float)sq((j - 16) * 0.5);
  }
  double run(double st, int c) {
    double u = (cl(st, -7.999, 7.999) + 8.0) * 2.0;
    int j = (int)u;
    fr = u - j;
    idx = c * 33 + j;
    return t[idx] * (1.0 - fr) + t[idx + 1] * fr;
  }
  void upd(int bit) {
    t[idx] += (float)((bit - t[idx]) * rate * (1.0 - fr));
    t[idx + 1] += (float)((bit - t[idx + 1]) * rate * fr);
  }
};

struct Tab {
  double m = 0;
  int n = 0;
};

static inline double lgst(double lo, double mid, double hi, double mu, double inv) {
  double a = (lo - 0.5 - mu) * inv, b = (mid - 0.5 - mu) * inv, c = (hi - 0.5 - mu) * inv;
  double up, tot;
  if (a > 30.0) {
    double e1 = std::exp(a - b), e2 = std::exp(a - c);
    up = e1 - e2;
    tot = 1.0 - e2;
  } else if (c < -30.0) {
    up = 1.0 - std::exp(b - c);
    tot = 1.0 - std::exp(a - c);
  } else {
    if (a < -40.0) a = -40.0;
    if (c > 40.0) c = 40.0;
    b = cl(b, a, c);
    double ea = std::exp(a), eb = std::exp(b), ec = std::exp(c);
    if (b <= 0.0) {
      double fa = ea / (1.0 + ea), fb = eb / (1.0 + eb), fc = ec / (1.0 + ec);
      up = fc - fb;
      tot = fc - fa;
    } else {
      double sa = 1.0 / (1.0 + ea), sb = 1.0 / (1.0 + eb), sc = 1.0 / (1.0 + ec);
      up = sb - sc;
      tot = sa - sc;
    }
  }
  double p = tot > 1e-280 ? up / tot : 0.5;
  p = cl(p, 1e-7, 1.0 - 1e-7);
  return std::log(p / (1.0 - p));
}

static inline int qa(double a) { return cli((int)(4.0 * std::log2(a + 0.0625) + 16.0), 0, 95); }
static inline int qs(double s) { return cli((int)(std::log2(s + 0.0625) + 4.0), 0, 19); }
static inline int zb(double z) { return cli((int)std::floor(z * 4.0 + 0.5), -60, 60) + 60; }
static inline int wb(double w) { return cli((int)std::floor(2.0 * std::log2(w) + 8.5), 0, 47); }

static void codec(std::vector<int> &v, size_t n, long W, long P, int B, AC &ac, bool enc) {
  const int NF = 27;
  static const int DX[NF] = {-1, -2, -3, -4, 0, -1, 1, -2, 2, -3, 3, 0, -1, 1, -2, 2, 0, -1, 1, -4, 4, -3, 3, -2, 2, 0, -5};
  static const int DY[NF] = {0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 1, 1, 2, 2, 3, 3, 4, 0};
  static const int IA[4] = {0, 4, 5, 6}, IB[8] = {0, 1, 4, 5, 6, 7, 8, 11};
  long off2[NF];
  for (int j = 0; j < NF; j++) off2[j] = (long)DY[j] * W - DX[j];
  const long off3[10] = {P, P + 1, P - 1, P + W, P - W, P + W + 1, P + W - 1, P - W + 1, P - W - 1, 2 * P};
  OLS oA(4, 0.98), oB(8, 0.995), oC(19, 0.999), oD(27, 0.9997), oE(18, 0.998), oF(29, 0.9995);
  const int K = P ? 13 : 8, KT = K + 2;
  OLS oS(K - 1, 0.998, 1e-3);
  std::vector<std::vector<float>> ae(KT, std::vector<float>(n, 0.f));
  const int NPR = 8, NE = 12, NC = 5, NI = NE + NC + 1;
  int prim[NPR] = {K, K + 1, 2, 3, 1, 6, P ? 8 : 0, P ? 9 : 7};
  std::vector<Tab> ET((size_t)NPR * 96);
  const long so[7] = {1, W, W + 1, W - 1, 2, 2 * W, P};
  const double sw7[7] = {2, 2, 1, 1, 1, 1, 2};
  const int ns = P ? 7 : 6;
  const double maxv = (double)((1 << B) - 1);
  const int ZB = 121, WB = 48;
  std::vector<Ctr> c1((size_t)ZB * WB), c2((size_t)ZB * WB), c3((size_t)ZB * WB), c5((size_t)16 * 20);
  const bool direct = B <= 12;
  const int QL = direct ? std::min(1 << B, 256) : 1;
  std::vector<Ctr> c4(direct ? ((size_t)QL << B) : 1);
  Mix m1(NI, 16 * 20, 0.12, 0.004), m2(NI, ZB * WB, 0.12, 0.004), m3(NI, WB, 0.12, 0.004);
  Mix mf(3, 16, 0.4, 0.002);
  APM a1(ZB * WB, 0.02), a2(16 * 20, 0.02);
  double xin[NI], mu[16], la[16], lm[16], ps[NPR], pinv[NE], pmu[NE];
  int pbk[NPR];
  for (size_t i = 0; i < n; i++) {
    auto G = [&](long o) -> double {
      long j = (long)i - o;
      return j >= 0 ? (double)v[j] : (i ? (double)v[i - 1] : (double)(1 << (B - 1)));
    };
    double nb[NF], pl[10];
    for (int j = 0; j < NF; j++) nb[j] = G(off2[j]);
    const double pw = nb[0], pn = nb[4], pnw = nb[5], pne = nb[6];
    const double bs = (pw + pn + pnw + pne) * 0.25;
    for (int j = 0; j < 4; j++) oA.f[j] = nb[IA[j]] - bs;
    for (int j = 0; j < 8; j++) oB.f[j] = nb[IB[j]] - bs;
    for (int j = 0; j < 19; j++) oC.f[j] = nb[j] - bs;
    for (int j = 0; j < 27; j++) oD.f[j] = nb[j] - bs;
    mu[0] = bs + oA.predict();
    mu[1] = bs + oB.predict();
    mu[2] = bs + oC.predict();
    mu[3] = bs + oD.predict();
    mu[4] = pw;
    mu[5] = pn;
    {
      double mx = std::max(pw, pn), mn = std::min(pw, pn);
      mu[6] = pnw >= mx ? mn : pnw <= mn ? mx : pw + pn - pnw;
    }
    mu[7] = (pw + pn) * 0.5;
    if (P) {
      for (int j = 0; j < 10; j++) pl[j] = G(off3[j]);
      for (int j = 0; j < 8; j++) oE.f[j] = nb[IB[j]] - bs;
      for (int j = 0; j < 10; j++) oE.f[8 + j] = pl[j] - bs;
      for (int j = 0; j < 19; j++) oF.f[j] = nb[j] - bs;
      for (int j = 0; j < 10; j++) oF.f[19 + j] = pl[j] - bs;
      mu[8] = bs + oE.predict();
      mu[9] = bs + oF.predict();
      mu[10] = pl[0];
      mu[11] = pw + pl[0] - pl[1];
      mu[12] = pn + pl[0] - pl[3];
    }
    for (int k = 0; k < K; k++) mu[k] = cl(mu[k], 0.0, maxv);
    for (int t = 0; t < KT; t++) {
      const float *e = ae[t].data();
      double sa = 0, sm = 0, sw = 0;
      for (int j = 0; j < ns; j++) {
        long q = (long)i - so[j];
        if (q >= 0) {
          double x = e[q];
          sa += sw7[j] * x;
          sm += sw7[j] * x * x;
          sw += sw7[j];
        }
      }
      la[t] = sw > 0 ? sa / sw : maxv / 8.0;
      lm[t] = sw > 0 ? sm / sw : maxv * maxv / 64.0;
    }
    double mb;
    {
      double sw = 0, sx = 0;
      for (int k = 0; k < K; k++) {
        double q = 1.0 / (lm[k] + 0.01);
        q *= q;
        sw += q;
        sx += q * mu[k];
      }
      mb = sx / sw;
    }
    mu[K] = mb;
    {
      int c = 0;
      for (int k = 0; k < K; k++)
        if (k != 7) oS.f[c++] = mu[k] - mb;
      mu[K + 1] = cl(mb + oS.predict(), 0.0, maxv);
    }
    for (int e = 0; e < NPR; e++) {
      double a = la[prim[e]];
      int bk = qa(a);
      const Tab &tb = ET[(size_t)e * 96 + bk];
      double ea = (tb.m * tb.n + a * 2.0) / (tb.n + 2.0);
      pbk[e] = bk;
      ps[e] = std::max(ea / 1.386, 0.05);
      pmu[e] = mu[prim[e]];
      pinv[e] = 1.0 / ps[e];
    }
    pmu[8] = pmu[0];
    pinv[8] = pinv[0] / 0.6;
    pmu[9] = pmu[0];
    pinv[9] = pinv[0] / 1.7;
    pmu[10] = pmu[1];
    pinv[10] = pinv[1] / 0.6;
    pmu[11] = pmu[1];
    pinv[11] = pinv[1] / 1.7;
    const int qs0 = qs(ps[0]);
    const int qm = direct ? cli((int)(mb * QL / (maxv + 1.0)), 0, QL - 1) : 0;
    int lo = 0, hi = 1 << B, node = 1;
    const int val = enc ? v[i] : 0;
    for (int k = B - 1; k >= 0; k--) {
      const int mid = (lo + hi) >> 1;
      for (int e = 0; e < NE; e++) xin[e] = lgst(lo, mid, hi, pmu[e], pinv[e]);
      const double wd = (double)(hi - lo), bd = mid - 0.5;
      const int z0 = zb((bd - pmu[0]) * pinv[0]), w0 = wb(wd * pinv[0]);
      const int z1 = zb((bd - pmu[1]) * pinv[1]), w1 = wb(wd * pinv[1]);
      const int z2 = zb((bd - pmu[2]) * pinv[2]), w2 = wb(wd * pinv[2]);
      Ctr &r1 = c1[(size_t)z0 * WB + w0], &r2 = c2[(size_t)z1 * WB + w1], &r3 = c3[(size_t)z2 * WB + w2];
      Ctr &r4 = c4[direct ? (((size_t)qm << B) | node) : 0], &r5 = c5[(size_t)k * 20 + qs0];
      xin[NE] = cst(r1);
      xin[NE + 1] = cst(r2);
      xin[NE + 2] = cst(r3);
      xin[NE + 3] = direct ? cst(r4) : 0.0;
      xin[NE + 4] = cst(r5);
      xin[NE + 5] = 0.3;
      double y[3];
      y[0] = cl(m1.run(xin, k * 20 + qs0), -14.0, 14.0);
      y[1] = cl(m2.run(xin, z0 * WB + w0), -14.0, 14.0);
      y[2] = cl(m3.run(xin, w1), -14.0, 14.0);
      const double st = cl(mf.run(y, k), -14.0, 14.0);
      const double p0 = sq(st);
      const double pa = a1.run(st, z0 * WB + w0), pb = a2.run(st, k * 20 + qs0);
      const double p = 0.5 * p0 + 0.25 * pa + 0.25 * pb;
      const uint32_t p16 = (uint32_t)cli((int)(p * 65536.0), 1, 65535);
      const int bit = ac.code(enc ? (val >= mid) : 0, p16);
      m1.upd(xin, bit - sq(y[0]));
      m2.upd(xin, bit - sq(y[1]));
      m3.upd(xin, bit - sq(y[2]));
      mf.upd(y, bit - p0);
      a1.upd(bit);
      a2.upd(bit);
      cup(r1, bit, 255);
      cup(r2, bit, 255);
      cup(r3, bit, 255);
      if (direct) cup(r4, bit, 255);
      cup(r5, bit, 255);
      node = (node << 1) | bit;
      if (bit) lo = mid; else hi = mid;
    }
    if (!enc) v[i] = lo;
    const double x = (double)v[i];
    for (int t = 0; t < KT; t++) ae[t][i] = (float)std::fabs(x - mu[t]);
    for (int e = 0; e < NPR; e++) {
      Tab &tb = ET[(size_t)e * 96 + pbk[e]];
      double a = ae[prim[e]][i];
      tb.m += (a - tb.m) / (tb.n + 1.0);
      if (tb.n < 255) tb.n++;
    }
    const double yv = x - bs;
    oA.update(yv);
    oB.update(yv);
    oC.update(yv);
    oD.update(yv);
    if (P) {
      oE.update(yv);
      oF.update(yv);
    }
    oS.update(x - mb);
  }
}

static bool detect(const std::vector<int> &s, long &W, long &P) {
  const size_t n = s.size();
  const long LM = (long)std::min<size_t>(n / 4, 8192);
  if (LM < 16) return false;
  auto msd = [&](long L, size_t step) {
    double a = 0;
    size_t m = 0;
    for (size_t i = (size_t)L; i < n; i += step) {
      double e = (double)(s[i] - s[i - L]);
      a += e * e;
      m++;
    }
    return a / (double)m;
  };
  std::vector<double> d(LM + 1, 0.0);
  for (long L = 8; L <= LM; L++) d[L] = msd(L, 3);
  long L0 = 8;
  for (long L = 8; L <= LM; L++)
    if (d[L] < d[L0]) L0 = L;
  W = L0;
  for (long k = L0 / 9; k >= 2; k--) {
    if (L0 % k) continue;
    const long c = L0 / k;
    if (d[c] <= 1.5 * d[L0] && d[c] < d[c - 1] && d[c] < d[c + 1]) {
      W = c;
      break;
    }
  }
  std::vector<double> o(d.begin() + 8, d.end());
  std::nth_element(o.begin(), o.begin() + o.size() / 2, o.end());
  const double dw = msd(W, 1);
  if (!(dw < 0.3 * o[o.size() / 2])) return false;
  P = 0;
  const long km = (long)(n / 3) / W;
  if (km >= 3) {
    std::vector<double> e(km + 2, 0.0);
    for (long k = 1; k <= km + 1; k++) e[k] = msd(k * W, 1);
    double best = 1.3 * e[1];
    for (long k = 2; k <= km; k++)
      if (e[k] < best && e[k] < e[k - 1] && e[k] < e[k + 1]) {
        best = e[k];
        P = k * W;
      }
  }
  return true;
}

static const uint8_t MAGIC[4] = {'G', '1', '6', 1};

static bool compress(const Buf &in, Buf &out) {
  const size_t sz = in.size(), n = sz / 2;
  if (n < 4096) return false;
  std::vector<int> s(n);
  int mn = 65535, mx = 0;
  for (size_t i = 0; i < n; i++) {
    s[i] = in[2 * i] | (in[2 * i + 1] << 8);
    mn = std::min(mn, s[i]);
    mx = std::max(mx, s[i]);
  }
  long W = 0, P = 0;
  if (!detect(s, W, P)) return false;
  int B = 1;
  while ((mx - mn) >> B) B++;
  for (size_t i = 0; i < n; i++) s[i] -= mn;
  out.assign(MAGIC, MAGIC + 4);
  for (int i = 0; i < 8; i++) out.push_back((uint8_t)((uint64_t)sz >> (8 * i)));
  for (int i = 0; i < 4; i++) out.push_back((uint8_t)((uint32_t)W >> (8 * i)));
  for (int i = 0; i < 4; i++) out.push_back((uint8_t)((uint32_t)P >> (8 * i)));
  out.push_back((uint8_t)B);
  out.push_back((uint8_t)mn);
  out.push_back((uint8_t)(mn >> 8));
  if (sz & 1) out.push_back(in[sz - 1]);
  AC ac;
  ac.out = &out;
  codec(s, n, W, P, B, ac, true);
  ac.flush();
  return true;
}

static bool decompress(const Buf &in, Buf &out) {
  if (in.size() < 23 || memcmp(in.data(), MAGIC, 4)) return false;
  uint64_t sz = 0;
  uint32_t W = 0, P = 0;
  for (int i = 0; i < 8; i++) sz |= (uint64_t)in[4 + i] << (8 * i);
  for (int i = 0; i < 4; i++) W |= (uint32_t)in[12 + i] << (8 * i);
  for (int i = 0; i < 4; i++) P |= (uint32_t)in[16 + i] << (8 * i);
  const int B = in[20];
  const int mn = in[21] | (in[22] << 8);
  size_t pos = 23;
  if (sz > ((uint64_t)1 << 32) || B < 1 || B > 16 || W < 8) return false;
  const size_t n = (size_t)(sz / 2);
  uint8_t tail = 0;
  if (sz & 1) {
    if (pos >= in.size()) return false;
    tail = in[pos++];
  }
  std::vector<int> s(n, 0);
  AC ac;
  ac.start(in.data() + pos, in.size() - pos);
  codec(s, n, (long)W, (long)P, B, ac, false);
  out.resize((size_t)sz);
  for (size_t i = 0; i < n; i++) {
    int x = s[i] + mn;
    out[2 * i] = (uint8_t)x;
    out[2 * i + 1] = (uint8_t)(x >> 8);
  }
  if (sz & 1) out[(size_t)sz - 1] = tail;
  return true;
}

int main(int argc, char **argv) {
  if (argc < 4) return 2;
  for (int i = 0; i < 4096; i++) {
    double p = (i + 0.5) / 4096.0;
    STR[i] = std::log(p / (1.0 - p));
  }
  Buf in = load(argv[2]), out;
  if (argv[1][0] == 'c') {
    if (!compress(in, out)) return 3;
  } else {
    if (!decompress(in, out)) return 3;
  }
  save(argv[3], out);
  return 0;
}

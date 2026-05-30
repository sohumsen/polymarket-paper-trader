# How it works (and why)

A walk through the bot's pipeline, with the math written out and a worked
example. If you just want results, see [public/blog.md](public/blog.md).
If you want to read the code, [engine.py](engine.py) is where everything
lives — line refs are included throughout.

## The premise

Polymarket lists prediction markets like _"Will Rory McIlroy win the 2026
Masters?"_ At any moment the YES contract trades at some price $p \in
(0, 1)$. If YES is correct at resolution, the contract pays $1; otherwise
it pays $0. So **the price is the market's implied probability** that
YES happens.

If our own probability estimate $q$ differs from the market price $p$, we
have an opinion. The question this project asks is: **can an LLM produce
a $q$ that is better-calibrated than $p$, often enough to be profitable
after sizing and execution costs?** Spoiler from the
[blog](public/blog.md): after one month, the honest answer is "not yet."
But the pipeline is what makes the question answerable at all.

## Pipeline overview

For every market the scanner picks up, the bot runs four steps:

```
Polymarket API  →  ensemble forecast  →  calibration  →  Kelly sizing  →  trade
                                              ↑                              │
                                              │                              ↓
                                     resolved outcomes  ←──────────  exit rules
```

That feedback loop in the bottom right — resolved outcomes flowing back
into calibration — is the whole point. The bot learns how wrong it is
and corrects for it on the next forecast.

Now each step.

## 1. Forecasting with 5 personas

Why 5 personas instead of one prompt? Two reasons.

**Ensemble noise reduction.** If five _independent_ forecasters each have
unbiased noise $\epsilon_i \sim \mathcal{N}(0, \sigma^2)$ around the true
probability, averaging them reduces the noise to $\sigma / \sqrt{5}$.
That assumes independence, which we approximate by giving each persona a
different mandate — generalist analyst, base-rate Bayesian, momentum
trader, contrarian, calibrated forecaster. They share a language model
but disagree on what to weight.

**Variance is signal.** When all five personas land within ±0.02 of each
other, that's an informative consensus. When they spread by ±0.15, the
question is genuinely hard and the bot should size down (or skip
entirely).

### Combining: log-odds, not probability

The naïve way to combine forecasters is to average their probabilities:
$\bar p = \frac{1}{n}\sum_i p_i$. **Don't do this.** It systematically
underweights extreme forecasts that the ensemble agrees on.

Consider three forecasters all saying $p_i = 0.95$. The average is $0.95$
— same as any one of them. But if three independent calibrated forecasters
all say 0.95, the right combined probability is higher, because the
agreement itself is evidence. Averaging in probability space throws that
information away.

The fix is to combine in **log-odds space**:

$$\text{logit}(p) = \log\frac{p}{1-p}$$

$$\bar L = \frac{1}{n}\sum_i \text{logit}(p_i)$$

$$\bar p = \sigma(\bar L) = \frac{1}{1+e^{-\bar L}}$$

Averaging log-odds is equivalent to multiplying odds, which is what
Bayes' rule does when you stack independent pieces of evidence. The math
is in [engine.py:54](engine.py#L54) (logit + sigmoid) and the combiner
at [engine.py:712](engine.py#L712).

We then apply an _extremize transform_ — multiply $\bar L$ by a constant
$\gamma > 1$ before passing through sigmoid — to correct the well-known
conservative bias that ensemble averaging introduces. Default $\gamma =
1.3$.

## 2. Calibration

Even after ensemble combination, the LLM's probabilities are systematically
wrong in ways we don't know in advance. Maybe it's overconfident on
high-probability events, underconfident on low ones, or biased toward
nice round numbers like 0.5.

We can't fix what we can't measure, so the bot **logs every forecast it
makes** (`calibration.json`) and waits for each market to resolve. After
$\geq 20$ resolved predictions we can fit a calibration model.

### Platt scaling

Platt scaling is the simplest possible calibration model:

$$p_\text{calibrated} = \sigma\big(a \cdot \text{logit}(p_\text{raw}) + b\big)$$

Two free parameters: $a$ controls how aggressive the bot's confidence
should be ($a = 0.5$ means soften it by half, $a = 2$ means sharpen it),
and $b$ shifts the whole forecast up or down. We fit $a, b$ by gradient
descent on log loss against the resolved outcomes
([engine.py:189](engine.py#L189)).

If the bot is overconfident, the fit learns $a < 1$ and pulls every new
forecast back toward 0.5. If it's biased upward, $b < 0$ shifts everything
down. Crucially, **the bot doesn't know in advance which way it's wrong**
— Platt scaling figures that out from the data.

### Brier score

To know whether the bot is _improving_ over time, we score every forecast
against its outcome using the Brier score:

$$B = \frac{1}{n}\sum_i (f_i - o_i)^2$$

where $f_i$ is the bot's forecast and $o_i \in \{0, 1\}$ is the actual
outcome. Lower is better. Critically, we also compute the Brier score of
the **market price** on the same set of resolved markets. If our Brier
isn't lower than the market's, we have no edge — the market is a better
forecaster than us, and we should size down or stop.

Brier also decomposes into _reliability_ + _resolution_ – _uncertainty_
([engine.py:119](engine.py#L119)), letting us tell "calibrated but
uninformative" apart from "informative but miscalibrated" — different
problems with different fixes.

## 3. Sizing with Kelly

We have $q$ (our calibrated probability) and $p$ (the market price). Now
how much do we bet?

For a binary contract that pays $1 on YES at entry price $p$, the
Kelly-optimal fraction of bankroll is:

$$f^* = \frac{q - p}{1 - p}$$

This is the fraction of bankroll that maximises the expected log-growth
of equity ([Kelly 1956]). Quick derivation: if you bet fraction $f$ at
price $p$, your bankroll multiplier is $1 + f \cdot \frac{1 - p}{p}$ on
a win and $1 - f$ on a loss. Maximising $q \log(\text{win}) + (1-q)
\log(\text{loss})$ over $f$ gives the formula above.

### Why quarter-Kelly

Full Kelly assumes you know $q$ exactly. We don't — even after Platt
scaling, $q$ is a noisy estimate. Kelly is famously unforgiving of
overestimation: if your $q$ is biased upward by even a few percent, full
Kelly causes catastrophic drawdowns. The fix is **fractional Kelly**:
multiply $f^*$ by some $k \in (0, 1)$.

We use $k = 0.25$ (quarter-Kelly). It sacrifices ~25% of the expected
growth rate in exchange for roughly 4× lower variance. Empirically it
also makes the strategy survive being wrong about $q$.

We also cap each bet at 10% of starting balance and total same-day-
resolution exposure at 15%. These are structural caps, not parameters to
tune — the day-cap exists because two correlated NBA bets cost the bot
18% of bankroll in week one of paper trading.

### Worked example

Suppose the bot scans this market:

> "Will Bitcoin close above $100k on December 31?"
> Current YES price: **$p = 0.42$**

The 5-persona ensemble produces these raw probabilities:

| persona | $p_i$ | $\text{logit}(p_i)$ |
|---|---:|---:|
| generalist | 0.55 | +0.20 |
| base-rate | 0.48 | -0.08 |
| momentum | 0.62 | +0.49 |
| contrarian | 0.50 | 0.00 |
| calibrated | 0.56 | +0.24 |

Combined log-odds: $\bar L = (0.20 - 0.08 + 0.49 + 0 + 0.24)/5 = 0.17$.

After extremize ($\gamma = 1.3$): $L' = 0.221$, giving $\bar p =
\sigma(0.221) = 0.555$.

Suppose the fitted Platt parameters are $a = 0.85, b = -0.05$ (slightly
soften, slightly shift down). Then:

$$q = \sigma(0.85 \cdot 0.221 + (-0.05)) = \sigma(0.138) = 0.534$$

So our calibrated estimate is $q = 0.534$, market is $p = 0.42$, edge is
$+0.114$. Kelly fraction:

$$f^* = \frac{0.534 - 0.42}{1 - 0.42} = \frac{0.114}{0.58} = 0.197$$

Quarter-Kelly: $0.197 \cdot 0.25 = 0.049 \approx 4.9\%$ of bankroll. On
a $1{,}000 bankroll, that's **a $49 bet on YES at $0.42**, buying
$49/0.42 \approx 117$ shares. If the market resolves YES we receive
$117; if NO we lose the $49.

## 4. Exits

The exit rules live in `check_exits()` in [engine.py](engine.py):

**Edge erosion (take-profit).** If the market moves toward our estimate
after entry, the remaining edge shrinks. Once $<20\%$ of the original
edge is left, we close the position and take the partial profit. The
Polymarket data shows this rule is doing most of the strategy's real
work — it's the only category of exits that's net profitable across the
resolved trades in the public log.

**Thesis re-check (not a price stop-loss).** The original build also ran
a $-50\%$ price stop-loss, and the data was damning: across 49 stops it
lost \$1,227 (−88% ROI) and gave back nearly every dollar the take-profit
rule earned. In a binary market an adverse price move *without new
information* increases our edge — selling there realises a full loss at
the moment expected value is highest. So the stop-loss is now **off by
default**. Instead, when a position moves hard against us (≥12pp) and
there's still time to matter, we re-run a fresh, cheap analysis. We cut
**only if that fresh view says the thesis is genuinely broken** (negative
edge on our side); otherwise we hold to settlement and refresh our
estimate. Everything else rides to resolution, where the most we can lose
is the stake anyway.

There's also a subtler third rule: **edge-eroded exits don't update the
calibration log immediately.** Calibration cares about the binary
outcome of the underlying market, not whether we made money on it. So
even after an early exit, the bot still has to wait for the market to
actually close and record that outcome — otherwise the calibration data
becomes a biased sample of "events where the price didn't converge to
our view," which silently corrupts Platt scaling. The fix and the bug
story are in the [blog](public/blog.md#what-id-do-differently---and-the-bug-i-found).

## What this teaches you about the math

A few things worth noticing:

- **Log-odds is the right space for combining probabilities.** Averaging
  in probability space is a beginner mistake that costs information.
- **Calibration is not optional.** Any forecaster — human, LLM, classical
  model — produces probabilities that need to be re-mapped onto realised
  frequencies before they can drive decisions. Without Platt or similar,
  even a "smart" forecaster can be a worse bettor than the market.
- **Kelly is the answer to the sizing question, but only if you know
  $q$.** Since we don't, fractional Kelly is the unavoidable hedge.
- **Brier scoring against the market baseline is the right scoreboard.**
  Beating an absolute Brier threshold means nothing — beating the market's
  Brier on the same resolutions is what determines whether you have edge.

Each of those is one paragraph here and a whole research literature in
real life. The bot is a small, opinionated implementation of all four.

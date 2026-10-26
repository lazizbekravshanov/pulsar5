# Impact data — every figure and where it came from

Checked 26 Oct 2026. Used on deck slides 5 (*What the gap cost*) and 6 (*The forecast was not wrong*).

Each number below is traceable to a primary source. If a judge asks "where did that come from", the answer is on this page.

---

## Primary source

**NHC Tropical Cyclone Report, Hurricane Ian (AL092022)** — the official post-storm analysis.
https://www.nhc.noaa.gov/data/tcr/AL092022_Ian.pdf

This is a US Government work and public domain. Everything below marked *(TCR)* is quoted or derived from it directly, not from press coverage.

---

## Cost and casualties

| Figure | Value | Source |
|---|---|---|
| Total US damage | **$112.9 B** (90 % CI $86.8–135.9 B) | TCR, Casualty and Damage Statistics |
| Damage in Florida | **$109.5 B** — costliest hurricane ever to affect the state | TCR |
| Rank | **Third-costliest US hurricane on record** | TCR, via NOAA NCEI |
| Total US fatalities | **156**, of which **66 direct** | TCR |
| Deadliest hazard | **Storm surge, 41 deaths** | TCR |
| Surge deaths in Lee County | **36 of those 41** | TCR |
| Indirect deaths | **90**; leading cause *lack of access to timely medical care* (18), then accidents (16) and cardiac events (16) | TCR |
| Age skew | Distribution peaks toward the elderly, "consistent with other hurricane landfalls where the oldest die at the highest rates" | TCR |
| Lee County structures | **52,514 impacted**, 5,369 destroyed, 14,245 major damage | TCR |
| Fort Myers Beach | ~900 structures totally destroyed, 2,200 damaged | TCR |

**Why the age skew is on the slide:** it is the direct justification for `vulnerability_score` in `app/agents/analyst.py` weighting over-65 share, no-vehicle households, disability count and poverty rate rather than raw population.

---

## The forecast was not the failure

This is the argument on slide 6, and it rests on four facts from the TCR's own *Forecast and Warning Critique*.

**1. The cone always contained the landfall.**

> "the forecast track cone of uncertainty did contain the landfall location for all advisory cycles"

The information needed to protect Lee County was present in every advisory. This is the single most important sentence in the report for this project.

**2. Ian's track forecast was normal, not bad.** Official (OFCL) track errors in nautical miles, TCR Table 5a:

| Lead time | 12 h | 24 h | 36 h | 48 h | 72 h | 96 h | 120 h |
|---|---|---|---|---|---|---|---|
| **Ian (OFCL)** | 18.3 | 32.0 | 50.9 | 67.8 | 110.5 | 140.3 | 141.8 |
| 5-yr mean 2017–21 | 23.6 | 35.5 | 47.6 | 61.4 | 91.3 | 125.6 | 172.1 |

Ian beat the five-year average at 12, 24 and 120 h and was comparable at 36 and 48 h. **67.8 n mi ≈ 78 statute miles of error at two days out is simply what a good forecast looks like.** The TCR attributes the residual error mainly to cross-track error with a consistent westward bias, and notes that storms paralleling a coastline are intrinsically harder because a small heading change moves landfall a long way.

**3. The surge forecast tripled inside a day.** At Fort Myers Beach (TCR, Storm Surge Forecasts and Warnings, Fig. 31):

- initial peak forecast **4–7 ft AGL**
- morning of 27 Sep, as the track shifted south → **8–12 ft**
- morning of 28 Sep, after recon found further intensification → **12–18 ft**
- measured: **12.70 ft above MHHW** (USGS wave-filtered sensor)

Watch and warning were issued 48 h and 30 h before tropical-storm-force winds arrived.

**4. Thirty miles decided everything.**

| Location | Peak water level above MHHW |
|---|---|
| Fort Myers Beach | **12.70 ft** |
| Sanibel Island | **12.58 ft** |
| Pirate Harbor (< 30 mi north) | **4.47 ft** |
| Tampa Bay (Old Port Tampa) | **4.38 ft BELOW** mean lower low water |

The TCR notes the Storm Surge Warning "did not verify north of Englewood due to a southward shift in the track", and that sensors from Port Charlotte through Tampa Bay stayed dry. Tampa Bay, where the early forecast pointed, experienced a reverse surge.

---

## The evacuation timing question

Context for why Lee County is the focus, from contemporaneous reporting (October 2022) rather than the TCR:

- Lee County issued its mandatory evacuation order later than neighbouring counties. **Charlotte County, immediately north, ordered barrier-island evacuations the preceding Monday.**
- County officials attributed the timing to forecast uncertainty, saying the path was not yet clear enough the day before.
- Reporting noted Lee's own 2018 emergency management plan recommends evacuating Zone A at a 10 % chance of six-foot storm surge, a threshold that appears to have been met earlier than the order.

**Handle this carefully.** This is journalism and a contested question of judgement under genuine uncertainty, not a settled finding. The deck does not claim Pulsar 5 would have saved lives, and neither should the video. What it claims is narrower and defensible: the system surfaces a high-vulnerability zone outside the current cone to the operator as an explicit decision, instead of letting it fall out of the plan silently.

Sources: [WUSF](https://www.wusf.org/weather/2022-10-25/did-lee-county-just-in-time-evacuation-protocol-hurricane-ian-cost-lives) · [NPR](https://www.npr.org/2022/10/03/1126517090/officials-face-questions-over-the-late-evacuation-order-in-floridas-lee-county)

---

## It keeps happening

| Event | Damage | Deaths | Source |
|---|---|---|---|
| Helene (Sep 2024) | $79.6 B | 219 | NOAA climate.gov / NCEI |
| Milton (Oct 2024) | $34.3 B | 32 | NOAA climate.gov / NCEI |
| Ian (Sep 2022) | $112.9 B | 156 | NHC TCR / NOAA NCEI |

Helene and Milton together exceeded $100 B across six states inside two weeks. NOAA ranked 2024 fourth among years by billion-dollar-disaster cost.

**Caveat worth knowing:** NOAA's billion-dollar disasters product has had methodology revisions and its future has been in question, so figures occasionally differ slightly between NOAA products (Helene appears as both $79.6 B and $78.7 B). The deck uses the climate.gov figures and the TCR for Ian.

---

## The other half: over-warning

Blanket evacuation is not free, which is why Pulsar 5's buffer under-covers deliberately and escalates rather than expanding scope.

- ~**3.5** hurricane storm warnings declared per year in the US, averaging ~**417 miles** of coastline each
- hurricane-force winds reach only about **25 %** of the warned area
- estimated **> $1 B/year** in avoidable evacuation spend

Source: Frontiers in Public Health **7**:149 (2019), https://doi.org/10.3389/fpubh.2019.00149

Per-mile evacuation cost estimates range roughly **$400 K–$1 M per mile** and are all rough. Whitehead's survey-based work on Hurricane Bonnie (1998) found real costs well below the widely quoted "$1 million per mile" rule of thumb. Cite the range, not a single number.

---

## A note on HuggingFace

Worth saying plainly: HuggingFace was not the right source for this.

The authoritative numbers for US hurricane impact live in NHC Tropical Cyclone Reports, NOAA NCEI and OpenFEMA — all public domain, all primary. HF hosts derived ML datasets (satellite building-damage corpora and similar) which are useful for computer-vision damage assessment, but they are *derived* products and would be a weaker citation for a damage or casualty figure than the government report the figure originally came from.

Pulsar 5's data layer is Zetaris regardless, so an HF dataset would also have to be registered as a source there rather than loaded directly — and none of these figures need it.

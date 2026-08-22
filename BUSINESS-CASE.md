# native2py — Business Case

**Audience:** CTO, Directors of Engineering, and the budget holder for legacy
modernization.
**Source:** <https://github.com/ravikings/native2py>
**Companion documents:** [`design.md`](design.md) (the specification),
[`README.md`](README.md) (what exists today),
[`tools/native2py/docs/production-readiness.md`](tools/native2py/docs/production-readiness.md)
(the honest gap list).

---

## 1. The problem in one sentence

We have decades of C++ and FORTRAN that computes correctly and that nobody
dares touch. Every new product needs those numbers: a web portal, an
optimizer, an ML pipeline, a customer-facing API. Today there are three ways
to get them.

| Option | What it costs |
|---|---|
| Rewrite the physics in Python | Months of engineering plus full numerical re-validation by domain experts |
| Hand-write bindings | A specialist skill, one-off per API, rots on the next signature change |
| Shell out to a batch executable and parse text files | Brittle, slow, and invisible to monitoring |

native2py is the fourth option: **leave the native code exactly as it is and
generate the bridge.**

---

## 2. The pathfinding principle

You do not modernize a 30-year codebase by deciding up front to modernize it.
You send one thin path all the way through to production — one routine, source
→ binding → wheel → HTTP → container — and see what breaks. Then you widen the
path.

That is why [`libraries/petro/`](libraries/petro/) exists in this repo: ~4,800
lines of period-accurate 1988–2004 code, with `IMPLICIT` typing, COMMON
blocks, INCLUDE decks, macro-mangled `extern "C"` bridges and a pre-standard
C++ layer. The generator was walked against that rather than against a toy
header. [`libraries/petro/FINDINGS.md`](libraries/petro/FINDINGS.md) records
what broke on the first attempt, including a bug the tool's own write-up
initially misdiagnosed.

One consequence makes this safe to hand to a team. The `sim` service is mostly
*refusals*: forward-declared types and raw pointers cause the generator to
decline, with a reported reason per symbol. It fails at generation time with
an explanation rather than at 3am with a segfault in a worker.

---

## 3. Where the money is: bindings are no longer written

Every other number in this document follows from this one.

### 3.1 What a hand-written binding actually costs

For a real legacy API, a binding runs to eight steps:

1. Read the native header or deck and work out the true signatures — for F77,
   that means resolving `IMPLICIT` typing, COMMON blocks, and INCLUDE
   expansion by hand.
2. Decide the argument intent (in / out / inout) that the source never
   declared.
3. Write the pybind11 or f2py glue, including array shape and ownership rules.
4. Write the CMake / build integration and get it to link — several classes of
   failure only surface at first import, not at compile.
5. Write the Python package wrapper.
6. Write tests.
7. Validate that the numbers coming out match the numbers the native code
   produced before.
8. Do all of it again the next time a native signature changes.

Steps 1–6 consume a bindings specialist. Step 7 consumes a domain expert,
which is why it costs more. Step 8 turns the whole thing from a one-time cost
into a permanent tax.

### 3.2 What the market pays for those two people

Both roles are scarce, and both are priced accordingly.

The bindings specialist, an HPC or scientific-computing engineer fluent in
C++, Fortran and the CPython extension layer:

| Source | Figure (US, 2026) |
|---|---|
| Comparably — HPC Software Engineer, average | **$148,808** |
| ZipRecruiter — HPC Engineer, 25th–75th percentile | **$113,403 – $188,314** |
| ZipRecruiter — broader HPC roles, 25th–75th | $83,500 – $133,500 (90th: $158,500) |
| 6figr — HPC Engineer | $83k – $126k |
| San Jose, CA average total comp | $293,804 |

The domain expert who certifies the numbers. A reservoir engineer here;
substitute your own quant, physicist or process engineer:

| Source | Figure (US, 2026) |
|---|---|
| ZipRecruiter — 25th–75th percentile | **$106,000 – $146,000** (90th: $166,500) |
| Glassdoor — average / 25th–75th | $205,653 / $158,380 – $271,995 |
| PayScale — average, 10th–90th | $136,375 / $92,000 – $214,000 |
| SalaryExpert — average | $137,706 |
| Senior (8+ years) average | **$171,611** |

Buying the work in rather than hiring for it costs more, and the cost is
visible on an invoice rather than buried in headcount:

| Source | Figure (2026) |
|---|---|
| Application-modernization consulting, realistic enterprise band | **$150 – $350 / hour** |
| Full published spread, US & EU qualified consultants | $75 – $850 / hour |
| Independent practitioners, 8+ years — recommended floor | $150 – $200 / hr, or **$1,500 / day** |
| Offshore-led delivery | from ~$20 / hour |

Legacy-Fortran modernization sits at the upper end of the $150–$350 band,
being the specialization that commands the premium.

#### Converting to a loaded day rate

Salary × ~1.35 for employer burden, ÷ ~220 working days:

| Role | Basis | **Loaded day rate** |
|---|---|---|
| Bindings specialist (employee) | $115k – $190k salary | **≈ $700 – $1,170** |
| Domain expert (employee) | $120k – $210k salary | **≈ $735 – $1,290** |
| Senior domain expert (employee) | $171.6k salary | ≈ $1,050 |
| Either role (contracted) | $150 – $350 / hr | **≈ $1,200 – $2,800** |

Use `A ≈ $700–$1,200` (specialist-day) and `B ≈ $750–$1,300` (expert-day) for
in-house work; roughly double both if the work is contracted out.

**Per exposed API, hand-written:**

| Step | Who | Days (typical) |
|---|---|---|
| Understand the native signature | Specialist | 0.5 – 3 |
| Write bindings + build integration | Specialist | 1 – 5 |
| Get it to link and import | Specialist | 0.5 – 3 |
| Python package + tests | Specialist | 0.5 – 2 |
| Numerical validation | Domain expert | 1 – 5 |
| **Subtotal** | | **~3.5 – 18 days** |

**Per exposed API, generated:**

| Step | Who | Days (typical) |
|---|---|---|
| Add the symbol to `native2py.yaml` | Any engineer | ~0 |
| `native2py generate` / `build` | Any engineer | ~0 |
| Review refusals and unsupported types | Any engineer | 0 – 1 |
| Record and eyeball `golden.json` | Domain expert | 0.25 – 1 |
| **Subtotal** | | **~0.25 – 2 days** |

#### Priced out

Applying the loaded day rates above:

| Per exposed API | In-house | Contracted |
|---|---|---|
| Hand-written | **$2,500 – $22,000** | **$4,000 – $43,000** |
| Generated | **$200 – $2,500** | $300 – $4,000 |
| **Saving per API** | **≈ $2,300 – $19,600** | **≈ $3,700 – $39,000** |

The `petro` library alone exposes **12 routines**, which at in-house rates is
roughly **$28,000 – $235,000** of avoided one-time work. That figure excludes
the recurring maintenance in §3.3 and the validation cost in §3.4, both of
which would otherwise repeat on every rebuild.

The saving is about an order of magnitude per API, and it falls on the two
scarcest resources in the building: the person who can write bindings, and the
person who can certify the numbers.

### 3.3 The recurring cost that goes to zero

Bindings, CMake, the Python package, the FastAPI service, tests and the
Dockerfile are all regenerated on demand:

```bash
native2py generate petro
```

What is *not* regenerated is the code a human wrote: `native/`,
`native2py.yaml`, and the recorded answers in `golden.json`.

A native signature change becomes a re-run rather than a maintenance ticket
assigned to a specialist, so binding maintenance stops being a line item in
the budget. For a portfolio of N exposed APIs, this removes an ongoing
`N × (change frequency) × (specialist days)` charge that compounds every year
the codebase stays alive.

### 3.4 Validation cost is bounded, not eliminated

`golden.json` pins what the bindings return. In the worked example under
`services/pvt/`, the 1988 correlations called at 35 °API, 0.75 gas gravity,
180 °F and 2500 psia give **Rs = 610.696 scf/stb** and **Bo = 1.34069**,
values a reservoir engineer can check by hand.

```bash
native2py golden record pvt    # -> services/pvt/golden.json
native2py golden verify pvt    # 4 entry point(s) unchanged
```

Commit the file, and any rebuild that moves a number fails.

Financially, the expert's time is spent once, at record time. Every
subsequent rebuild, dependency bump, compiler upgrade or refactor is verified
by CI at zero marginal expert cost. Without it, each of those events is either
a re-validation exercise or, more commonly, an unmanaged risk that nobody
prices at all.

### 3.5 A second cost axis: agents pay in context, not hours

The same argument holds when the engineer is an AI coding agent, but the
currency changes. For a person the saving is hours. For an agent it is
context, which decides whether the task finishes at all.

| Binding one 20-method C++ class | What enters the context window | Output tokens |
|---|---|---|
| Hand-written | ~500 lines emitted (pybind11 TU, CMakeLists, pyproject, package init, FastAPI app, tests), then 3–4 compile and link repair rounds, all resident afterwards | 20,000 – 40,000 |
| Generated | One command and a four-line checklist. The generated files never enter context, because there is no reason to read them | 300 – 500 |

The hand-written figure is dominated by repair, not by the first draft. Three
or four compile-or-link rounds is normal for template-adjacent code, and
`include/`-vs-`src/` layout problems and undefined-symbol-at-import failures
reliably burn several.

Three effects matter more than the ratio:

- **The repair loop disappears rather than shrinks.** The generator emits code
  that already compiles for the constructs it accepts, and refuses the rest
  with a reason. The agent never enters the read-error → re-emit → rebuild
  cycle that dominates hand-written binding cost.
- **`golden verify` is a correctness signal the agent cannot fake.** Left to
  itself, an agent asked whether a re-hosted library still works writes tests
  from its own reading of the source, which validates its interpretation
  rather than the numbers. `golden record` / `golden verify` gives a real
  pass/fail on the answers for a couple of hundred tokens.
- **Correctness stops depending on how much context is left.** A hand-written
  binding degrades as the window fills, with later methods getting less
  attention than earlier ones. Generated output is identical at the start of a
  session and at 80% context.

> **On these token figures.** Estimates derived from the size of the generated
> artifacts, not a measured benchmark. The ratio is not a close call, and it
> is structural rather than a matter of prompting the agent better, but the
> absolute numbers are not data.

**This axis is addressable today.** `tools/native2py/skill/` is a Claude Code
skill, symlinked to `.claude/skills/native2py`, that makes a coding agent
invoke the CLI instead of hand-writing bindings. It encodes what an agent has
no way to infer from `--help`: that `suggest` exists, so it should not read
headers itself to choose a starting file; that a `regex reader` line means the
parse is missing symbols and must not be trusted; that skipped-binding reasons
should be relayed rather than summarized; and that generated files are
rewritten every run, so hand-edits are lost.

It also carries the production-readiness gap list, so an agent raises §6
unprompted rather than handing over a service with no auth as though it were
finished. For a governance conversation that matters as much as the token
count: it makes the honest disclosure the default behaviour rather than
something a reviewer has to catch.

Installing it in another project:

```bash
mkdir -p .claude/skills
cp -r /path/to/native2py/tools/native2py/skill .claude/skills/native2py
```

Full argument in "Use it as an agent skill" in
[`tools/native2py/README.md`](tools/native2py/README.md).

### 3.6 Where cost does *not* go away

Stated plainly so the model is not oversold:

- **Writing the native code.** Unchanged. This tool does not rewrite anything.
- **Choosing what to expose.** A design decision, and it should be. Exposing
  everything is explicitly a non-goal.
- **Coarse-graining chatty APIs.** A call-per-datapoint API will be slow
  through any binding layer. That is an API design cost, not a tooling cost.
- **The platform gaps in §6.** Auth, isolation, CI/CD and Kubernetes are real
  work that is not yet done.

---

## 4. For the CTO / Director: the monetary case

### 4.1 Five effects, in the order they hit the P&L

**1. Migration cost collapses from a rewrite to a config file.**
The unit of work becomes "expose these 12 routines" instead of "port the PVT
module to Python and prove it still works." No physics is re-derived, so no
physicist has to re-derive it. That is the difference between a multi-quarter
programme and a sprint.

**2. Numerical risk is bounded.**
"Did the re-host change our answers?" goes from a validation exercise measured
in expert-weeks to a green check in CI. For anything regulated, contractual,
or reported to a customer, this is usually the line item that gets the project
approved at all, and the strongest defence if the answer is ever challenged
after the fact.

**3. Key-person risk drops, and the bottleneck moves off the critical path.**
Today the two people who can compile the FORTRAN gate every downstream
project, and one of them is closer to retirement than to the roadmap.
Afterwards, Python developers write `from petro import ...` and never learn
f2py. The native code stays the computational core, but the scarce skill is no
longer required for each new feature. That is a throughput gain across every
team consuming these numbers, well beyond the modernization team itself.

**4. It is incremental, therefore cancellable.**
This is the funding case as much as the technical one. Phase 1 is embedding:
bindings inside the existing application, no deployment change, no
architecture review. Containerizing and extracting to services are separate,
later decisions, and the native implementation does not change across them.
You can fund one service, stop, and still hold something of value. A rewrite
has no useful intermediate state; this has one at every step.

**5. Optionality on the transport is preserved cheaply.**
The intermediate representation sits between the language parsers and the
binding generators so that `Python → pybind11 → C++` can later become
`Python → gRPC → C++ service` without changing calling code. That mode is not
built yet. The seam is, and the seam is the expensive part to retrofit.

### 4.2 Revenue-side effects

Cost reduction is the easy half of the argument. The larger number is usually
here:

- **Time-to-market on products that depend on these numbers.** Every portal,
  API, dashboard, or optimizer that currently waits on a binding specialist
  ships sooner.
- **Products that are currently not attempted.** Some ideas are never proposed
  because "we'd have to touch the FORTRAN." Lowering that cost changes what
  gets on the roadmap.
- **Monetizing existing computation.** A validated correlation library that is
  reachable over HTTP is a sellable or licensable asset in a way that a batch
  executable on a build server is not.
- **Extending the useful life of a paid-for asset.** The alternative to
  re-hosting is eventual forced replacement. This defers or removes a large
  future capital cost.

### 4.3 The open-API partner initiative

> **To be made specific.** This section is written against the general shape of
> a partner-API programme. It needs five facts before it goes in front of
> anyone: the partners or tier being targeted; which computations they want to
> call; the endpoint count and any committed date; the commercial model; and
> who owns auth, rate limiting and SLAs today. With those, the section names
> real bottlenecks instead of describing a category.

The partner programme needs computational endpoints that partners can call,
version against, and rely on contractually. Those computations exist today
only as FORTRAN routines and batch executables, so every partner endpoint is
gated on a bindings specialist and a validation cycle. That is the bottleneck
this removes.

Five things the partner initiative needs, and where each comes from:

| Partner-API requirement | What native2py already produces |
|---|---|
| A machine-readable contract partners can generate clients from | FastAPI generates OpenAPI schema automatically for every exposed service — no separate spec to write, and none to drift |
| A stable surface that does not break when the native code changes | Python is the stable interface, the implementation is not. Native internals can evolve behind `from petro import …`; a breaking native change fails binding generation rather than reaching a partner |
| Numbers you can stand behind contractually | `golden.json` pins the returned values. When a partner disputes a result, you have a committed, engineer-verifiable baseline and a CI check proving nothing moved |
| Independent versioning and release per endpoint | Every service is its own wheel, its own version, its own container. One partner endpoint ships without rebuilding the others |
| Endpoints added at partner-request speed | Adding a routine to the catalogue is a config entry and a regenerate — days, not a quarter |

What this does to the initiative's timeline. Per §3, each new partner
endpoint currently carries **3.5–18 specialist and expert days** of binding
and validation work before anyone writes a line of API surface. Generated,
that drops to **0.25–2 days**. Across a catalogue of a dozen endpoints, that
is the difference between a multi-quarter build-out and one sprint of
configuration, and the removed work is the work only two people in the
building can do.

The second-order effect matters as much. The partner roadmap stops being
shaped by *what we can afford to bind* and starts being shaped by *what
partners will pay for*. Endpoints not currently proposed because the binding
cost cannot be justified become viable.

The caveat, which is on the critical path here. A partner-facing API is
the case the gaps in §6 do not yet cover: no auth, no rate limiting, no
exception handling, and a native segfault takes the worker down. Tolerable for
internal phase-1 embedding; blocking for an external partner endpoint. The
sequencing that follows:

1. Use native2py now to remove the binding and validation bottleneck and
   build the internal catalogue. That value is available today.
2. Fund the platform layer — auth, rate limiting, process isolation for
   segfaults, deployment templates — as a named, scoped workstream *for the
   partner initiative*, since the initiative is what makes it mandatory.

Those two run in parallel. What is no longer on the critical path is the
thirty-year-old FORTRAN, which was always the part nobody could schedule.

### 4.4 How to size it for your organization

Three numbers, all of which you can obtain internally in a day:

1. **N.** How many native APIs do downstream teams want and not have? Ask the
   teams, not the platform group.
2. **D.** How long does a request for a new binding take, wall-clock, from ask
   to usable? Pull the last three from the tracker.
3. **C.** What is the current annual spend on maintaining existing bindings
   and re-validating after changes?

Near-term saving ≈ `N × (hand-written days − generated days) × loaded rate`,
plus `C` recurring, plus the value of reducing `D`. That last one is usually
what the business actually cares about, because it is the one blocking
revenue.

---

## 5. Recommended first move

Do not propose a programme. Propose a path.

1. Pick one routine that two or three downstream teams are currently
   waiting on.
2. Take it end to end: expose, build, record golden values, ship the wheel to
   one of those teams.
3. Measure the thing that moves budget: **how long that team waited before,
   versus after.**
4. Decide about the remaining routines with that number in hand.

Total exposure if it fails: one routine, a few days, and a `FINDINGS.md` entry
that makes the next attempt cheaper.

---

## 6. What must be said unprompted

Approving this on an incomplete picture is how the project loses credibility in
month three. As of today:

- Generated services have no auth, no rate limiting, and no exception handling.
- Native code that segfaults takes the worker down with it. There is no
  process isolation yet.
- `infrastructure/docker/` and `infrastructure/kubernetes/` are empty. No
  CI/CD pipelines or Kubernetes manifests are generated.

The accurate positioning is therefore:

> **Production-ready today as a build-time tool and as a phase-1 embedding
> story. Not yet production-ready as a public-facing service platform.**

The gaps are ordinary platform engineering: auth, a segfault-isolation
strategy, CI/CD and deployment templates. They are well understood and they
are scoped in
[`production-readiness.md`](tools/native2py/docs/production-readiness.md),
which grades the tool against `design.md`'s own requirements rather than
against a sales pitch. They are simply not done yet, and they should be costed
as part of any decision to go beyond phase 1.

---

## 7. Evidence available today

For anyone who wants to check the claims rather than take them:

- Eight generated services across C++ and Fortran, each an independently
  buildable wheel and container. Among them `sim`, which is mostly documented
  refusals, and `petro`, which exposes 12 routines from 7 F77 decks plus an
  F90 facade.
- **220 tests** in the generator's own suite, plus a second run against the
  fallback C++ parser backend.
- Real end-to-end builds, rather than unit tests alone: generated CMake fed to real
  `cmake`, `clang++` and `gfortran`, the extension imported, the FastAPI
  service exercised over HTTP. Several real bugs were found this way and not
  by inspection, including f2py's module nesting, a missing-`.cpp` link
  failure that only appears at first import, and a golden harness that
  replayed stateful F77 routines out of order.
- A recorded numerical baseline a domain expert can verify by hand
  (`services/pvt/golden.json`).

---

## 8. Sources for the rate figures in §3.2

Public US market data, retrieved 2026-08-22. Ranges vary by methodology; the
model above uses the mid-band deliberately rather than the most favourable
figure.

- [Comparably — HPC Software Engineer salary](https://www.comparably.com/salaries/salaries-for-hpc-software-engineer)
- [ZipRecruiter — HPC Engineer salary](https://www.ziprecruiter.com/Salaries/Hpc-Engineer-Salary)
- [6figr — High Performance Computing Engineer salaries](https://6figr.com/us/salary/high-performance-computing-engineer--t)
- [ZipRecruiter — Reservoir Engineer salary](https://www.ziprecruiter.com/Salaries/Reservoir-Engineer-Salary)
- [Glassdoor — Reservoir Engineer salary](https://www.glassdoor.com/Salaries/reservoir-engineer-salary-SRCH_KO0,18.htm)
- [PayScale — Reservoir Engineer salary](https://payscale.com/research/US/Job=Reservoir_Engineer/Salary)
- [SalaryExpert — Reservoir Engineer, United States](https://www.salaryexpert.com/salary/job/reservoir-engineer/united-states)
- [Application Modernization Consulting Rates — 2026 benchmark guide](https://softwaremodernizationservices.com/insights/application-modernization-consulting-rates/)
- [Cleveroad — Software development consulting rates 2026](https://www.cleveroad.com/blog/software-consulting-rates/)

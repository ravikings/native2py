# Design: the air-gapped deployment model

**Status:** proposal. Nothing here is implemented.
**Scope:** how native2py runs inside a customer's network with no path to the
internet, what that forces on the architecture *before* more of it is written,
and which parts of the hosted design survive the trip.
**Companion documents:**
[`design.md`](design.md) (the base specification),
[`design-cloud-run-lifecycle.md`](design-cloud-run-lifecycle.md) (the hosted
model, whose assumptions this document deliberately inverts),
[`design-verification-layers.md`](design-verification-layers.md) (attestation,
which §6 constrains),
[`SECURITY.md`](SECURITY.md).

The reason to write this now rather than after the first enterprise deal:
**a large share of the buyers with genuine legacy-Fortran pain are not
permitted to send that source anywhere.** National labs, defense primes,
aerospace, oil and gas reservoir simulation, weather and climate agencies,
banks with in-house quant libraries. For them, "upload your proprietary
numerical source to our cloud" is not a pricing objection or a trust
objection. It is a policy that no champion inside the account has the
authority to waive.

If that segment matters, the hosted product in
[`design-cloud-run-lifecycle.md`](design-cloud-run-lifecycle.md) is the
self-serve tier, and the enterprise product is the same engine shipped as
software. This document is about the decisions that get expensive if that
truth is discovered late.

---

## 1. "Air-gapped" is a spectrum, and the tiers are not equally hard

Customers say "air-gapped" to mean four quite different things. The
engineering cost differs by an order of magnitude across them, so the first
job is to know which one is being sold.

| Tier | What it means | Egress | Cost to support |
|---|---|---|---|
| **T1 — Private cloud** | Runs in the customer's own AWS/GCP/Azure account | Internet allowed | Low. Mostly packaging and a license check. |
| **T2 — On-prem, proxied** | Customer datacentre, outbound through an allowlisting proxy | Narrow, audited | Moderate. Every outbound call must be enumerable and justifiable. |
| **T3 — Air-gapped** | No network path to the internet, ever. Updates arrive on physical media or through a one-way diode | None | High. This document. |
| **T4 — Classified** | T3 plus cleared personnel, no vendor access to the environment under any circumstances, possible source escrow | None | Very high. Sell only with a services contract attached. |

**T3 is the design target.** T1 and T2 fall out of it for free — a system that
can run with zero outbound calls can trivially run with some. The reverse is
not true, and retrofitting is where the cost is.

T4 is a business decision, not an architecture one; it adds source escrow,
personnel constraints, and a support model where you may never see the
environment at all. Nothing here prevents it, but do not price it as T3.

---

## 2. The forcing function

**The product must be able to run indefinitely with zero calls home, and it
must be possible to prove that to a hostile reviewer.**

Everything else in this document is a consequence. Two things follow
immediately, and both are cheap now and painful later.

**One codebase, never a fork.** The moment there is an `enterprise/` branch,
the two products diverge, and every hosted fix has to be re-landed by hand
against a version from two quarters ago. The deployment target is
configuration, behind a platform abstraction (§3).

**All egress through one injectable client.** Every outbound HTTP call in the
codebase — telemetry, license, update check, package fetch, error reporting —
goes through a single client that can be constructed in a disabled state. Then
"we make no outbound calls" is a test that fails the build, not a claim in a
sales deck. A reviewer at a defense prime will run `tcpdump` against your
product; the assertion should already be in your CI.

---

## 3. The platform abstraction

Six seams separate the hosted product from the shipped one. Everything above
them is identical code.

| Seam | Hosted | Air-gapped |
|---|---|---|
| Compute | Cloud Run Service / Job | containerd or K8s with a gVisor `RuntimeClass`; Docker + `runsc` for single-node |
| Registry | Artifact Registry | Bundled OCI registry (`distribution`, zot, or Harbor if they already run one) |
| Object store | GCS / R2 | MinIO, or a plain filesystem path on a single node |
| Database | Cloud SQL | Bundled Postgres, or the customer's existing instance |
| Secrets | Secret Manager | Kubernetes secrets, or SOPS/age with a customer-held key |
| Identity | Hosted auth | OIDC/SAML against their IdP; local accounts as a fallback |

Two more seams are less obvious and cause more trouble:

**Certificates.** ACME is impossible without egress, so Let's Encrypt is out.
The install must accept a customer-supplied certificate and private key, and
must trust a customer-supplied CA bundle — their internal PKI is almost
certainly a private root. Any hardcoded assumption that TLS "just works"
becomes an install-day blocker. Support for a self-signed fallback is required
for evaluation installs, and it must be an explicit, logged choice rather than
a silent default.

**DNS.** There is no `*.native2py.app` wildcard. Projects are reachable at
whatever the customer's DNS gives you — a path prefix, a subdomain of their
internal zone, or a port. Per-project routing must not assume it owns a
wildcard domain.

**Consequence for the hosted design:** the router in
[`design-cloud-run-lifecycle.md`](design-cloud-run-lifecycle.md) §2 must key
off a project identifier extracted by configurable strategy (subdomain, path
prefix, or header), not off a hardcoded subdomain scheme.

---

## 4. The release bundle

The deliverable is a single signed archive, transferred on physical media,
that contains everything needed to install and run with no network. Producing
it is a CI job, and **it must exist from the first release**, even before
anyone has bought it — a bundle build that is not exercised continuously does
not work when it is finally needed.

Contents:

- **Application images** — control plane, worker, router, console, exported as
  an OCI layout tarball
- **Toolchain images** — the build sandbox: gcc, gfortran, CMake, pybind11,
  every supported Python ABI. This is the bulk of the size.
- **Python package mirror** — a pinned subset of PyPI sufficient for every
  supported build. Not all of PyPI; a resolved closure of the pinned
  dependency set, per platform and Python version.
- **OS package mirror** — the same, for whatever the toolchain images install
  at build time
- **Infrastructure images** — Postgres, MinIO, the registry, reverse proxy
- **Deployment manifests** — Helm chart and a Compose file; single-node and
  multi-node both need to be first-class
- **Database migrations** — as an offline, resumable step
- **SBOM per image** — CycloneDX or SPDX. Non-negotiable; see §8.
- **Checksums and a detached signature** over the whole bundle
- **Install runbook and a hardware sizing sheet**

Realistically 15–50 GB. Two properties matter more than size:

**Delta bundles.** A customer who updates quarterly should not move 40 GB each
time when only the application layer changed. Ship a full bundle per minor
version and deltas between them.

**Verifiable before install.** The customer's security team will want to
verify the signature and read the SBOM *before* anything is loaded into their
registry. That workflow has to be documented and must not require running any
of your code.

---

## 5. Licensing without phone-home

There is no metering, no usage-based billing, and no revocation. That is a
pricing consequence before it is an engineering one: **air-gapped installs are
site or seat licenses**, priced up front, because build-minutes cannot be
counted and reported.

The mechanism is an offline entitlement file: a signed document — a JWT or an
X.509 certificate — carrying customer identity, entitled features, a seat or
node limit, and an expiry, verified against a public key embedded in the
binary.

Three rules that are learned expensively otherwise:

**Expiry degrades, never hard-stops.** A license that lapses at 02:00 and
halts a running reservoir simulation converts a renewal conversation into an
incident and a lost account. Expiry moves the product to read-only: existing
builds keep serving, new builds are refused, and the console shows a
prominent, dismissible warning starting 60 days out.

**Assume the clock is wrong.** Air-gapped hosts drift and are sometimes rolled
back deliberately. Track the highest timestamp ever observed in local state
and refuse to accept a large backwards jump silently — but log it and warn
rather than failing closed.

**Do not build anti-tamper.** A determined customer with root on their own
hardware can bypass any check you write. Effort spent on obfuscation is effort
not spent on the product, and it reads as hostile during a security review.
Make the license a legal artifact with a technical reminder attached; breach
is a contract matter.

---

## 6. Attestation inside the gap

This is where the air-gap constraint touches the core of the product rather
than its packaging.

The hosted design assumes attestations accumulate in a ledger you operate. In
an air gap there is no such ledger, and there never can be — the records
describe the customer's proprietary source and cannot leave.

**The ledger becomes customer-owned and self-contained.** Entries are chained
locally into a Merkle structure, and checkpoints are signed with a key the
customer holds. You ship a trust anchor and the verification tooling; you
never see an entry.

Three requirements follow.

**The verifier must run offline and standalone.** A customer auditor must be
able to take an exported attestation bundle to a separate machine and verify
the chain with a tool that has no dependency on any service of yours. If
verification requires your API, the provenance claim is worthless to precisely
the customers who care about it most.

**Export must be a first-class operation.** `native2py attest export` produces
a portable, self-verifying archive covering a given artifact's full chain of
custody. This is the artifact that ships to a regulator or a customer's own
customer, and it is a large part of why this segment buys at all.

**Your own bundle needs provenance too.** The customer is installing a build
toolchain from a vendor into an environment that produces safety- or
revenue-critical numerical software. They will ask, correctly, how they know
the toolchain images are what you say they are. SLSA provenance and Sigstore
signatures over the bundle, verifiable offline with keys shipped out of band,
answer that. See §8 — this doubles as the strongest differentiator in the
product.

---

## 7. Support when you cannot see anything

You get no telemetry, no crash reports, no error tracking, and no SSH. This is
the single largest ongoing operational cost of the tier, and it is
underestimated in every plan that has not lived through it.

**A support bundle command is mandatory.** `native2py support-bundle` collects
versions, configuration, logs, resource state, and reconciler output into an
archive. Two properties decide whether it is usable:

- **Redaction by default.** No source code, no build outputs, no file paths
  from user projects, no secrets. Paths and identifiers are hashed. A bundle
  that might contain proprietary source will not be sent, so the redaction
  must be aggressive and *documented*, so their security team can approve the
  transfer once rather than reviewing every incident.
- **Human-reviewable.** Plain text and JSON the customer can read before
  sending. An opaque binary blob will be refused.

**Diagnostics must be local.** Everything the hosted console shows about a
failing build has to be present in the on-prem console and in CLI output,
because you will never be looking at a dashboard. Error messages carry more
weight here than anywhere else in the product: you cannot hotfix, and the next
release is a quarter away.

**Version skew is the standing tax.** Air-gapped customers update slowly.
Support **N-3 minor versions** concurrently, keep migrations forward-only and
resumable, and never require an intermediate version to be installed to
upgrade — that is unreasonable when each hop is a change-controlled physical
media transfer.

---

## 8. Their security review is a sales gate

The customer will scan every image, read every SBOM, and run a network capture
against the running product. Treat this as a product requirement with a
deadline, not as paperwork.

- **Minimal base images.** Distroless or slim wherever the toolchain permits.
  Every package is a CVE you will be asked about.
- **A CVE posture, not a CVE count.** Zero criticals is not achievable in
  images containing a full compiler toolchain. What is achievable, and what
  actually satisfies reviewers, is a documented policy: criticals triaged
  within N days, a stated patch cadence, and a per-release list of known
  findings with justifications.
- **SBOM per image**, generated in CI, shipped in the bundle.
- **Signed images and a signed bundle**, verifiable offline.
- **A no-egress assertion the customer can run themselves.** A documented
  procedure that puts the install in a network namespace with no route and
  demonstrates full function. This converts your strongest claim from an
  assurance into a demonstration.
- **Non-root, read-only rootfs, no privileged containers.** The gVisor
  requirement (see §3) will itself be questioned; be ready to explain why the
  build sandbox needs it and what it buys them.

---

## 9. What changes from the hosted design

Mapping [`design-cloud-run-lifecycle.md`](design-cloud-run-lifecycle.md) onto
this deployment, since roughly half of it does not apply and the other half
becomes more important.

| Hosted mechanism | Air-gapped |
|---|---|
| Scale-to-zero | **Irrelevant.** The customer owns the hardware; idle costs them nothing marginal. Keep services warm. |
| Cost-tiered retention (§4 there) | **Replaced by disk-pressure policy.** They have a fixed volume, not an elastic bill. Retention is configurable, defaults conservative, and alerts on capacity. |
| Free-tier hibernation | **Gone.** No free tier exists here. |
| Delete saga (§5 there) | **Retained, and still valuable** — for data hygiene, disk reclamation, and their own internal deletion obligations. |
| Reconciler (§6 there) | **Retained**, reconciling against the local container runtime, registry, and filesystem rather than GCP. The two-strike rule and blast-radius guard apply unchanged. |
| Blast-radius guard | **More important, not less.** There is no vendor on-call to catch a runaway. It must page *their* operator. |
| Per-project quotas | **More important.** A fixed box means one project can starve the others; cgroup limits are the only defence. |
| Erasure model (§8 there) | **Unchanged and still correct.** Hash-keyed attestations with a separate deletable owner mapping works identically offline. |

The single largest new requirement is **capacity planning as a deliverable**:
a sizing sheet giving CPU, RAM, and disk for N projects and M concurrent
builds, because the customer must procure hardware before they can install.
Nobody has to think about this in the hosted model, and it is the first thing
asked for here.

---

## 10. What will go wrong

| Failure | Mitigation |
|---|---|
| **A hidden egress call.** Some library phones home — a telemetry default, an update check, a font or CDN fetch in the console. Discovered by the customer's packet capture, during evaluation. | Single injectable HTTP client (§2) plus a no-network CI test. Audit third-party defaults explicitly; many are opt-out. |
| **Install fails on their PKI.** Internal CA not trusted, TLS interception in the proxy, or an unusual certificate chain. | Accept a CA bundle at install; test against a self-signed internal root in CI. |
| **Bundle rots.** Nobody buys for two quarters, the bundle job silently breaks, and the first real customer hits it. | Build the bundle on every release and install it end-to-end in a network-isolated CI job. |
| **Migration fails mid-upgrade** with no rollback and no vendor access. | Forward-only, resumable, idempotent migrations; a pre-upgrade backup step in the runbook that is not optional. |
| **Support bundle is refused** because legal cannot confirm it excludes source. | Aggressive default redaction, a documented field-by-field manifest, and a dry-run mode that shows exactly what would be sent. |
| **License expires during a production run.** | Read-only degradation, never a hard stop; warn from 60 days out (§5). |
| **Version skew beyond support.** Customer is four minors behind and hits a bug fixed three releases ago. | N-3 support policy stated in the contract; no required intermediate hops. |
| **Toolchain CVEs block procurement.** A compiler image cannot be CVE-free, and a reviewer with a zero-criticals checklist blocks the deal. | Published CVE policy and per-release justifications (§8). Engage security review early, not at signature. |

---

## 11. What to decide now

The point of writing this before more of the hosted product exists. Each of
these is nearly free today and expensive after another quarter of building.

1. **Platform abstraction over the six seams** (§3) — introduced now, even
   though only one implementation exists. Retrofitting an abstraction under
   working code that assumes Cloud Run is the expensive version.
2. **All egress through one injectable client** (§2), with a no-network test
   in CI from the first commit that adds an outbound call.
3. **Offline-first licensing and entitlements** (§5) — even the hosted tier
   reads entitlements from a local signed document, so there is only one code
   path and it is the one that works offline.
4. **A self-contained, exportable attestation ledger with a standalone offline
   verifier** (§6). This is the constraint with the deepest reach into the
   existing verification work, and the one that is genuinely hard to retrofit.
5. **No hardcoded wildcard-domain assumption** in routing (§3).
6. **Bundle build in CI from the first release** (§4), installed end-to-end in
   an isolated job.
7. **SBOM and image signing in the release pipeline** (§8) — needed for the
   hosted tier's own supply-chain story regardless, so this is not
   air-gap-specific work.

Items 1 and 4 are the ones worth being stubborn about. The rest can be added
later at roughly the cost of writing them; those two get more expensive with
every module that assumes a hosted control plane.

---

## 12. The business question this document does not answer

Supporting T3 properly is a standing cost: a slower release cadence, a support
model with no observability, a hardware sizing obligation, and a security
review in every sales cycle. The common rule of thumb is that an air-gapped
deal must be worth several times a comparable hosted deal to break even, and
that the tier should not be sold below a floor price.

The strategic case is not the margin on individual deals. It is that the
segment which cannot use a cloud product is the same segment with the most
valuable legacy Fortran, the strongest provenance requirements, and the least
competition — and that the provenance work this project is already doing
(see [`design-verification-layers.md`](design-verification-layers.md)) is
worth considerably more to a buyer under audit obligations than to a
self-serve user.

That is a positioning decision, not an engineering one. This document exists
so that making it in either direction, six months from now, does not invalidate
what has been built in the meantime.

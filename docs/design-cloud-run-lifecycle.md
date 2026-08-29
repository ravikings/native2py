# Design: hosting on Cloud Run, and the lifecycle of everything it creates

**Status:** proposal. Nothing here is implemented.
**Scope:** how a per-project REST + MCP endpoint is hosted, and — the larger
half of this document — how every cloud resource a project creates is tracked,
expired, and reclaimed.
**Companion documents:** [`design.md`](design.md) (the base specification),
[`design-verification-layers.md`](design-verification-layers.md) (attestation
and golden files, which §8 depends on),
[`BUSINESS-CASE.md`](BUSINESS-CASE.md).

This is written backwards from the hard part. Standing up a container that
serves a wheel is a week of work. Proving, eighteen months later, that
everything a deleted project ever created is actually gone — and that nothing
you stopped charging for is still quietly costing money — is the part that
determines whether the hosting has a cost floor or a cost slope.

---

## 1. The rule everything else follows from

**Postgres is the source of truth. GCP is an eventually-consistent projection
of it.** Every cloud resource exists because a row says it should, and every
row is reconciled against reality on a schedule.

The failure mode this prevents is the one that kills these systems: a delete
handler calls the GCP API inline, the API times out on step 4 of 9, the
request returns 500, the user retries, and now a half-deleted project has an
orphaned Artifact Registry repository that nobody will ever look at again.
Multiply by eighteen months and the cloud bill has a floor made entirely of
garbage.

So, as an invariant: **no request handler ever deletes a cloud resource.** A
handler writes intent to Postgres and returns. A worker executes that intent
idempotently, with retries. A reconciler independently sweeps for drift
between the two. Three separate mechanisms, because the first two will both
fail eventually and the third is what catches it.

The corollary is what makes cleanup tractable: if the reconciler finds a
resource in GCP with no owning row, that resource is garbage *by definition*,
regardless of how it got there. You never have to reason about provenance,
only about ownership.

### 1.1 Why Cloud Run

Cloud Run's first-generation execution environment runs workloads under
gVisor. That is the isolation boundary this project needs for untrusted
native code, without operating the hosts it runs on.

Versus AWS Lambda, the two differences that decide it:

- **Concurrency.** Lambda is one request per instance. Cloud Run serves up to
  1000 concurrent requests per instance, so a loaded native extension stays
  resident across requests — the expensive `dlopen` plus scientific-Python
  import happens once, not per invocation. For wrapped numerical code this is
  a large win.
- **MCP.** Cloud Run runs an ordinary uvicorn server, so MCP over Streamable
  HTTP and SSE work with no special handling. On Lambda you fight the
  execution model: Function URL response streaming, no true bidirectional
  session, a 15-minute wall.

Also relevant: 32 GiB memory ceiling versus 10 GB, 60-minute timeout versus
15, and per-project memory sizing is a config field rather than a capacity
planning exercise.

---

## 2. Serving and building are different shapes

Same platform, same gVisor sandbox, opposite network posture.

| Concern | Serving | Building |
|---|---|---|
| Product | Cloud Run **Service** | Cloud Run **Job** |
| Runs | Attested wheel, already built and verified | Untrusted C/C++/Fortran source, arbitrary compile |
| Network | Public ingress, egress deny-by-default | No ingress; egress to Artifact Registry + object store only |
| URL | `<project>.native2py.app` — REST + `/mcp` | None, ever |
| Lifetime | Scale-to-zero, wakes on request | Single execution, then gone |
| Memory | Per-project config field, up to 32 GiB | Sized to the build, up to 32 GiB |

The build Job produces an OCI image and pushes it to Artifact Registry; the
serving Service deploys that image **by immutable digest**. The two never
share a service account, and the build job never holds a credential that can
reach a serving surface. A compromised build burns compute; it must not be
able to become a persistent foothold.

### 2.1 MCP specifics

MCP over Streamable HTTP is one more route on the same uvicorn app. Two
things to get right:

- **Buffering.** SSE needs the proxy layer to flush rather than accumulate.
- **Timeouts.** Cloud Run's request timeout will cut a long-lived session. Set
  it deliberately (up to 60 minutes) and have the client reconnect rather than
  assuming the stream is eternal.

### 2.2 A quota to design around now

One Cloud Run service per project is clean at 100 and fine at 1,000, but
services-per-region is a finite quota in the low thousands. Past that the
shape changes to a single multi-tenant service with project routing and
per-tenant sandboxing — a materially different security design. Decide which
side of that line this is being built for **before** the routing layer
calcifies.

---

## 3. Everything a project creates

You cannot delete what you never wrote down. This inventory *is* the delete
plan. The order column is load-bearing: several of these pin each other, and
deleting out of order produces API errors that a naive retry loop chases
forever.

| # | Resource | Why tracked | Cost if orphaned |
|---|---|---|---|
| 1 | DNS record | Kill access first, before anything else moves | Trivial, but a dangling record is a subdomain-takeover surface |
| 2 | Domain mapping | Pins the service; must go before it | Blocks service deletion, holds a cert |
| 3 | `run.services` | The serving endpoint | Near-free idle, but consumes quota |
| 4 | `run.revisions` | **Pins image digests.** One per deploy, never auto-removed | Silently prevents all image GC |
| 5 | `run.jobs` + executions | Build definitions and history | Quota, log volume |
| 6 | Artifact Registry | Images, every tag, every digest | **The big one — GB-months, forever** |
| 7 | Storage: wheels | Build outputs, per version | GB-months, unbounded growth |
| 8 | Storage: sources | Uploaded source packs | Kilobytes — cheap to keep |
| 9 | Storage: goldens | Reference outputs | Small, but legally load-bearing |
| 10 | Build logs | Debugging trail | Log ingestion is a real line item |
| 11 | Secret Manager | Per-project config and keys | Small cost, **large security debt** |
| 12 | Service account | Per-project identity | Free — and that's the problem; orphans accrete silently |
| 13 | IAM bindings | Grants referencing that SA | Dangling grants outlive the SA and can be re-bound |
| 14 | CDN cache | Cached wheel downloads | Serves deleted content after deletion |
| 15 | Postgres rows | Project, calls, attestations | The record of everything above |

Every one of these is created with the same three labels, without exception:

```
n2p-managed = true
n2p-project = <project-id>
n2p-env     = prod | staging
```

The reconciler only ever touches resources carrying `n2p-managed=true`. That
single constraint is what lets an automated deleter run inside a shared GCP
project without becoming a loaded gun pointed at everything else in it.

### 3.1 The dependency that bites

Artifact Registry cannot reclaim an image while any Cloud Run revision
references it — and **revisions are not deleted when you deploy a new one**. A
project that ships fifty times has fifty revisions pinning fifty images. Prune
revisions on every deploy (keep the last 3), or image storage grows
monotonically no matter how good the cleanup policy is. See §7.

---

## 4. Retention is tiered by cost, not by one clock

The instinct is a single 30-day timer that removes everything. It is both too
aggressive and too generous: it throws away a 40 KB source pack that costs
nothing to keep, while letting a 900 MB image sit for a month at full price.
Split by what the resource actually costs.

| Resource | Free tier | Paid tier | Rationale |
|---|---|---|---|
| Container image | 30d idle | keep latest 3 | Dominant storage cost. Rebuildable from source. |
| Untagged digests | 7d | 7d | Failed and superseded builds. Pure waste. |
| Run service | 30d idle | indefinite | Free at zero scale, but consumes finite quota. |
| Built wheels | 90d | indefinite | Users may have pinned them in a requirements file. |
| Build logs | 14d | 90d | Debugging value decays fast; ingestion cost doesn't. |
| Source pack | 1y | indefinite | Kilobytes. Keeping it makes hibernation reversible. |
| Golden files | 1y | indefinite | Small, and the basis of every correctness claim. |
| Attestations | append-only | append-only | See §8. |

### 4.1 Hibernate, don't delete

This is the choice that makes a free tier both cheap and humane.

At 30 days of inactivity a free project does not die — it **hibernates**. The
Cloud Run service and the container image are deleted; the source pack, build
config, golden files and attestations are kept. Roughly 99% of the recurring
cost goes away. The project's page still loads, showing a **Wake this
project** button that rebuilds and redeploys in a minute or two.

You reclaim the expensive things immediately and keep the cheap things that
make the loss recoverable. Nobody loses work, no support ticket is ever "you
deleted my project without asking", and the cost curve behaves as though
everything had been deleted — because in dollars, it was.

Full purge comes at **day 180 of continuous hibernation**, with email at day
150 and day 173, and an export link live for the whole window.

---

## 5. The delete saga

When a user deletes a project, two things must be true immediately and
everything else may be eventually consistent: **access stops** and **billing
stops**. Reclamation is a background process that may take minutes and is
allowed to fail and retry.

Nine steps, each idempotent, each individually retryable, executed by a worker
and never by a request handler.

| # | Step | When | What happens |
|---|---|---|---|
| 1 | `mark` | t+0s | One Postgres transaction: `state=deleting`, `deleted_at=now()`, enqueue the saga. **The API returns here.** If the worker never runs, the reconciler still finishes the job. |
| 2 | `revoke` | t+1s | Remove DNS record, drop domain mapping, invalidate API keys, purge CDN prefix. Access is gone before any resource is touched, so a mid-saga failure never leaves a reachable endpoint. |
| 3 | `drain` | t+2s | `max-instances=0`; cancel queued and running build executions. Billing stops here. Cancel *before* delete — deleting a Job with a live execution is the classic stuck saga. |
| 4 | `hold` | grace | 7 days paid, 24 hours free. Invisible, costs nothing, fully restorable. Accidental deletion is the most common support ticket in every system lacking this step. |
| 5 | `unpin` | purge | Delete all revisions, then the service, then the Job. Must precede image deletion — revisions hold references and Artifact Registry will refuse or silently skip while they exist. |
| 6 | `images` | purge | Delete the project's Artifact Registry repository outright. Per-project repositories exist precisely so this is one call rather than a fragile enumeration. |
| 7 | `objects` | purge | Delete the object-store prefix: wheels, sources, goldens, logs. Prefix-scoped deletes, never a filter over a shared bucket — a filter bug here has unbounded cost. |
| 8 | `identity` | purge | IAM bindings first, then the service account, then its secrets. Reverse order leaves dangling bindings referencing a deleted principal, which a future SA of the same name can inherit. |
| 9 | `tombstone` | done | Drop project rows and PII; keep an append-only tombstone: project id, deletion timestamp, content hashes. Proof the deletion happened, carrying nothing about who owned it. |

### 5.1 Why a saga and not a transaction

There is no distributed transaction across DNS, Cloud Run, Artifact Registry,
object storage and IAM. Any step can fail independently, and step 6 can
succeed while its acknowledgement is lost. So every step must be safe when run
twice, and **"already deleted" is success, not an error** — a `404` from a
delete call being retried forever is the single most common bug in cleanup
code.

---

## 6. The reconciler

The component that determines whether this system has a cost floor. Build it
in week one, not after the first surprising invoice.

Nightly, list every GCP resource carrying `n2p-managed=true` and diff against
Postgres.

```python
for R in gcp.list_labelled("n2p-managed=true"):
    owner = db.lookup(R.labels["n2p-project"])

    if owner and owner.state == "active":
        pass                          # MATCHED — expected, no action

    elif owner is None or owner.state == "deleted":
        strikes[R.id] += 1            # ORPHAN — no owning row
        if strikes[R.id] >= 2:        # two consecutive nights
            enqueue_delete(R)         # never delete on first sighting

    elif owner.state == "active" and R.missing_in_gcp:
        mark_degraded(owner)          # DRIFT — repair, never delete

# blast radius guard — before any delete is dispatched
if len(pending_deletes) > max(10, 0.05 * fleet_size):
    abort_run(); page_oncall()        # a correct run is never this large
```

Three details carry almost all the value.

**The two-strike rule.** A resource created 200ms ago by an in-flight
provisioning saga has no committed owning row yet. Deleting on first sighting
means the reconciler races the creation path and destroys live projects under
load — the worst possible bug, because it only appears when you are busy.
Requiring two consecutive nightly sightings makes the race impossible.

**Drift repairs, never deletes.** If Postgres says a resource should exist and
GCP disagrees, rebuild it or flag the project degraded. A reconciler that
resolves disagreement by deleting will eventually delete a healthy fleet
because of one bad query.

**The blast-radius guard.** A correct run deletes a handful of things. If it
wants to delete 40% of the fleet, the reconciler is wrong, not the fleet — a
label rollout regressed, a query returned empty, a credential was scoped to
the wrong project. Refusing to act and paging a human is always the right
response to an implausibly large delete set. Twenty lines, and the difference
between an incident and a company-ending one.

Run it in **dry-run for the first two weeks**, logging what it *would* delete.
You will find the label you forgot to apply, and you will find it before it
costs anything.

---

## 7. Image cleanup, specifically

Container images are where the money silently goes; they get their own
mechanism. Three layers, increasing in authority:

1. **Artifact Registry cleanup policies** — declarative, native, no code.
   Delete untagged versions older than 7 days; keep the most recent 3 tagged
   versions. Set once per repository at creation.
2. **Revision pruning on every deploy** — because a cleanup policy cannot
   delete an image a live revision still pins. Prune to the last 3 revisions
   immediately after each successful deploy. **Without this, layer 1 quietly
   does nothing.**
3. **Repository deletion on project purge** — one call, everything gone, no
   enumeration to get wrong.

Tag by content digest, never by a mutable tag like `latest`, and deploy by
digest. This gives an exact answer to "is this image referenced?" instead of a
guess, and a rebuild producing identical bytes creates no new stored artifact.

**The tradeoff to make consciously:** base layers are shared within a
repository but *not* across repositories. Per-project repositories make
deletion trivial and also mean the ~800 MB scientific-Python base is stored
once per project. At 100 projects, pay it for the operational simplicity. At
1,000, move the base image to one shared repository that projects reference
but never own, keeping only the thin project layer per-repository.

**Cleanup policies need a dry run too.** Artifact Registry supports dry-run
mode. Use it for a full week before enforcing. A policy that misreads the
tagging convention deletes the image a live service is about to cold-start
from, and you find out when a scale-from-zero fails in production.

---

## 8. The erasure problem

This project's value rests on attestation: a durable, tamper-evident record of
what was built from what, and which sources a golden file came from (see
[`design-verification-layers.md`](design-verification-layers.md)). That ledger
is append-only by design — a provenance record you can quietly rewrite is not
a provenance record.

But a user who deletes their account is entitled to erasure, and under GDPR
that is a 30-day obligation, not a courtesy.

The resolution is to make the ledger structurally incapable of holding
personal data. **Attestation rows are keyed by content hash and contain no
identity** — no user id, no email, no project name, no source path. The
mapping from content hash to owner lives in a separate, ordinary, deletable
table.

Deleting a user drops the mapping. The attestation survives as an anonymous
statement of the form *"this artifact digest was produced from these source
digests at this time"* — exactly what it needed to be to serve its purpose,
and containing nothing to erase. The chain of custody stays intact; the person
disappears from it entirely.

**This must be designed in from the first migration.** Retrofitting identity
out of an append-only ledger means rewriting it, which defeats having had one.

---

## 9. Cost

100 projects, typical bursty per-project traffic, all cleanup mechanisms
running.

| Line | Assumption | Monthly |
|---|---|---|
| Cloud Run serving | ~1k req/project/mo, 200ms, 1 vCPU / 2 GiB, scale-to-zero | $5 – 30 |
| Cloud Run jobs | ~4 builds/project/mo, 3 min, 4 vCPU / 8 GiB | $15 – 40 |
| Artifact Registry | ~1.5 GB/project after pruning, ~150 GB total | $15 |
| Object storage | Wheels, sources, goldens, sccache — R2, zero egress | $8 |
| Postgres | Small managed instance | $25 |
| Logging | With 14 / 90-day retention set | $5 |
| DNS + TLS | Cloudflare + Google-managed certs | $0 |
| **Total** | ≈ **$0.75 – 1.25 per project per month** | **$73 – 123** |

Two caveats stated plainly. **Artifact Registry is the line that grows
fastest** — it is the one that punishes a missing cleanup policy, and the
figure above assumes revision pruning actually works. **Serverless economics
invert under sustained load**: if a handful of projects run genuinely heavy
numerical workloads continuously, dedicated compute becomes cheaper by a wide
margin. Set per-project CPU quotas from day one so this is discovered from a
dashboard rather than an invoice.

These are estimates from published list pricing at assumed utilisation. They
should be validated against a real month of usage before informing pricing.

---

## 10. What will go wrong

Named in advance, because each has a specific mitigation that is cheap now and
expensive later.

| Failure | Mitigation |
|---|---|
| **Delete lands mid-build.** A build is running when the project is deleted; Job delete blocks, the saga wedges, retries hammer a resource that will never become deletable. | Cancel executions in step 3 and wait for terminal state before step 5. |
| **Restore during grace period.** User deletes, restores on day 3, and the already-enqueued purge fires on day 7 anyway. | The purge worker re-reads project state at execution time and no-ops unless still `deleting`. |
| **Label rollout regression.** A deploy stops applying `n2p-managed`; the next reconciler run sees the whole new fleet as unowned. | The blast-radius guard catches it. Alert on any drop in labelled-resource count. |
| **Hibernation wakes cold and slow.** Waking needs a full rebuild — a minute or more. Users read that as broken. | Keep the image for 7 days past hibernation so recent wakes are instant; show real build progress after that. |
| **Orphaned service accounts accrete.** SAs are free, so nothing signals accumulation until you near the quota with thousands of grantable identities. | Include SAs in the reconciler; alert on count, not on cost. |
| **CDN serves deleted wheels.** Storage is purged but the edge still holds artifacts hours after deletion completed. | Purge the CDN prefix in step 2, alongside DNS — not at the end. |
| **Cleanup policy outruns a cold start.** A policy deletes the image a scaled-to-zero service is about to start from; the service is healthy until someone visits it. | Exempt digests referenced by any live revision; verify with dry-run before enforcing. |
| **Egress not actually denied.** The build sandbox needs registry and object-store access, so "no network" becomes "some network", then all network. | Allowlist by hostname via VPC egress controls; assert it in a test that fails the deploy. |

---

## 11. Build order

Sequenced so the expensive mistakes become impossible before the fleet is
large enough for them to hurt.

1. **Labels and the resource inventory table** — before any resource is
   created in anger. Retrofitting labels onto a live fleet is the one task
   here with no safe path.
2. **Provisioning saga** — worker-driven, idempotent, from the first project.
3. **Reconciler in dry-run** — two weeks of logs before it may delete
   anything.
4. **Delete saga** with grace period and restore.
5. **Artifact Registry cleanup policies** — dry-run first, then enforcing.
6. **Revision pruning on deploy** — small, and it unblocks step 5 entirely.
7. **Hibernation** for the free tier, once there is a free tier with idle
   projects in it.
8. **Blast-radius guard and cost alerting** — before the reconciler is ever
   allowed to delete unattended.

Steps 1 and 3 are the ones worth being stubborn about. Everything else can be
added later at roughly the cost of writing it; those two get dramatically more
expensive with every project onboarded without them.

"""Deploy backend that runs services on Google Cloud Run.

The hosted tier (docs/design-cloud-run-lifecycle.md). Exists because the
console itself cannot keep shelling out to ``docker run`` once it is hosted:
Cloud Run gives a container no Docker socket and no nested containers, so
the DockerBackend cannot function there at all. That is the concrete reason
this seam had to exist before the console could be hosted anywhere.

Shells out to ``gcloud`` rather than importing google-cloud-run, for the
same reason the Docker backend shells out to the ``docker`` CLI: no extra
dependency, and the CLI is present wherever the credentials are. Auth is
whatever ``gcloud`` already has — on Cloud Run that is the attached service
account via the metadata server, with no key material anywhere.

Four differences from the Docker backend that callers must not paper over:

* **No host port.** Cloud Run publishes a URL. ``port`` is always None; the
  ``url``/``mcp_url`` keys are the only portable way to reach a service.
* **Images must be published.** Cloud Run pulls from a registry, so a
  locally-built ``<slug>:latest`` is invisible to it. The image has to be in
  Artifact Registry under NGATE_ARTIFACT_REPO first.
* **Deploys are slow.** A revision rollout is tens of seconds, not the
  sub-second ``docker run``, hence the much larger timeout below.
* **Deletion is not immediate.** ``docs/design-cloud-run-lifecycle.md`` §5
  covers why reclamation is a saga; this backend does the two synchronous
  steps (service, image) and leaves revision pruning and orphan sweeping to
  that background machinery, which does not exist yet — see the note on
  ``remove_image``.

STATUS: written against the documented gcloud surface but **not yet
exercised against a live GCP project** — there is no billing account wired
up to this repo. Treat every command here as unverified until the first
real deploy, and expect the failure modes to be flag spellings rather than
logic. The Docker backend remains the default precisely so that an untested
path cannot become the one everybody runs.
"""

from __future__ import annotations

import json
import os
import subprocess

from console.backends.base import DeployBackend

# A revision rollout involves an image pull and a health check, so this is
# tens of seconds on a good day. The Docker backend's 30s would time out on
# nearly every real deploy.
GCLOUD_TIMEOUT = 300


def _service_name(slug: str) -> str:
    """Cloud Run service name for a project slug.

    Kept identical to the Docker container name so one project reads the
    same in both backends' logs, dashboards and error messages. Cloud Run
    requires [a-z]([-a-z0-9]*[a-z0-9])? and at most 49 characters; the
    ``ngate-svc-`` prefix costs 10 of those, which is checked rather than
    assumed because the failure is a rejected deploy with an opaque message.
    """
    name = f"ngate-svc-{slug}"
    if len(name) > 49:
        raise RuntimeError(
            f"Project slug {slug!r} is too long for Cloud Run: the service name "
            f"{name!r} is {len(name)} characters and the limit is 49. Rename the "
            f"project to at most {49 - len('ngate-svc-')} characters."
        )
    return name


def _project() -> str:
    project = os.environ.get("NGATE_GCP_PROJECT", "").strip()
    if not project:
        raise RuntimeError(
            "NGATE_GCP_PROJECT is not set, so the Cloud Run backend does not know "
            "which GCP project to deploy into. Set it, or set "
            "NGATE_DEPLOY_BACKEND=docker to deploy to the local Docker daemon."
        )
    return project


def _region() -> str:
    return os.environ.get("NGATE_GCP_REGION", "us-central1").strip() or "us-central1"


def _artifact_repo() -> str:
    repo = os.environ.get("NGATE_ARTIFACT_REPO", "").strip()
    if not repo:
        raise RuntimeError(
            "NGATE_ARTIFACT_REPO is not set. The Cloud Run backend can only deploy "
            "images that have been pushed to Artifact Registry — a locally built "
            "'<slug>:latest' is invisible to Cloud Run. Set it to a repository path "
            "like 'us-central1-docker.pkg.dev/my-project/nativegate'."
        )
    return repo.rstrip("/")


def _image_ref(slug: str) -> str:
    return f"{_artifact_repo()}/{slug}:latest"


def _run_gcloud(args: list[str], timeout: int = GCLOUD_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a gcloud command, returning the completed process.

    ``--quiet`` suppresses the interactive confirmation prompts that would
    otherwise block forever in a non-TTY: a deploy that hangs until timeout
    because gcloud is waiting on a y/n is indistinguishable from a slow
    rollout in the logs.
    """
    return subprocess.run(
        ["gcloud", *args, "--quiet"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class CloudRunBackend(DeployBackend):
    """Runs each project as its own Cloud Run service."""

    name = "cloudrun"

    def start_service(self, slug: str, build_id: int | None = None) -> dict:
        image = _image_ref(slug)

        # Same provenance stamped by the Docker backend, minus the image
        # digest: Cloud Run resolves the tag to a digest itself at deploy
        # time, and reading it back is a second API call whose only use is
        # a display field. The deployed revision records it either way.
        env = [f"NATIVEGATE_PROJECT={slug}"]
        if build_id is not None:
            env.append(f"NATIVEGATE_BUILD_ID={build_id}")

        proc = _run_gcloud(
            [
                "run",
                "deploy",
                _service_name(slug),
                "--image",
                image,
                "--project",
                _project(),
                "--region",
                _region(),
                "--platform",
                "managed",
                # Matches the Docker backend's 512m/1cpu (console-design.md §5).
                "--memory",
                "512Mi",
                "--cpu",
                "1",
                "--port",
                "8000",
                # Scale-to-zero is the whole cost model for the hosted tier
                # (design-cloud-run-lifecycle.md §4) — an idle project must
                # cost nothing, which is what makes 30-day hibernation
                # cheap rather than merely tidy.
                "--min-instances",
                "0",
                # A per-project ceiling so one runaway project cannot consume
                # the region's quota and take every other tenant down with it.
                "--max-instances",
                "10",
                # Public: these services are the product. Tenant isolation is
                # per-service, not per-network — nothing here is a shared
                # backend that authenticated ingress would protect.
                "--allow-unauthenticated",
                "--set-env-vars",
                ",".join(env),
                "--format",
                "value(status.latestCreatedRevisionName)",
            ]
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            # The overwhelmingly common cause is an unpublished image, and
            # "PERMISSION_DENIED on ...:latest" does not tell the user to go
            # push it. Name the actual fix.
            if "not found" in stderr.lower() or "denied" in stderr.lower():
                raise RuntimeError(
                    f"Cloud Run could not pull {image!r}. The image must be pushed to "
                    f"Artifact Registry before deploying — a locally built image is not "
                    f"visible to Cloud Run. Original error: {stderr}"
                )
            raise RuntimeError(f"Failed to deploy {_service_name(slug)!r}: {stderr}")

        revision = proc.stdout.strip()
        url = self._service_url(slug)
        if url is None:
            # A deploy that reports success but exposes no URL is a broken
            # service, not a running one. Failing here beats handing the user
            # a project page with a dead link on it.
            raise RuntimeError(
                f"Deployed {_service_name(slug)!r} but Cloud Run reported no URL for it."
            )

        return {
            "container_id": revision,
            "port": None,
            "url": f"{url}/",
            "mcp_url": f"{url}/mcp/",
        }

    def stop_service(self, slug: str) -> None:
        proc = _run_gcloud(
            [
                "run",
                "services",
                "delete",
                _service_name(slug),
                "--project",
                _project(),
                "--region",
                _region(),
                "--platform",
                "managed",
            ]
        )
        if proc.returncode != 0 and not _is_absent(proc.stderr):
            raise RuntimeError(
                f"Failed to delete Cloud Run service {_service_name(slug)!r}: "
                f"{proc.stderr.strip()}"
            )

    def remove_image(self, slug: str) -> None:
        """Delete the project's image from Artifact Registry.

        Deletes the tag only. Untagged digests and the revisions that still
        pin them are left to the cleanup policy and reconciler described in
        docs/design-cloud-run-lifecycle.md §7 — **neither of which exists
        yet**, so until they do, this backend leaks image storage for every
        project ever deployed. That is a known, bounded cost, recorded here
        rather than hidden behind a delete call that looks complete.

        Never raises, per the interface contract: project deletion must not
        fail because a registry refused.
        """
        try:
            image = _image_ref(slug)
            _run_gcloud(["artifacts", "docker", "images", "delete", image, "--delete-tags"])
        except (RuntimeError, OSError, subprocess.SubprocessError):
            # No repo configured, no gcloud on PATH, or the call timed out.
            # All three are storage-cost problems, and the contract says
            # project deletion must not fail because a registry refused.
            return

    def service_status(self, slug: str) -> dict:
        not_running = {"running": False, "port": None, "url": None, "mcp_url": None}

        proc = _run_gcloud(
            [
                "run",
                "services",
                "describe",
                _service_name(slug),
                "--project",
                _project(),
                "--region",
                _region(),
                "--platform",
                "managed",
                "--format",
                "json",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            return not_running

        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return not_running

        url = (data.get("status") or {}).get("url")
        if not url:
            return not_running

        # "Running" means Ready=True, not "the service resource exists". A
        # service whose latest revision failed to come up still describes
        # cleanly and still has a URL — reporting it as running would make
        # the orchestrator's health tracking permanently optimistic.
        ready = False
        for cond in (data.get("status") or {}).get("conditions") or []:
            if cond.get("type") == "Ready":
                ready = cond.get("status") == "True"
                break

        if not ready:
            return {"running": False, "port": None, "url": None, "mcp_url": None}

        return {
            "running": True,
            # Cloud Run publishes no host port. Callers must use `url`.
            "port": None,
            "url": f"{url}/",
            "mcp_url": f"{url}/mcp/",
        }

    def _service_url(self, slug: str) -> str | None:
        proc = _run_gcloud(
            [
                "run",
                "services",
                "describe",
                _service_name(slug),
                "--project",
                _project(),
                "--region",
                _region(),
                "--platform",
                "managed",
                "--format",
                "value(status.url)",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def log_follow_command(
        self,
        slug: str,
        tail: str | None = None,
        since: str | None = None,
    ) -> list[str]:
        """Stream this service's logs via ``gcloud beta run services logs tail``.

        ``tail`` and ``since`` are accepted and ignored, which is a real
        behavioural gap rather than an oversight: this command has no
        equivalent of ``docker logs --since``, so a tailer that dies cannot
        resume from where it stopped and the calls recorded during the gap
        are lost. console/calls.py's resume path is therefore only correct
        on the Docker backend today. Closing this needs a different source —
        ``gcloud logging read`` with a timestamp filter, paged rather than
        followed — and that is a larger change than this refactor.
        """
        return [
            "gcloud",
            "beta",
            "run",
            "services",
            "logs",
            "tail",
            _service_name(slug),
            "--project",
            _project(),
            "--region",
            _region(),
        ]


def _is_absent(stderr: str) -> bool:
    """True when gcloud failed because the resource simply is not there.

    Deletes have to be idempotent (the interface says stop_service is a
    no-op when nothing is running), and gcloud signals absence with a
    non-zero exit and a message rather than a distinct status code.
    """
    lowered = stderr.lower()
    return "not found" in lowered or "could not be found" in lowered

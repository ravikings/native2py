"""The generated Kubernetes manifests.

Parsed as YAML and asserted on the resulting objects rather than on the text.
A manifest is a data structure; substring checks would pass on a file that
`kubectl` rejects, which is the only failure mode that matters here.
"""

import pytest
import yaml

from nativegate.generators import k8s_gen


def _objects(**kwargs):
    manifests = k8s_gen.generate_k8s_manifests(
        kwargs.pop("service_name", "svc"), kwargs.pop("language", "cpp"), **kwargs
    )
    return {obj["kind"]: obj for obj in yaml.safe_load_all(manifests)}


def _container(**kwargs):
    deployment = _objects(**kwargs)["Deployment"]
    return deployment["spec"]["template"]["spec"]["containers"][0]


def test_manifests_are_valid_yaml_with_a_deployment_and_a_service():
    objects = _objects()

    assert set(objects) == {"Deployment", "Service"}
    assert objects["Service"]["spec"]["selector"] == {"app": "svc"}
    assert (
        objects["Deployment"]["spec"]["template"]["metadata"]["labels"]["app"] == "svc"
    )


def test_readiness_and_liveness_point_at_different_endpoints():
    # The single most important assertion in this file, because swapping them
    # is easy and both failure modes are bad:
    #   liveness -> /readyz  kills a DRAINING pod mid-request instead of
    #               letting it drain, turning a rolling deploy into dropped
    #               connections.
    #   readiness -> /healthz keeps routing new work to a pod that is shutting
    #               down, which is the burst of 502s draining exists to stop.
    container = _container()

    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    # A native extension can be slow to load; without a startup probe a slow
    # start reads as a failed start and the pod restarts forever.
    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"


def test_the_pod_is_hardened():
    objects = _objects()
    pod = objects["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_a_read_only_root_still_has_somewhere_to_write():
    # readOnlyRootFilesystem without this is a pod that cannot start: CPython
    # wants a temp dir, and numerical code often wants scratch space.
    objects = _objects()
    pod = objects["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in pod["volumes"])


def test_the_uid_matches_the_image():
    # The Dockerfile creates appuser with --uid 1000. A manifest asking for a
    # different runAsUser would fail against runAsNonRoot or land on a user
    # that does not own the install.
    from nativegate.generators import docker_gen

    pod = _objects()["Deployment"]["spec"]["template"]["spec"]

    assert pod["securityContext"]["runAsUser"] == 1000
    assert "--uid 1000 appuser" in docker_gen.generate_dockerfile("svc", "cpp", "svc")


@pytest.mark.parametrize(
    "language,expected", [("fortran", "1"), ("cpp", "2")]
)
def test_worker_count_matches_the_dockerfile_default(language, expected):
    # Drift here would silently reintroduce the COMMON-block bug at deploy
    # time, where it is far harder to see than in a Dockerfile.
    from nativegate.generators import docker_gen

    container = _container(language=language)
    env = {e["name"]: e.get("value") for e in container["env"]}

    assert env["WEB_CONCURRENCY"] == expected
    assert f"ENV WEB_CONCURRENCY={expected}" in docker_gen.generate_dockerfile(
        "svc", language, "svc"
    )


def test_termination_grace_outlasts_the_graceful_timeout():
    # gunicorn is generated with --graceful-timeout 30. A grace period shorter
    # than that means Kubernetes SIGKILLs the pod while a native call is still
    # finishing, which is exactly the in-flight loss draining is meant to avoid.
    pod = _objects()["Deployment"]["spec"]["template"]["spec"]

    assert pod["terminationGracePeriodSeconds"] > 30


def test_a_rollout_cannot_remove_a_healthy_pod_for_a_broken_one():
    # maxUnavailable: 0 means a new pod must pass readiness before an old one
    # goes. A native extension that fails to load then takes the ROLLOUT down
    # instead of the service.
    strategy = _objects()["Deployment"]["spec"]["strategy"]

    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


def test_memory_is_limited_but_cpu_is_not():
    # Deliberate asymmetry. Throttling a native numerical routine mid-call
    # inflates latency badly, and CPU is already bounded by WEB_CONCURRENCY.
    # Memory is limited because a native leak otherwise takes the NODE down
    # rather than the pod.
    resources = _container()["resources"]

    assert "memory" in resources["limits"]
    assert "cpu" not in resources["limits"]
    assert resources["requests"]["cpu"]


def test_api_keys_come_from_a_secret_only_when_auth_is_on():
    with_auth = {e["name"]: e for e in _container(auth="api_key")["env"]}
    assert with_auth["NATIVEGATE_API_KEYS"]["valueFrom"]["secretKeyRef"] == {
        "name": "svc-api-keys",
        "key": "keys",
    }

    # And NOT otherwise: a secretKeyRef to a Secret nobody was told to create
    # leaves the pod in CreateContainerConfigError, which is a baffling failure
    # to hand someone who never asked for authentication.
    without = {e["name"] for e in _container(auth="none")["env"]}
    assert "NATIVEGATE_API_KEYS" not in without


def test_an_api_key_service_tells_you_to_create_the_secret():
    # The service refuses to start without keys — by design. The manifest is
    # where someone will look when it does.
    manifests = k8s_gen.generate_k8s_manifests("svc", "cpp", auth="api_key")

    assert "kubectl create secret generic svc-api-keys" in manifests
    # Hyphenated in the manifest so the phrase survives line wrapping —
    # a warning split across two comment lines is one a reader skims past.
    assert "REFUSE-TO-START" in manifests

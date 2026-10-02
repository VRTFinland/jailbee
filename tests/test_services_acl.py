"""Host-global service ACL lifecycle."""

from unittest.mock import MagicMock

import pytest
import yaml

from jailbee.incus import IncusError
from jailbee.network import SERVICES_ACL, services_acl_yaml
from jailbee.services_acl import (
    EGRESS_PROXY_LABEL,
    LITELLM_LABEL,
    ensure_services_acl,
    set_service,
)

NFT_FLUSH_MISSING_STDERR = (
    "`incus network acl edit jailbee-services` failed: "
    "Error: Failed to run: nft -f -: exit status 1 "
    "(/dev/stdin:2:24-35: Error: No such file or directory; "
    "flush chain inet incus acl.incusbr0"
)


def test_ensure_creates_empty_acl_once():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False

    ensure_services_acl(incus)

    incus.network_acl_create.assert_called_once_with(SERVICES_ACL)
    written = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert written["egress"] == []


def test_ensure_is_noop_when_present():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True

    ensure_services_acl(incus)

    incus.network_acl_create.assert_not_called()
    incus.network_acl_set_yaml.assert_not_called()


def test_ensure_tolerates_missing_nft_chain_after_acl_creation():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False
    incus.network_acl_set_yaml.side_effect = IncusError(NFT_FLUSH_MISSING_STDERR)

    ensure_services_acl(incus)

    incus.network_acl_create.assert_called_once_with(SERVICES_ACL)
    incus.network_acl_set_yaml.assert_called_once()
    assert incus.network_acl_set_yaml.call_args.args[0] == SERVICES_ACL


def test_ensure_does_not_swallow_other_acl_write_errors():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False
    incus.network_acl_set_yaml.side_effect = IncusError("Error: yaml: invalid syntax")

    with pytest.raises(IncusError, match="invalid syntax"):
        ensure_services_acl(incus)


def _rules(incus: MagicMock) -> set[tuple[str, str, str]]:
    written = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    return {(r["description"], r["destination"], r["destination_port"]) for r in written["egress"]}


def _both_labels_acl() -> str:
    return services_acl_yaml(
        {
            LITELLM_LABEL: (["10.9.0.3"], [4000]),
            EGRESS_PROXY_LABEL: (["10.1.0.2"], [3128]),
        }
    )


def test_empty_services_acl_yaml_is_unchanged():
    assert services_acl_yaml({}) == (
        "name: jailbee-services\n"
        "description: jailbee service containers reachable from strict containers\n"
        "egress: []\n"
        "ingress: []\n"
    )


def test_set_service_writes_rules():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = services_acl_yaml({})

    set_service(incus, LITELLM_LABEL, (["10.9.0.3"], [4100]))

    assert incus.network_acl_set_yaml.call_args.args[0] == SERVICES_ACL
    assert _rules(incus) == {(LITELLM_LABEL, "10.9.0.3/32", "4100")}


def test_set_service_creates_missing_acl_before_writing_endpoint():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False
    incus.network_acl_show.return_value = services_acl_yaml({})

    set_service(incus, LITELLM_LABEL, (["10.9.0.3"], [4100]))

    calls = incus.mock_calls
    create = next(
        i
        for i, call in enumerate(calls)
        if call[0] == "network_acl_create" and call.args == (SERVICES_ACL,)
    )
    writes = [i for i, call in enumerate(calls) if call[0] == "network_acl_set_yaml"]
    assert create < writes[0] < writes[1]
    assert yaml.safe_load(calls[writes[-1]].args[1])["egress"][0]["destination_port"] == "4100"


def test_set_service_preserves_other_services():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = services_acl_yaml({LITELLM_LABEL: (["10.9.0.3"], [4000])})

    set_service(incus, EGRESS_PROXY_LABEL, (["10.1.0.2", "10.2.0.2"], [3128]))

    assert _rules(incus) == {
        (LITELLM_LABEL, "10.9.0.3/32", "4000"),
        (EGRESS_PROXY_LABEL, "10.1.0.2/32", "3128"),
        (EGRESS_PROXY_LABEL, "10.2.0.2/32", "3128"),
    }


def test_set_service_none_removes_only_that_label():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = _both_labels_acl()

    set_service(incus, LITELLM_LABEL, None)

    assert _rules(incus) == {(EGRESS_PROXY_LABEL, "10.1.0.2/32", "3128")}


def test_set_service_replaces_stale_rules_of_the_same_label():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = _both_labels_acl()

    set_service(incus, LITELLM_LABEL, (["10.9.0.7"], [4100]))

    assert _rules(incus) == {
        (LITELLM_LABEL, "10.9.0.7/32", "4100"),
        (EGRESS_PROXY_LABEL, "10.1.0.2/32", "3128"),
    }


def test_set_service_treats_unparsable_live_acl_as_empty():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = "- just\n- a list\n"

    set_service(incus, LITELLM_LABEL, (["10.9.0.3"], [4100]))

    assert _rules(incus) == {(LITELLM_LABEL, "10.9.0.3/32", "4100")}

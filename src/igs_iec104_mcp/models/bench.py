"""
A test bench: its networks, its runtimes, its external targets, its connections, and its
cleanup policy.

The bench is checked as a whole (IMPLEMENTATION.md §5, §6): every name is unique, every
reference resolves, the two ends of a connection have the roles they need, and the
endpoints share a network. A runtime that reaches an external target must be on an
external network.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from igs_iec104_mcp.models.common import Definition, Name, SchemaVersion
from igs_iec104_mcp.models.instance import Instance
from igs_iec104_mcp.models.target import ExternalTarget


class Network(Definition):
    """A network of the bench. An external network can reach the external targets."""

    name: Name
    external: bool = Field(
        default=False,
        description="Whether the runtimes on it may reach external targets (§6).",
    )


class Connection(Definition):
    """A connection from a client to a server, both declared by the bench."""

    client: Name = Field(
        description="The instance or external target that starts the connection.",
    )
    server: Name = Field(
        description="The instance or external target that accepts the connection.",
    )
    fault_proxy: StrictBool = Field(
        default=False,
        description="The connection goes through a fault proxy, which a scenario can "
        "interrupt or delay. Both ends must be managed instances on one network.",
    )


class CleanupPolicy(Definition):
    """
    When the bench's resources are removed, and whether the evidence is kept (§6, §10).
    """

    remove: Literal["success_and_failure", "success", "never"] = "success_and_failure"
    keep_evidence: bool = True


class TestBench(Definition):
    """The definition of a test bench."""

    schema_version: SchemaVersion
    name: Name
    networks: list[Network] = Field(min_length=1)
    instances: list[Instance] = Field(default_factory=list)
    external_targets: list[ExternalTarget] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    cleanup: CleanupPolicy = Field(default_factory=CleanupPolicy)

    @model_validator(mode="after")
    def _check_bench(self) -> TestBench:
        _all_unique([network.name for network in self.networks], "network")
        networks = {network.name: network for network in self.networks}
        roles = _roles_by_name(self.instances, self.external_targets)
        for instance in self.instances:
            if instance.network not in networks:
                raise ValueError(
                    f"instance {instance.name!r} is on network {instance.network!r}, "
                    f"which the "
                    f"bench does not declare; declare it in 'networks' "
                    f"(declared: {sorted(networks)})"
                )
        for target in self.external_targets:
            _check_target_network(target, networks)
        seen_pairs: set[tuple[str, str]] = set()
        for connection in self.connections:
            _check_connection(
                connection, roles, networks, self.instances, self.external_targets
            )
            pair = (connection.client, connection.server)
            if pair in seen_pairs:
                raise ValueError(
                    f"the connection from {connection.client!r} to "
                    f"{connection.server!r} "
                    "is declared twice"
                )
            seen_pairs.add(pair)
        return self


def _all_unique(names: list[str], what: str) -> None:
    if len(set(names)) != len(names):
        raise ValueError(
            f"two {what}s have the same name: each {what} name is used once"
        )


def _roles_by_name(
    instances: list[Instance], targets: list[ExternalTarget]
) -> dict[str, str]:
    """
    The role of each instance and target, by name. Instances and targets share one name
        space.
    """
    roles: dict[str, str] = {}
    named = [(i.name, i.role) for i in instances] + [(t.name, t.role) for t in targets]
    for name, role in named:
        if name in roles:
            raise ValueError(
                f"the name {name!r} is used twice: instances and external targets "
                "share one name space"
            )
        roles[name] = role
    return roles


def _check_target_network(target: ExternalTarget, networks: dict[str, Network]) -> None:
    if target.network not in networks:
        raise ValueError(
            f"external target {target.name!r} is on network {target.network!r}, which "
            f"the bench "
            "does not declare; declare it in 'networks'"
        )
    if not networks[target.network].external:
        raise ValueError(
            f"external target {target.name!r} is on network {target.network!r}, which "
            f"is not "
            "external: set 'external: true' on that network"
        )


def _check_connection(
    connection: Connection,
    roles: dict[str, str],
    networks: dict[str, Network],
    instances: list[Instance],
    targets: list[ExternalTarget],
) -> None:
    for end, name in (("client", connection.client), ("server", connection.server)):
        if name not in roles:
            raise ValueError(
                f"the connection's {end} {name!r} is neither an instance nor an "
                f"external target "
                f"of this bench (declared: {sorted(roles)})"
            )
    if roles[connection.client] != "client":
        raise ValueError(
            f"{connection.client!r} is a server, so it cannot start a connection: "
            f"name a client"
        )
    if roles[connection.server] != "server":
        raise ValueError(
            f"{connection.server!r} is a client, so it cannot accept a connection: "
            f"name a server"
        )
    if connection.client == connection.server:
        raise ValueError(f"a connection cannot join {connection.client!r} to itself")
    instance_networks = {instance.name: instance.network for instance in instances}
    target_names = {target.name for target in targets}
    client_is_target = connection.client in target_names
    server_is_target = connection.server in target_names
    if client_is_target and server_is_target:
        raise ValueError(
            f"a connection cannot join two external targets ({connection.client!r}, "
            f"{connection.server!r}): the bench manages neither"
        )
    if not client_is_target and not server_is_target:
        client_network = instance_networks[connection.client]
        server_network = instance_networks[connection.server]
        if client_network != server_network:
            raise ValueError(
                f"{connection.client!r} is on network {client_network!r} and "
                f"{connection.server!r} on {server_network!r}: put both on the same "
                f"network"
            )
        return
    managed = connection.server if client_is_target else connection.client
    if (
        managed in instance_networks
        and not networks[instance_networks[managed]].external
    ):
        raise ValueError(
            f"{managed!r} reaches an external target, so it must be on an external "
            f"network; "
            f"{instance_networks[managed]!r} is not external"
        )

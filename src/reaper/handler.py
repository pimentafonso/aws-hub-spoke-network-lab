"""TTL reaper: delete the lab's billable resources once they have expired.

Runs every 15 minutes from EventBridge Scheduler (see infra/account). A
resource is a candidate when it carries the lab's Project tag; it is expired
when its ExpiresAt tag has passed, when the tag is missing or invalid, or when
it is older than the hard cap (MAX_AGE_HOURS) whatever the tag says.

Only resources that bill by the hour or by the GB are deleted, most expensive
first. Free resources (VPCs, subnets, security groups, the transit gateway
itself, customer gateways, S3 buckets) are left for `lab.sh destroy`.

The reaper never trusts a list alone: the Resource Groups Tagging API keeps
returning resources for a while after they are deleted, so every resource is
read from its own service, with its current state, on every pass.

Also runs locally:
    python src/reaper/handler.py --list     every billable lab resource, expired or not
    python src/reaper/handler.py --check    the same, exit 1 if any exist
    python src/reaper/handler.py --dry-run  what a reaper run would delete now
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

PROJECT = os.environ.get("PROJECT", "aws-hub-spoke-network-lab")
MAX_AGE = timedelta(hours=float(os.environ.get("MAX_AGE_HOURS", "10")))
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
PASS_INTERVAL_S = 20
# Leave this much of the Lambda timeout for the last pass and the summary.
SAFETY_MARGIN_S = 60

log = logging.getLogger("reaper")
log.setLevel(logging.INFO)


@dataclass(frozen=True)
class Resource:
    kind: str
    id: str
    state: str
    tags: dict[str, str]
    created: datetime | None = None

    def expiry_reason(self, now: datetime) -> str | None:
        """Why this resource has expired, or None while it may keep running."""
        raw = self.tags.get("ExpiresAt")
        if raw is None:
            return "no ExpiresAt tag"
        try:
            expires = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return f"invalid ExpiresAt {raw!r}"
        if now >= expires:
            return f"expired at {raw}"
        if self.created is not None and now - self.created >= MAX_AGE:
            return f"older than the {MAX_AGE.total_seconds() / 3600:g} h cap"
        return None


@dataclass(frozen=True)
class Kind:
    """One billable resource type: how to list it and how to delete it."""

    name: str
    list: Callable[[Any], Iterable[Resource]]
    delete: Callable[[Any, str], None]
    actionable: frozenset[str]  # states in which a delete call can succeed
    waiting: frozenset[str]  # states on the way out, or blocked for now


@dataclass
class Clients:
    ec2: Any
    elbv2: Any

    @classmethod
    def default(cls) -> Clients:
        return cls(ec2=boto3.client("ec2"), elbv2=boto3.client("elbv2"))


def _tags(items: list[dict] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in items or []}


def _ours(tags: dict[str, str]) -> bool:
    return tags.get("Project") == PROJECT


TAG_FILTER = [{"Name": "tag:Project", "Values": [PROJECT]}]


def _pages(client: Any, operation: str, key: str, **kwargs: Any) -> Iterator[dict]:
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page.get(key, [])


# --- listers: every one re-checks the tag, whatever the server filter did ---


def _vpn_connections(c: Clients) -> Iterator[Resource]:
    for v in c.ec2.describe_vpn_connections(Filters=TAG_FILTER)["VpnConnections"]:
        tags = _tags(v.get("Tags"))
        if _ours(tags):
            yield Resource("vpn-connection", v["VpnConnectionId"], v["State"], tags)


def _vpc_endpoints(c: Clients) -> Iterator[Resource]:
    for e in _pages(c.ec2, "describe_vpc_endpoints", "VpcEndpoints", Filters=TAG_FILTER):
        tags = _tags(e.get("Tags"))
        # Gateway endpoints (S3, DynamoDB) are free.
        if _ours(tags) and e.get("VpcEndpointType") != "Gateway":
            yield Resource(
                "vpc-endpoint",
                e["VpcEndpointId"],
                e["State"].lower(),
                tags,
                e.get("CreationTimestamp"),
            )


def _load_balancers(c: Clients) -> Iterator[Resource]:
    lbs = list(_pages(c.elbv2, "describe_load_balancers", "LoadBalancers"))
    for i in range(0, len(lbs), 20):
        batch = lbs[i : i + 20]
        described = c.elbv2.describe_tags(ResourceArns=[lb["LoadBalancerArn"] for lb in batch])
        tags_by_arn = {d["ResourceArn"]: _tags(d.get("Tags")) for d in described["TagDescriptions"]}
        for lb in batch:
            tags = tags_by_arn.get(lb["LoadBalancerArn"], {})
            if _ours(tags):
                yield Resource(
                    "load-balancer",
                    lb["LoadBalancerArn"],
                    lb.get("State", {}).get("Code", "active"),
                    tags,
                    lb.get("CreatedTime"),
                )


def _nat_gateways(c: Clients) -> Iterator[Resource]:
    for n in _pages(c.ec2, "describe_nat_gateways", "NatGateways", Filter=TAG_FILTER):
        tags = _tags(n.get("Tags"))
        if _ours(tags):
            yield Resource("nat-gateway", n["NatGatewayId"], n["State"], tags, n.get("CreateTime"))


def _tgw_vpc_attachments(c: Clients) -> Iterator[Resource]:
    # VPN attachments go with their VPN connection; peering and Connect are not used.
    filters = [*TAG_FILTER, {"Name": "resource-type", "Values": ["vpc"]}]
    for a in _pages(
        c.ec2,
        "describe_transit_gateway_attachments",
        "TransitGatewayAttachments",
        Filters=filters,
    ):
        tags = _tags(a.get("Tags"))
        if _ours(tags):
            yield Resource(
                "tgw-vpc-attachment",
                a["TransitGatewayAttachmentId"],
                a["State"],
                tags,
                a.get("CreationTime"),
            )


def _flow_logs(c: Clients) -> Iterator[Resource]:
    for f in _pages(c.ec2, "describe_flow_logs", "FlowLogs", Filters=TAG_FILTER):
        tags = _tags(f.get("Tags"))
        if _ours(tags):
            yield Resource(
                "flow-log", f["FlowLogId"], f["FlowLogStatus"].lower(), tags, f.get("CreationTime")
            )


def _instances(c: Clients) -> Iterator[Resource]:
    for r in _pages(c.ec2, "describe_instances", "Reservations", Filters=TAG_FILTER):
        for i in r["Instances"]:
            tags = _tags(i.get("Tags"))
            if _ours(tags):
                yield Resource(
                    "instance", i["InstanceId"], i["State"]["Name"], tags, i.get("LaunchTime")
                )


def _elastic_ips(c: Clients) -> Iterator[Resource]:
    for a in c.ec2.describe_addresses(Filters=TAG_FILTER)["Addresses"]:
        tags = _tags(a.get("Tags"))
        if _ours(tags):
            # Released only once nothing uses it (a NAT gateway being deleted, say).
            state = "associated" if a.get("AssociationId") else "unassociated"
            yield Resource("elastic-ip", a["AllocationId"], state, tags)


def _volumes(c: Clients) -> Iterator[Resource]:
    for v in _pages(c.ec2, "describe_volumes", "Volumes", Filters=TAG_FILTER):
        tags = _tags(v.get("Tags"))
        if _ours(tags):
            yield Resource("volume", v["VolumeId"], v["State"], tags, v.get("CreateTime"))


# Most expensive first, and dependencies before what they depend on: the VPN
# connection removes its TGW attachment, endpoints go before the NLB they front,
# NAT gateways and instances before their Elastic IPs.
KINDS: list[Kind] = [
    Kind(
        "vpn-connection",
        _vpn_connections,
        lambda c, i: c.ec2.delete_vpn_connection(VpnConnectionId=i),
        frozenset({"pending", "available"}),
        frozenset({"deleting"}),
    ),
    Kind(
        "vpc-endpoint",
        _vpc_endpoints,
        lambda c, i: c.ec2.delete_vpc_endpoints(VpcEndpointIds=[i]),
        frozenset({"available", "pending", "pendingacceptance", "rejected", "failed"}),
        frozenset({"deleting"}),
    ),
    Kind(
        "load-balancer",
        _load_balancers,
        lambda c, i: c.elbv2.delete_load_balancer(LoadBalancerArn=i),
        frozenset({"active", "active_impaired", "provisioning", "failed"}),
        frozenset(),
    ),
    Kind(
        "nat-gateway",
        _nat_gateways,
        lambda c, i: c.ec2.delete_nat_gateway(NatGatewayId=i),
        frozenset({"pending", "available"}),
        frozenset({"deleting"}),
    ),
    Kind(
        "tgw-vpc-attachment",
        _tgw_vpc_attachments,
        lambda c, i: c.ec2.delete_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=i),
        frozenset({"available", "pendingAcceptance", "rejected", "failed"}),
        frozenset({"pending", "modifying", "deleting", "initiating", "rollingBack"}),
    ),
    Kind(
        "flow-log",
        _flow_logs,
        lambda c, i: c.ec2.delete_flow_logs(FlowLogIds=[i]),
        frozenset({"active"}),
        frozenset(),
    ),
    Kind(
        "instance",
        _instances,
        lambda c, i: c.ec2.terminate_instances(InstanceIds=[i]),
        frozenset({"pending", "running", "stopping", "stopped"}),
        frozenset({"shutting-down"}),
    ),
    Kind(
        "elastic-ip",
        _elastic_ips,
        lambda c, i: c.ec2.release_address(AllocationId=i),
        frozenset({"unassociated"}),
        frozenset({"associated"}),
    ),
    Kind(
        "volume",
        _volumes,
        lambda c, i: c.ec2.delete_volume(VolumeId=i),
        frozenset({"available", "error"}),
        frozenset({"creating", "in-use", "deleting"}),
    ),
]


def existing(clients: Clients) -> list[tuple[Kind, Resource]]:
    """Every billable lab resource that still exists, expired or not."""
    found = []
    for kind in KINDS:
        for r in kind.list(clients):
            if r.state in kind.actionable or r.state in kind.waiting:
                found.append((kind, r))
    return found


@dataclass
class Report:
    deleted: list[dict] = field(default_factory=list)
    remaining: list[dict] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    passes: int = 0
    dry_run: bool = False

    @property
    def quiet(self) -> bool:
        return not (self.deleted or self.remaining or self.errors)

    def summary(self) -> str:
        verb = "Would delete" if self.dry_run else "Deleted"
        lines = [f"{verb} {len(self.deleted)} expired lab resource(s) in {self.passes} pass(es)."]
        lines += [f"- {d['kind']} {d['id']}: {d['reason']}" for d in self.deleted]
        if self.remaining:
            lines.append(f"Still present ({len(self.remaining)}), retried on the next run:")
            lines += [f"- {r['kind']} {r['id']} ({r['state']})" for r in self.remaining]
        if self.errors:
            lines.append("Errors:")
            lines += [f"- {rid}: {err}" for rid, err in self.errors.items()]
        return "\n".join(lines)


def reap(
    clients: Clients,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    seconds_left: Callable[[], float] = lambda: 0,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
) -> Report:
    """Delete expired resources, pass after pass, until none is left or time runs out.

    A pass deletes whatever can be deleted now. Resources that are on their
    way out, or blocked by another (an Elastic IP still held by a NAT gateway
    being deleted), are picked up by a later pass.
    """
    report = Report(dry_run=dry_run)
    requested: set[str] = set()
    while True:
        report.passes += 1
        remaining = []
        for kind, r in existing(clients):
            reason = r.expiry_reason(now())
            if reason is None:
                continue
            if r.state in kind.actionable and r.id not in requested:
                try:
                    if not dry_run:
                        kind.delete(clients, r.id)
                except ClientError as e:
                    report.errors[r.id] = e.response["Error"]["Code"]
                else:
                    requested.add(r.id)
                    report.errors.pop(r.id, None)
                    report.deleted.append({"kind": r.kind, "id": r.id, "reason": reason})
                    continue
            remaining.append({"kind": r.kind, "id": r.id, "state": r.state})
        report.remaining = remaining
        if dry_run or not remaining or seconds_left() < PASS_INTERVAL_S + SAFETY_MARGIN_S:
            return report
        sleep(PASS_INTERVAL_S)


def handler(event: dict, context: Any) -> dict:
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    report = reap(
        Clients.default(),
        seconds_left=lambda: context.get_remaining_time_in_millis() / 1000,
        dry_run=dry_run,
    )
    result = {
        "deleted": report.deleted,
        "remaining": report.remaining,
        "errors": report.errors,
        "passes": report.passes,
        "dry_run": dry_run,
    }
    log.info(json.dumps(result, default=str))
    if not report.quiet and TOPIC_ARN:
        boto3.client("sns").publish(
            TopicArn=TOPIC_ARN,
            Subject=f"nwlab reaper: {len(report.deleted)} deleted, {len(report.remaining)} left",
            Message=report.summary(),
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="list billable lab resources")
    mode.add_argument("--check", action="store_true", help="like --list, exit 1 if any exist")
    mode.add_argument("--dry-run", action="store_true", help="show what a run would delete")
    args = parser.parse_args(argv)

    clients = Clients.default()
    if args.dry_run:
        print(reap(clients, dry_run=True).summary())
        return 0
    now = datetime.now(UTC)
    found = existing(clients)
    for _, r in found:
        reason = r.expiry_reason(now) or f"expires {r.tags.get('ExpiresAt')}"
        print(f"{r.kind:20} {r.id}  {r.state:14} {reason}")
    print(f"{len(found)} billable lab resource(s) found.")
    return 1 if args.check and found else 0


if __name__ == "__main__":
    logging.basicConfig()
    sys.exit(main())

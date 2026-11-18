from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from reaper import handler
from reaper.handler import Clients, Resource, reap

REGION = "eu-west-1"
# moto stamps real creation times, so the test clock is the real one too.
NOW = datetime.now(UTC).replace(microsecond=0)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


PAST = stamp(NOW - timedelta(hours=1))
FUTURE = stamp(NOW + timedelta(hours=4))


def lab_tags(expires=PAST, project=handler.PROJECT):
    tags = [{"Key": "Project", "Value": project}]
    if expires is not None:
        tags.append({"Key": "ExpiresAt", "Value": expires})
    return tags


def spec(resource_type, tags):
    return [{"ResourceType": resource_type, "Tags": tags}]


# --- expiry rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "created", "expected"),
    [
        ({"ExpiresAt": FUTURE}, None, None),
        ({"ExpiresAt": PAST}, None, f"expired at {PAST}"),
        ({"ExpiresAt": stamp(NOW)}, None, f"expired at {stamp(NOW)}"),
        ({}, None, "no ExpiresAt tag"),
        ({"ExpiresAt": "tomorrow"}, None, "invalid ExpiresAt 'tomorrow'"),
        ({"ExpiresAt": FUTURE}, NOW - timedelta(hours=9, minutes=59), None),
        ({"ExpiresAt": FUTURE}, NOW - timedelta(hours=10), "older than the 10 h cap"),
    ],
)
def test_expiry_reason(tags, created, expected):
    assert Resource("instance", "i-1", "running", tags, created).expiry_reason(NOW) == expected


# --- against a mocked account -----------------------------------------------------


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield Clients(ec2=boto3.client("ec2"), elbv2=boto3.client("elbv2"))


def build_lab(c, expires=PAST, project=handler.PROJECT):
    """One of each billable kind, all carrying the given tags."""
    tags = lab_tags(expires, project)
    ec2 = c.ec2
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    nat_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(
        SubnetId=subnet, AllocationId=nat_eip, TagSpecifications=spec("natgateway", tags)
    )
    ec2.allocate_address(Domain="vpc", TagSpecifications=spec("elastic-ip", tags))
    ec2.run_instances(
        ImageId="ami-12c6146b",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet,
        TagSpecifications=spec("instance", tags),
    )
    ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=8, TagSpecifications=spec("volume", tags))
    tgw = ec2.create_transit_gateway()["TransitGateway"]["TransitGatewayId"]
    attachment = ec2.create_transit_gateway_vpc_attachment(
        TransitGatewayId=tgw, VpcId=vpc, SubnetIds=[subnet]
    )["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
    # moto ignores TagSpecifications on TGW attachments; real AWS applies them.
    ec2.create_tags(Resources=[attachment], Tags=tags)
    cgw = ec2.create_customer_gateway(Type="ipsec.1", PublicIp="203.0.113.1", BgpAsn=65000)
    ec2.create_vpn_connection(
        Type="ipsec.1",
        CustomerGatewayId=cgw["CustomerGateway"]["CustomerGatewayId"],
        TransitGatewayId=tgw,
        TagSpecifications=spec("vpn-connection", tags),
    )
    ec2.create_vpc_endpoint(
        VpcId=vpc,
        ServiceName=f"com.amazonaws.{REGION}.ssm",
        VpcEndpointType="Interface",
        SubnetIds=[subnet],
        TagSpecifications=spec("vpc-endpoint", tags),
    )
    boto3.client("s3").create_bucket(
        Bucket="flow-log-bucket", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    ec2.create_flow_logs(
        ResourceIds=[vpc],
        ResourceType="VPC",
        TrafficType="ALL",
        LogDestinationType="s3",
        LogDestination="arn:aws:s3:::flow-log-bucket",
        TagSpecifications=spec("vpc-flow-log", tags),
    )
    c.elbv2.create_load_balancer(Name="nwlab-nlb", Type="network", Subnets=[subnet], Tags=tags)


def kinds(c):
    return sorted(r.kind for _, r in handler.existing(c))


ALL_KINDS = sorted(
    [
        "elastic-ip",
        "flow-log",
        "instance",
        "load-balancer",
        "nat-gateway",
        "tgw-vpc-attachment",
        "volume",
        "vpc-endpoint",
        "vpn-connection",
    ]
)


def run(c, **kwargs):
    budget = iter(range(1000, 0, -100))  # ten passes at most
    return reap(
        c, now=lambda: NOW, seconds_left=lambda: next(budget, 0), sleep=lambda s: None, **kwargs
    )


def test_existing_finds_every_billable_kind(aws):
    build_lab(aws)
    assert kinds(aws) == ALL_KINDS


def test_expired_lab_is_deleted(aws):
    build_lab(aws)
    report = run(aws)
    assert sorted(d["kind"] for d in report.deleted) == ALL_KINDS
    assert report.errors == {}
    assert report.remaining == []
    assert kinds(aws) == []


def test_lab_within_its_expiry_is_left_alone(aws):
    build_lab(aws, expires=FUTURE)
    report = run(aws)
    assert report.quiet
    assert kinds(aws) == ALL_KINDS


def test_other_projects_are_never_touched(aws):
    build_lab(aws, project="aws-event-driven-orders")
    assert run(aws).quiet
    assert len(aws.ec2.describe_instances()["Reservations"]) == 1


def test_missing_expiry_counts_as_expired(aws):
    build_lab(aws, expires=None)
    assert len(run(aws).deleted) == len(ALL_KINDS)


def test_dry_run_deletes_nothing(aws):
    build_lab(aws)
    report = run(aws, dry_run=True)
    assert len(report.deleted) == len(ALL_KINDS)
    assert report.passes == 1
    assert kinds(aws) == ALL_KINDS
    assert report.summary().startswith(f"Would delete {len(ALL_KINDS)}")


def test_failed_delete_is_reported_and_retried(aws, monkeypatch):
    build_lab(aws)
    calls = {"n": 0}
    real = aws.ec2.delete_nat_gateway

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "DependencyViolation"}}, "DeleteNatGateway")
        return real(**kwargs)

    monkeypatch.setattr(aws.ec2, "delete_nat_gateway", flaky)
    report = run(aws)
    assert calls["n"] == 2
    assert report.errors == {}
    assert "nat-gateway" in [d["kind"] for d in report.deleted]


def test_blocked_resources_are_left_for_the_next_run(aws):
    build_lab(aws)
    # Associate the tagged Elastic IP with the instance: it stays blocked.
    eip = aws.ec2.describe_addresses(Filters=handler.TAG_FILTER)["Addresses"][0]
    instance = aws.ec2.describe_instances()["Reservations"][0]["Instances"][0]["InstanceId"]
    aws.ec2.associate_address(AllocationId=eip["AllocationId"], InstanceId=instance)
    report = reap(aws, now=lambda: NOW, seconds_left=lambda: 0, sleep=lambda s: None)
    assert report.passes == 1
    assert {"kind": "elastic-ip", "id": eip["AllocationId"], "state": "associated"} in (
        report.remaining
    )
    assert "Still present" in report.summary()


class FakeContext:
    def get_remaining_time_in_millis(self):
        return 0


def test_handler_publishes_a_summary_only_when_something_happened(aws, monkeypatch):
    sns = boto3.client("sns")
    topic = sns.create_topic(Name="nwlab-alerts")["TopicArn"]
    monkeypatch.setattr(handler, "TOPIC_ARN", topic)
    published = []
    monkeypatch.setattr(handler.boto3, "client", _recording_client(published))

    assert handler.handler({}, FakeContext())["deleted"] == []
    assert published == []

    build_lab(aws)
    result = handler.handler({}, FakeContext())
    assert len(result["deleted"]) == len(ALL_KINDS)
    assert len(published) == 1
    assert published[0]["Subject"].startswith(f"nwlab reaper: {len(ALL_KINDS)} deleted")


def _recording_client(published):
    real = boto3.client

    def client(name, *args, **kwargs):
        c = real(name, *args, **kwargs)
        if name == "sns":
            original = c.publish

            def publish(**kw):
                published.append(kw)
                return original(**kw)

            c.publish = publish
        return c

    return client

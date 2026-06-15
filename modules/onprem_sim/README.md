# `modules/onprem_sim`

Provisions a separate VPC that simulates an on-premise datacenter, plus the AWS-side glue (`aws_customer_gateway`, `aws_vpn_connection`) needed to bring up a Site-to-Site IPsec tunnel against the Virtual Private Gateway exposed by [`modules/network`](../network).

The simulated router is a single EC2 instance running [strongSwan](https://www.strongswan.org/) and Quagga BGP, deployed by embedding the existing CloudFormation template at [`templates/vpn-gateway-strongswan.yml`](../../templates/vpn-gateway-strongswan.yml) inside an `aws_cloudformation_stack`. Terraform pipes the AWS-generated tunnel parameters (PSKs, inside/outside IPs, BGP ASNs) into the stack so the bootstrap script can configure both tunnels deterministically.

The module is intentionally minimal — single AZ, permissive security group, no flow logs — because the whole point is to *simulate* an external network for the academic lab, not to harden one.

## Resources

- `aws_vpc.onprem` + `aws_internet_gateway.igw` — the simulated on-premise VPC (default `192.168.0.0/16`).
- `aws_subnet.public` + `aws_route_table.public` + `aws_route.public_default` + `aws_route_table_association.public` — single public subnet in `var.azs[0]` with default route to the IGW and `map_public_ip_on_launch = true`.
- `aws_security_group.router` — permissive SG: ingress for IKE (UDP 500/4500) and ESP/AH (IP proto 50/51) from the world, all inbound from `var.aws_vpc_cidr`, all egress. The CFN template attaches its own SG to the EC2 NIC; this one is here for diagnostics and is exported via outputs but not directly attached.
- `aws_eip.gw` — Elastic IP allocated up-front so its `public_ip` is stable as `aws_customer_gateway.ip_address` and as `pEipAllocationId` for the CFN stack.
- `aws_customer_gateway.cgw` — `bgp_asn = 65000` (matches `pLocalBgpAsn` default of the template), `type = "ipsec.1"`, `ip_address = aws_eip.gw.public_ip`.
- `aws_vpn_connection.vpn` — BGP-based (`static_routes_only = false`) Site-to-Site VPN between `var.vpn_gateway_id` and the customer gateway above. AWS auto-generates two tunnels with their PSKs and inside `/30` link networks.
- `aws_secretsmanager_secret.tunnel{1,2}` + `aws_secretsmanager_secret_version.tunnel{1,2}` — store each tunnel's PSK as `{"psk": "<value>"}`, matching the `jq -r '.SecretString' | jq -r '.psk'` parser in the CFN template's `set-psk.sh`.
- `aws_cloudformation_stack.strongswan` — deploys [`templates/vpn-gateway-strongswan.yml`](../../templates/vpn-gateway-strongswan.yml). All tunnel/BGP/EIP/VPC parameters are wired from the resources above. The template declares the EC2's primary NIC as a dedicated `AWS::EC2::NetworkInterface` (`rVpnGatewayEni`) and exposes its ID via the stack `Outputs` (`VpnGatewayInstanceId`, `VpnGatewayPrimaryEniId`), so Terraform can consume the ENI ID without any post-creation data lookup.
- `aws_route.to_aws_vpc` — adds `var.aws_vpc_cidr → strongSwan ENI` to `aws_route_table.public`. The next-hop is read directly from `aws_cloudformation_stack.strongswan.outputs["VpnGatewayPrimaryEniId"]`. Without this static route, on-prem traffic destined for AWS-side private IPs (in particular the SQS VPCE ENIs) would not enter the VPN.
- `data.aws_network_interface.sqs_vpce` — `for_each` over the SQS VPCE ENI IDs received as input, used to read each ENI's `private_ip`.
- `aws_route53_zone.sqs_private` — Private Hosted Zone scoped to `sqs.<region>.amazonaws.com`, associated **only** with the on-prem VPC. Coexists with the AWS-managed PHZ that `private_dns_enabled = true` creates inside the AWS VPC; Route 53 supports two PHZs with the same name on disjoint VPC associations.
- `aws_route53_record.sqs_apex` — A record at the zone apex with the list of private IPs of the SQS VPCE ENIs (TTL 60s).
- `aws_security_group.producer` + `aws_instance.producer` (`for_each` over `producer-1`, `producer-2`) — two Terraform-managed EC2 instances that bootstrap Python + boto3, install `/opt/onprem-tx-producer/tx_producer.py`, and enable `onprem-tx-producer.service` to enqueue synthetic transactions continuously. Disabled when `enable_traffic_producers = false`.

## Inputs

| Name                        | Type           | Default              | Description                                                                                  |
| --------------------------- | -------------- | -------------------- | -------------------------------------------------------------------------------------------- |
| `project`                   | `string`       | n/a                  | Prefix for resource names.                                                                   |
| `vpn_gateway_id`            | `string`       | n/a                  | ID of the AWS-side VGW (from `modules/network`).                                             |
| `aws_vpc_cidr`              | `string`       | n/a                  | CIDR of the AWS-side VPC, used to authorise return traffic in the on-prem SG.                |
| `azs`                       | `list(string)` | n/a                  | Available AZs; only the first is used for the on-prem public subnet.                         |
| `onprem_vpc_cidr`           | `string`       | `"192.168.0.0/16"`   | CIDR of the simulated on-prem VPC. Must not overlap `aws_vpc_cidr`.                          |
| `onprem_public_subnet_cidr` | `string`       | `"192.168.1.0/24"`   | CIDR of the public subnet that hosts the strongSwan EC2.                                     |
| `instance_type`             | `string`       | `"t3.micro"`         | EC2 type for the VPN gateway. Restricted to the values allowed by the CFN template.          |
| `sqs_vpc_endpoint_network_interface_ids` | `list(string)` | `[]`    | ENIs of the SQS Interface VPC Endpoint in the AWS VPC. When non-empty, the module creates the SQS PHZ + apex A record. |
| `enable_traffic_producers`  | `bool`         | `true`               | Create two EC2 producers that continuously send to the ingestion SQS queue.                  |
| `ingestion_queue_url`       | `string`       | `""`                 | SQS queue URL wired into producer user-data (required when `enable_traffic_producers = true`). |
| `producer_instance_type`    | `string`       | `"t3.micro"`         | EC2 type for traffic producers.                                                              |
| `producer_batch_size`       | `number`       | `5`                  | Messages sent per producer loop iteration.                                                     |
| `producer_loop_interval_sec`| `number`       | `12`                 | Sleep between iterations (~25 tx/min per producer; ~50 tx/min total with two producers).     |
| `producer_fraud_pct`        | `number`       | `8`                  | Percentage of generated transactions with fraud patterns.                                    |
| `tags`                      | `map(string)`  | `{}`                 | Common tags merged with `Component = "onprem-sim"`.                                          |

## Outputs

| Name                       | Description                                                                  |
| -------------------------- | ---------------------------------------------------------------------------- |
| `onprem_vpc_id`            | ID of the simulated on-prem VPC.                                             |
| `onprem_vpc_cidr`          | CIDR of the simulated on-prem VPC.                                           |
| `onprem_public_subnet_id`  | ID of the public subnet hosting the strongSwan EC2.                          |
| `vpn_gateway_public_ip`    | Public EIP of the strongSwan EC2 (also the CGW `ip_address`).                |
| `customer_gateway_id`      | ID of the Customer Gateway.                                                  |
| `vpn_connection_id`        | ID of the Site-to-Site VPN connection.                                       |
| `cloudformation_stack_id`  | ID of the embedded CloudFormation stack that runs the EC2 + strongSwan.      |
| `traffic_producer_instance_ids` | Map of on-prem traffic producer EC2 instance IDs (`producer-1`, `producer-2`). |
| `traffic_producer_private_ips`  | Map of private IPs for on-prem traffic producers.                            |

## Notes

- **Why an embedded CloudFormation stack?** The template at [`templates/vpn-gateway-strongswan.yml`](../../templates/vpn-gateway-strongswan.yml) is the source of truth for the strongSwan + Quagga bootstrap. Translating its ~940 lines of `cfn-init` into Terraform `user_data` would invite drift; embedding it via `aws_cloudformation_stack` keeps the YAML authoritative.
- **AMI drift suppression.** The template's `pAmiId` resolves an SSM Parameter to the latest Amazon Linux 2 AMI, which moves over time. The stack uses `lifecycle { ignore_changes = [parameters["pAmiId"]] }` to avoid perpetual drift; taint the stack manually when an AMI rotation is intended.
- **PSK handling.** `aws_vpn_connection.tunnel{1,2}_preshared_key` is sensitive throughout; it is only ever read into `aws_secretsmanager_secret_version.secret_string` and never surfaced as an output.
- **AWS Academy.** The CFN template references `arn:aws:iam::<account>:instance-profile/LabInstanceProfile`, which exists in the lab account. Secrets Manager reads use the default permissions on `LabRole`/`LabInstanceProfile`.
- **Idempotency caveat.** `aws_cloudformation_stack` triggers an UPDATE whenever any parameter changes. Because the AWS-generated PSKs and inside IPs are stable across plans (pinned in state by the `aws_vpn_connection` resource), re-running `terraform apply` with no input changes produces no diff once the first apply succeeds.
- **Private DNS for SQS from on-prem.** The PHZ `sqs.<region>.amazonaws.com` is associated only with the on-prem VPC, so on-prem clients resolve SQS to the private IPs of the AWS-side VPCE without needing DHCP options changes or a Route 53 Resolver Inbound Endpoint. The static route `var.aws_vpc_cidr → strongSwan ENI` is what makes the resolved private IPs actually reachable through the VPN.
- **Continuous traffic producers.** When `enable_traffic_producers = true`, two EC2 instances bootstrap a Python sender (`files/tx_producer.py`) via user-data and keep a `systemd` unit running after boot. Tune `producer_batch_size`, `producer_loop_interval_sec`, and `producer_fraud_pct` to change steady load. Stop/start with `systemctl {stop,start} onprem-tx-producer` over SSM Session Manager.
- **Checkov skips.** The permissive security group (`CKV_AWS_24`, `CKV_AWS_260`), the lack of secret rotation (`CKV2_AWS_57`) and the AWS-managed KMS key on Secrets Manager (`CKV_AWS_149`) are intentional trade-offs for this academic simulation and are skipped inline with explanatory comments.

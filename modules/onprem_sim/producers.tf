locals {
  traffic_producer_keys = var.enable_traffic_producers ? toset(["producer-1", "producer-2"]) : toset([])
}

data "aws_ami" "amazon_linux" {
  count = var.enable_traffic_producers ? 1 : 0

  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-hvm-*-x86_64-gp2"]
  }
}

data "aws_iam_instance_profile" "lab" {
  count = var.enable_traffic_producers ? 1 : 0

  name = "LabInstanceProfile"
}

resource "aws_security_group" "producer" {
  count = var.enable_traffic_producers ? 1 : 0

  # checkov:skip=CKV2_AWS_5: egress-only SG for lab producers; no ingress paths are required because SSM uses outbound HTTPS and responses are established connections.
  name        = format("%s-producer-sg", local.name_prefix)
  description = "Egress-only security group for simulated on-prem SQS traffic producers."
  vpc_id      = aws_vpc.onprem.id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-producer-sg", local.name_prefix)
  })
}

resource "aws_vpc_security_group_egress_rule" "producer_https_aws_vpc" {
  count = var.enable_traffic_producers ? 1 : 0

  security_group_id = aws_security_group.producer[0].id
  description       = "SQS Interface VPC Endpoint traffic toward the AWS-side VPC over the VPN."
  cidr_ipv4         = var.aws_vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_vpc_security_group_egress_rule" "producer_https_internet" {
  count = var.enable_traffic_producers ? 1 : 0

  security_group_id = aws_security_group.producer[0].id
  description       = "HTTPS for Systems Manager, package mirrors, and pip during bootstrap."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_instance" "producer" {
  for_each = local.traffic_producer_keys

  # checkov:skip=CKV_AWS_126: detailed CloudWatch monitoring disabled on lab EC2 to reduce cost.
  # checkov:skip=CKV_AWS_135: on-prem producers are intentionally single-AZ in this academic simulation.
  ami                         = data.aws_ami.amazon_linux[0].id
  instance_type               = var.producer_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.producer[0].id]
  iam_instance_profile        = data.aws_iam_instance_profile.lab[0].name
  associate_public_ip_address = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/templates/producer-userdata.sh.tpl", {
    region            = data.aws_region.current.name
    queue_url         = var.ingestion_queue_url
    batch_size        = var.producer_batch_size
    loop_interval_sec = var.producer_loop_interval_sec
    fraud_pct         = var.producer_fraud_pct
    wait_script       = file("${path.module}/files/wait_for_sqs_dns.sh")
    producer_script   = file("${path.module}/files/tx_producer.py")
  })

  user_data_replace_on_change = true

  tags = merge(local.module_tags, {
    Component = "ec2-instance"
    Name      = format("%s-%s", local.name_prefix, each.key)
    Role      = "traffic-producer"
    Index     = each.key
  })

  depends_on = [
    aws_cloudformation_stack.strongswan,
    aws_route.to_aws_vpc,
    aws_route53_record.sqs_apex,
  ]
}

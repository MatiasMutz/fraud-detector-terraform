data "aws_region" "current" {}

locals {
  module_tags = merge(var.tags, {
    Service = "fraud-engine"
  })

  cluster_name        = format("%s-cluster", var.project)
  service_name        = format("%s-fraud-engine", var.project)
  task_family         = format("%s-fraud-engine", var.project)
  log_group_name      = format("/ecs/%s-fraud-engine", var.project)
  ecr_repository_name = format("%s/fraud-engine", var.project)

  resolved_image_uri = var.image_uri != "" ? var.image_uri : format("%s:placeholder", aws_ecr_repository.app.repository_url)

  container_definitions = [
    {
      name      = "fraud-engine"
      image     = local.resolved_image_uri
      essential = true

      cpu                    = var.task_cpu
      memory                 = var.task_memory
      readonlyRootFilesystem = true
      privileged             = false

      environment = [
        { name = "AWS_REGION", value = data.aws_region.current.name },
        { name = "QUEUE_URL", value = var.queue_url },
        { name = "QUEUE_NAME", value = var.queue_name },
        { name = "DYNAMODB_TABLE_NAME", value = var.table_name },
        { name = "RESULTS_QUEUE_URL", value = var.results_queue_url },
        { name = "FRAUD_ALERT_QUEUE_URL", value = var.fraud_alert_queue_url },
        { name = "S3_AUDIT_BUCKET", value = var.audit_bucket_name },
        { name = "PROCESSOR_CONCURRENCY", value = tostring(var.processor_concurrency) },
        { name = "PROCESSOR_POLLERS", value = tostring(var.processor_pollers) },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "fraud-engine"
        }
      }
    }
  ]
}

resource "aws_ecr_repository" "app" {
  # checkov:skip=CKV_AWS_136: AWS Academy no permite KMS CMK; AES256 (default) cumple el requisito de cifrado at-rest.
  name                 = local.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.module_tags, {
    Component = "ecr-repository"
    Name      = local.ecr_repository_name
  })
}

resource "aws_cloudwatch_log_group" "app" {
  # checkov:skip=CKV_AWS_158: AWS Academy no permite KMS CMK; los logs viajan por TLS y se cifran con la clave AWS-owned.
  # checkov:skip=CKV_AWS_338: retención corta acorde al alcance académico; se reduce costo en una cuenta efímera de lab.
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = merge(local.module_tags, {
    Component = "cloudwatch-log-group"
    Name      = local.log_group_name
  })
}

resource "aws_ecs_cluster" "main" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.module_tags, {
    Component = "ecs-cluster"
    Name      = local.cluster_name
  })
}

resource "aws_security_group" "task" {
  name        = format("%s-task-sg", var.project)
  description = "Fargate task security group; no ingress; egress to interface VPC endpoint security group only."
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-task-sg", var.project)
  })
}

resource "aws_vpc_security_group_egress_rule" "task_to_endpoints" {
  security_group_id            = aws_security_group.task.id
  description                  = "HTTPS hacia los Interface VPC Endpoints"
  referenced_security_group_id = var.endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

data "aws_prefix_list" "s3" {
  name = format("com.amazonaws.%s.s3", data.aws_region.current.name)
}

data "aws_prefix_list" "dynamodb" {
  name = format("com.amazonaws.%s.dynamodb", data.aws_region.current.name)
}

resource "aws_vpc_security_group_egress_rule" "task_to_s3" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS hacia S3 via Gateway VPC Endpoint (audit log)"
  prefix_list_id    = data.aws_prefix_list.s3.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_vpc_security_group_egress_rule" "task_to_dynamodb" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS hacia DynamoDB via Gateway VPC Endpoint (user profiles)"
  prefix_list_id    = data.aws_prefix_list.dynamodb.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_ecs_task_definition" "app" {
  # checkov:skip=CKV_AWS_249: AWS Academy expone únicamente LabRole; task_role y execution_role deben coincidir por la restricción del entorno.
  family                   = local.task_family
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]

  task_role_arn      = var.task_role_arn
  execution_role_arn = var.execution_role_arn

  container_definitions = jsonencode(local.container_definitions)

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  tags = merge(local.module_tags, {
    Component = "ecs-task-definition"
    Name      = local.task_family
  })
}

resource "aws_ecs_service" "app" {
  name            = local.service_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [
    aws_vpc_security_group_egress_rule.task_to_endpoints,
  ]

  tags = merge(local.module_tags, {
    Component = "ecs-service"
    Name      = local.service_name
  })
}

resource "aws_appautoscaling_target" "ecs" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = format("service/%s/%s", aws_ecs_cluster.main.name, aws_ecs_service.app.name)
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"

  tags = merge(local.module_tags, {
    Component = "autoscaling-target"
  })
}

resource "aws_appautoscaling_policy" "queue_depth_target" {
  name               = format("%s-queue-depth-target", var.project)
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = var.scaling_target_messages_per_task
    scale_in_cooldown  = 120
    scale_out_cooldown = 60

    customized_metric_specification {
      metrics {
        id    = "m_visible"
        label = "MessagesVisible"

        metric_stat {
          metric {
            metric_name = "ApproximateNumberOfMessagesVisible"
            namespace   = "AWS/SQS"

            dimensions {
              name  = "QueueName"
              value = var.queue_name
            }
          }
          stat = "Average"
        }

        return_data = false
      }

      metrics {
        id    = "m_running"
        label = "RunningTaskCount"

        metric_stat {
          metric {
            metric_name = "RunningTaskCount"
            namespace   = "ECS/ContainerInsights"

            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.main.name
            }

            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.app.name
            }
          }
          stat = "Average"
        }

        return_data = false
      }

      metrics {
        id          = "messages_per_task"
        label       = "MessagesPerTask"
        expression  = "m_visible / IF(m_running > 0, m_running, 1)"
        return_data = true
      }
    }
  }
}

resource "aws_appautoscaling_policy" "queue_depth_step" {
  name               = format("%s-queue-depth-step", var.project)
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 100
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = 100
      scaling_adjustment          = 2
    }
  }
}

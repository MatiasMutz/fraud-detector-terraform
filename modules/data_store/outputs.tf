output "table_name" {
  description = "Nombre de la tabla DynamoDB que persiste el comportamiento histórico de usuarios."
  value       = aws_dynamodb_table.user_behavior.name
}

output "table_arn" {
  description = "ARN de la tabla DynamoDB que persiste el comportamiento histórico de usuarios."
  value       = aws_dynamodb_table.user_behavior.arn
}

output "table_id" {
  description = "Identificador interno (id) de la tabla DynamoDB."
  value       = aws_dynamodb_table.user_behavior.id
}

output "hash_key_name" {
  description = "Nombre del atributo hash key configurado en la tabla DynamoDB."
  value       = var.hash_key_name
}

output "db_address" {
  description = "Hostname del endpoint RDS (sin puerto); usar como DB_HOST en las Lambdas."
  value       = aws_db_instance.results.address
}

output "db_port" {
  description = "Puerto de la instancia RDS PostgreSQL."
  value       = aws_db_instance.results.port
}

output "db_name" {
  description = "Nombre de la base de datos inicial creada en la instancia."
  value       = aws_db_instance.results.db_name
}

output "db_username" {
  description = "Nombre del usuario master de la instancia PostgreSQL."
  value       = aws_db_instance.results.username
}

output "db_credentials_secret_arn" {
  description = "ARN del secret de Secrets Manager que contiene username/password para PostgreSQL."
  value       = aws_secretsmanager_secret.db_credentials.arn
}

output "db_endpoint" {
  description = "Endpoint completo en formato host:port."
  value       = aws_db_instance.results.endpoint
}

output "rds_security_group_id" {
  description = "ID del Security Group de la instancia RDS; expuesto para que la composición raíz agregue las reglas de ingress desde las Lambdas."
  value       = aws_security_group.rds.id
}

output "db_instance_id" {
  description = "Identificador de la instancia RDS."
  value       = aws_db_instance.results.identifier
}

output "proxy_endpoint" {
  description = "Hostname del endpoint del RDS Proxy; usar como DB_HOST en las Lambdas en lugar del endpoint directo de RDS."
  value       = aws_db_proxy.results.endpoint
}

output "proxy_security_group_id" {
  description = "ID del Security Group del RDS Proxy; expuesto para que la composición raíz agregue las reglas de ingress/egress desde las Lambdas."
  value       = aws_security_group.proxy.id
}

output "audit_bucket_name" {
  description = "Nombre del bucket S3 donde el scoring engine escribe el audit log de cada transacción procesada."
  value       = aws_s3_bucket.audit.id
}

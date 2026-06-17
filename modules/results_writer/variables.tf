variable "project" {
  description = "Nombre del proyecto, usado como prefijo en los nombres de los recursos del módulo."
  type        = string

  validation {
    condition     = length(var.project) > 0 && length(var.project) <= 24
    error_message = "El nombre de proyecto debe tener entre 1 y 24 caracteres."
  }
}

variable "tags" {
  description = "Tags comunes a propagar a todos los recursos creados por el módulo."
  type        = map(string)
  default     = {}
}

variable "principal_arn" {
  description = "ARN del rol IAM usado como execution role de la Lambda y para operar la cola SQS. En AWS Academy se reutiliza LabRole."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:iam::\\d{12}:role/.+$", var.principal_arn))
    error_message = "principal_arn debe ser un ARN de rol IAM válido."
  }
}

variable "vpc_id" {
  description = "ID de la VPC donde se despliega la Lambda."
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs de las subnets privadas de aplicación donde se despliega la Lambda."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 1
    error_message = "Se requiere al menos 1 subnet privada."
  }
}

variable "endpoint_security_group_id" {
  description = "ID del Security Group de los Interface VPC Endpoints; la Lambda abre egress TCP/443 hacia este SG."
  type        = string
}

variable "db_host" {
  description = "Hostname del endpoint RDS (DB_HOST en la Lambda)."
  type        = string
}

variable "db_port" {
  description = "Puerto del endpoint RDS (DB_PORT en la Lambda)."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Nombre de la base de datos PostgreSQL (DB_NAME en la Lambda)."
  type        = string
  default     = "fraud_results"
}

variable "db_credentials_secret_arn" {
  description = "ARN del secret de Secrets Manager con username/password de PostgreSQL."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:\\d{12}:secret:.+$", var.db_credentials_secret_arn))
    error_message = "db_credentials_secret_arn debe ser un ARN valido de Secrets Manager."
  }
}

variable "package_file" {
  description = "Ruta local del paquete zip de la Lambda results-writer. La composición raíz lo construye desde app/results_writer."
  type        = string
}

variable "sqs_batch_size" {
  description = "Tamaño máximo del lote SQS para la Lambda writer. AWS SQS standard queues allow up to 10,000 records por lote."
  type        = number
  default     = 5000

  validation {
    condition     = var.sqs_batch_size >= 1 && var.sqs_batch_size <= 10000
    error_message = "sqs_batch_size debe estar entre 1 y 10000."
  }
}

variable "sqs_batching_window_seconds" {
  description = "Ventana de batching en segundos para el event source mapping de SQS."
  type        = number
  default     = 5

  validation {
    condition     = var.sqs_batching_window_seconds >= 0 && var.sqs_batching_window_seconds <= 300 && (var.sqs_batch_size <= 10 || var.sqs_batching_window_seconds >= 1)
    error_message = "sqs_batching_window_seconds debe estar entre 0 y 300; si sqs_batch_size es mayor que 10, la ventana debe ser de al menos 1 segundo."
  }
}

variable "log_retention_days" {
  description = "Días de retención de los logs de la Lambda en CloudWatch Logs."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "log_retention_days debe ser un valor permitido por CloudWatch Logs."
  }
}
